"""Accounts core: passwords, signup codes, registration, people, sessions, invites, lockout. No network."""
import pytest

from app import accounts as ac


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def a(clock):
    acc = ac.Accounts(now=clock)
    acc.sync_codes(["code-one", "code-two"])
    return acc


def register(a, email="owner@acme.test", code="code-one", company="Acme Ltd"):
    return a.register_company(company, "Olivia Owner", email, "a-long-password", code, "Asia/Kolkata")


def test_passwords_are_hashed_with_salt_and_verify_only_when_right():
    h1, h2 = ac.hash_password("a-long-password"), ac.hash_password("a-long-password")
    assert h1 != h2 and "a-long-password" not in h1 and h1.startswith("scrypt$")
    assert ac.verify_password("a-long-password", h1) and not ac.verify_password("a-long-passworD", h1) and not ac.verify_password("x", "garbage")


@pytest.mark.parametrize("pw,email,bad", [("short", "", True), ("password123", "", True), ("owner@acme.test", "owner@acme.test", True),
                                          ("owner", "owner@acme.test", True), ("x" * 201, "", True), ("a-long-password", "", False)])
def test_password_policy(pw, email, bad):
    assert bool(ac.password_problem(pw, email)) is bad


def test_registration_needs_a_valid_single_use_code_and_creates_company_and_owner(a):
    org, owner = register(a)
    assert org["id"].startswith("org_") and not org["is_demo"] and owner["access_role"] == "owner" and owner["status"] == "active"
    assert owner["password_hash"] and "a-long-password" not in owner["password_hash"]
    with pytest.raises(ac.AccountError) as e:
        register(a, "second@other.test", "code-one")  # already used
    assert e.value.status == 403
    with pytest.raises(ac.AccountError):
        register(a, "third@other.test", "nope")
    assert register(a, "third@other.test", "code-two")[0]["id"] != org["id"]


def test_a_removed_code_stops_working_and_a_failed_registration_refunds_the_code(a):
    a.sync_codes(["code-two"])
    with pytest.raises(ac.AccountError):
        register(a, code="code-one")
    register(a, "o@a.test", "code-two")
    with pytest.raises(ac.AccountError):  # duplicate email is refused before the code is spent
        register(a, "o@a.test", "code-two")
    a.sync_codes(["code-two", "code-three"])
    assert register(a, "p@a.test", "code-three")


def test_one_email_belongs_to_one_company_case_insensitive(a):
    register(a, "Owner@Acme.test")
    with pytest.raises(ac.AccountError) as e:
        register(a, "owner@acme.test", "code-two")
    assert e.value.status == 409


def test_sign_in_throttle_and_generic_errors(a, clock):
    register(a)
    for _ in range(5):
        with pytest.raises(ac.AccountError) as e:
            a.authenticate("owner@acme.test", "wrong-password", "1.2.3.4")
        assert e.value.status == 401 and "not right" in str(e.value)
    with pytest.raises(ac.AccountError) as e:
        a.authenticate("owner@acme.test", "a-long-password", "1.2.3.4")  # even the right password waits
    assert e.value.status == 429
    with pytest.raises(ac.AccountError) as e2:
        a.authenticate("owner@acme.test", "a-long-password", "9.9.9.9")  # changing the reported address does not dodge the lockout
    assert e2.value.status == 429
    clock.t += 16 * 60
    assert a.authenticate("owner@acme.test", "a-long-password", "1.2.3.4")
    with pytest.raises(ac.AccountError) as e:
        a.authenticate("nobody@acme.test", "whatever-long-pw", "1.2.3.4")
    assert str(e.value) == "The email or password is not right."  # the same words for unknown email and wrong password


def test_sessions_expire_when_idle_or_old_and_die_on_logout_and_deactivation(a, clock):
    org, owner = register(a)
    token, csrf = a.create_session(owner, "1.1.1.1", "ua")
    assert a.get_session(token)["user"]["id"] == owner["id"] and a.get_session(token)["csrf"] == csrf
    assert a.s.one("SELECT COUNT(*) c FROM sessions WHERE id_hash=?", (token,))["c"] == 0  # only a hash is stored
    clock.t += ac.SESSION_IDLE_S + 1
    assert a.get_session(token) is None
    t2, _ = a.create_session(owner)
    clock.t += ac.SESSION_ABSOLUTE_S + 1
    assert a.get_session(t2) is None
    t3, _ = a.create_session(owner)
    a.end_session(t3)
    assert a.get_session(t3) is None
    assert a.get_session("forged-token") is None and a.get_session("") is None
    t4, _ = a.create_session(owner)
    member = a.create_user(org["id"], "m@acme.test", "Mia", "member", status="active", password="a-long-password")
    t5, _ = a.create_session(member)
    a.update_user(org["id"], member["id"], status="disabled")
    assert a.get_session(t5) is None and a.get_session(t4)
    with pytest.raises(ac.AccountError):
        a.authenticate("m@acme.test", "a-long-password")


def test_a_refused_session_says_why_in_a_fixed_word(a, clock):
    org, owner = register(a)
    token, _ = a.create_session(owner)
    assert a.session_state(token)[1] is None and a.session_state("")[1] == "no_cookie" and a.session_state("forged")[1] == "unknown_session"
    clock.t += ac.SESSION_IDLE_S + 1
    assert a.session_state(token) == (None, "idle") and a.session_state(token) == (None, "unknown_session")  # a refused session is removed, so the next look finds nothing
    t2, _ = a.create_session(owner)
    clock.t += ac.SESSION_ABSOLUTE_S + 1
    assert a.session_state(t2) == (None, "expired")
    member = a.create_user(org["id"], "m@acme.test", "Mia", "member", status="active", password="a-long-password")
    t3, _ = a.create_session(member)
    a.s.q("UPDATE users SET status='disabled' WHERE id=?", (member["id"],))
    assert a.session_state(t3) == (None, "user_disabled")


def test_invites_are_single_use_expire_and_make_the_person_active(a, clock):
    org, owner = register(a)
    user, token = a.create_invite(org["id"], "Mia@Acme.test", "Mia Member", "member", owner["id"])
    assert user["status"] == "invited" and token not in str(a.s.all("SELECT * FROM invites"))
    assert a.peek_invite(token)["company"] == "Acme Ltd"
    with pytest.raises(ac.AccountError):
        a.authenticate("mia@acme.test", "anything-long-1", "ip")  # cannot sign in before accepting
    with pytest.raises(ac.AccountError):
        a.accept_invite(token, "short")
    u = a.accept_invite(token, "a-long-password")
    assert u["status"] == "active" and a.authenticate("mia@acme.test", "a-long-password")
    with pytest.raises(ac.AccountError) as e:
        a.accept_invite(token, "another-long-password")
    assert e.value.status == 410 and a.peek_invite(token) is None
    _, t2 = a.create_invite(org["id"], "late@acme.test", "Late", "member", owner["id"])
    clock.t += ac.INVITE_TTL_S + 1
    assert a.peek_invite(t2) is None
    with pytest.raises(ac.AccountError):
        a.accept_invite(t2, "a-long-password")
    with pytest.raises(ac.AccountError):
        a.create_invite(org["id"], "mia@acme.test", "Dup", "member", owner["id"])
    with pytest.raises(ac.AccountError):
        a.create_invite(org["id"], "x@acme.test", "X", "owner", owner["id"])


def test_reset_link_changes_the_password_and_signs_everyone_out(a):
    org, owner = register(a)
    token, _ = a.create_session(owner)
    link = a.create_reset_link(org["id"], owner["id"], owner["id"])
    a.accept_invite(link, "brand-new-password")
    assert a.get_session(token) is None
    assert a.authenticate("owner@acme.test", "brand-new-password")
    with pytest.raises(ac.AccountError):
        a.authenticate("owner@acme.test", "a-long-password")


def test_people_edits_owner_rules_and_reporting_line(a):
    org, owner = register(a)
    m1, _ = a.create_invite(org["id"], "m1@acme.test", "M1", "member", owner["id"])
    m2, _ = a.create_invite(org["id"], "m2@acme.test", "M2", "member", owner["id"], manager_id=m1["id"])
    assert a.update_user(org["id"], m1["id"], department=" Engineering ")["department"] == "Engineering"
    with pytest.raises(ac.AccountError):
        a.update_user(org["id"], m1["id"], manager_id=m2["id"])  # a loop: m1 -> m2 -> m1
    with pytest.raises(ac.AccountError):
        a.update_user(org["id"], m1["id"], manager_id=m1["id"])
    with pytest.raises(ac.AccountError):
        a.update_user(org["id"], owner["id"], access_role="member")
    with pytest.raises(ac.AccountError):
        a.update_user(org["id"], owner["id"], status="disabled")
    with pytest.raises(ac.AccountError):
        a.update_user(org["id"], m1["id"], access_role="owner")
    assert a.update_user(org["id"], m1["id"], access_role="admin")["access_role"] == "admin"
    with pytest.raises(ac.AccountError):  # a field outside the allowed set is silently ignored, an unknown status is refused
        a.update_user(org["id"], m1["id"], status="invited")
    out = a.update_user(org["id"], m1["id"], password_hash="x", id="u_hacked")  # fields outside the allowed set are ignored
    assert out["id"] == m1["id"] and a.get_user(m1["id"])["password_hash"] is None


def test_people_of_one_company_are_invisible_and_untouchable_from_another(a):
    org1, o1 = register(a)
    org2, o2 = register(a, "o2@other.test", "code-two", "Other Co")
    m, _ = a.create_invite(org1["id"], "m@acme.test", "Mia", "member", o1["id"])
    assert [u["email"] for u in a.list_users(org2["id"])] == ["o2@other.test"]
    with pytest.raises(ac.AccountError) as e:
        a.update_user(org2["id"], m["id"], name="Hacked")
    assert e.value.status == 404
    with pytest.raises(ac.AccountError):
        a.create_reset_link(org2["id"], m["id"], o2["id"])
    with pytest.raises(ac.AccountError):
        a.create_invite(org2["id"], "z@other.test", "Z", "member", o2["id"], manager_id=m["id"])


def test_demo_session_can_switch_person_only_inside_the_demo_company(a):
    demo = a.create_org("Acme Demo", is_demo=True, org_id="acme-demo")
    d1 = a.create_user("acme-demo", "p@demo.test", "Priya", "member", status="demo", user_id="e01")
    d2 = a.create_user("acme-demo", "r@demo.test", "Rahul", "member", status="demo", user_id="e04")
    tok, _ = a.create_session(d1)
    a.switch_demo_user(tok, "e04")
    assert a.get_session(tok)["user"]["id"] == "e04"
    with pytest.raises(ac.AccountError):
        a.authenticate("r@demo.test", "whatever-long-pw")  # demo people have no password
    real, owner = register(a)
    rt, _ = a.create_session(owner)
    with pytest.raises(ac.AccountError):
        a.switch_demo_user(rt, "e01")
    with pytest.raises(ac.AccountError):
        a.switch_demo_user(tok, owner["id"])


def test_platform_audit_is_append_only_and_has_no_secrets(a):
    org, owner = register(a)
    a.authenticate("owner@acme.test", "a-long-password")
    rows = a.s.all("SELECT type, data_json FROM platform_audit")
    assert {"company_registered", "login_succeeded"} <= {r["type"] for r in rows}
    assert "a-long-password" not in str(rows) and "code-one" not in str(rows)
    with pytest.raises(Exception):
        a.s.q("UPDATE platform_audit SET type='x'")
    with pytest.raises(Exception):
        a.s.q("DELETE FROM platform_audit")
