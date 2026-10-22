"""The admin's delivery log through the HTTP API: admin only, metadata only (never what a message said), filters, totals, paging, retry, isolation. No network."""
import json
import time

import pytest

from tests import apiutil
from tests.apiutil import Browser
from tests.test_notifications_api import ask, put
from tests.test_viasocket_adapter import SCRIPT, SECRET, env  # noqa: F401 - env is an autouse fixture


@pytest.fixture
def main(tmp_path, monkeypatch):
    return apiutil.load_app(monkeypatch, tmp_path)


@pytest.fixture
def team(main):
    owner = apiutil.company(main)
    boss = apiutil.add_person(owner, "boss@acme.test", "Bo Boss")
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee", manager_id=boss.uid)
    bob = apiutil.add_person(owner, "bob@acme.test", "Bob Employee", manager_id=boss.uid)
    return owner, boss, ann, bob


def log(b, **q):
    r = b.get("/api/delivery/log", params=q)
    assert r.status_code == 200, r.text
    return r.json()


def ids(b, **q):
    return [i["id"] for i in log(b, **q)["items"]]


SECRET_TEXT = "my salary details and the tax number 99-1234"


@pytest.fixture
def mixed(team):
    """Known rows: two people, both channels, every status, different ages."""
    owner, boss, ann, bob = team
    t0 = time.time() - 5 * 86400
    leave = ask(ann, "I need Friday and Monday off, family function.")
    s = owner.engine.s
    s.q("DELETE FROM outbox")  # start from exactly the rows below
    a = put(owner, leave, boss.uid, "approval_request", SECRET_TEXT, n=1, at=t0)  # chat + email rows
    b = put(owner, leave, boss.uid, "reminder", SECRET_TEXT, at=t0 + 86400)
    c = put(owner, leave, ann.uid, "approved", SECRET_TEXT, at=t0 + 2 * 86400)
    d = put(owner, leave, owner.uid, "parked", SECRET_TEXT, at=t0 + 3 * 86400)
    mail = {k: s.one("SELECT id FROM outbox WHERE idempotency_key=?", (f"{leave}:{k}:{p}:{n}:email",))["id"] for k, p, n in (("approval_request", boss.uid, 1), ("reminder", boss.uid, 0), ("approved", ann.uid, 0), ("parked", owner.uid, 0))}
    s.q("UPDATE outbox SET delivery_status='delivered', delivered_at=?, delivery_attempts=1, sent_to=? WHERE id=?", (t0, owner.uid, a))
    s.q("UPDATE outbox SET delivery_status='failed', delivery_attempts=3, delivery_error=? WHERE id=?", (f"Slack said no {SECRET} for {SCRIPT}", b))
    s.q("UPDATE outbox SET delivery_status='failed', delivery_attempts=4, delivery_error='Gmail refused' WHERE id=?", (mail["reminder"],))
    s.q("UPDATE outbox SET delivery_status='pending' WHERE id=?", (c,))
    s.q("UPDATE outbox SET delivery_status='delivered', delivered_at=?, attempts=0 WHERE id=?".replace(", attempts=0", ""), (t0, mail["approved"]))
    return owner, boss, ann, bob, leave, dict(a=a, b=b, c=c, d=d, **{"m_" + k: v for k, v in mail.items()}), t0


# ---------------- who may see it, and what it never contains ----------------
def test_only_an_admin_can_open_the_log(team):
    owner, boss, ann, bob = team
    assert ann.get("/api/delivery/log").status_code == 403 and boss.get("/api/delivery/log").status_code == 403
    assert Browser(owner.main).get("/api/delivery/log").status_code == 401
    owner.post(f"/api/people/{boss.uid}", {"access_role": "admin"})
    assert boss.get("/api/delivery/log").status_code == 401  # a changed role ends the person's sessions
    assert boss.login("boss@acme.test").status_code == 200 and log(boss)["items"] == []  # signed in again, now an admin: allowed


def test_the_log_never_contains_what_a_message_said_or_its_subject(mixed):
    owner, boss, ann, bob, leave, n, t0 = mixed
    j = log(owner, limit=200)
    assert len(j["items"]) == 8
    blob = json.dumps(j)
    for text in (SECRET_TEXT, "salary", "99-1234", "Approval needed", "Hi Bo", "Deskhand"):
        assert text not in blob
    allowed = {"id", "request_id", "type_name", "kind", "category", "channel", "to", "from", "sent_to", "status", "attempts", "error", "note", "next_attempt_at", "delivered_at", "created_at", "can_retry"}
    assert all(set(i) == allowed for i in j["items"])  # no body, text or subject field exists at all
    assert all(set(i["to"]) == {"id", "name"} for i in j["items"])


def test_errors_are_redacted_before_an_admin_sees_them(mixed):
    owner, boss, ann, bob, leave, n, t0 = mixed
    blob = json.dumps(log(owner))
    assert SECRET not in blob and SCRIPT not in blob
    row = next(i for i in log(owner)["items"] if i["id"] == n["b"])
    assert row["error"].startswith("Slack said no") and "[REDACTED]" in row["error"] and row["can_retry"] is True and row["attempts"] == 3


# ---------------- what each row says ----------------
def test_a_row_says_who_it_was_for_who_really_got_it_and_how_it_ended(mixed):
    owner, boss, ann, bob, leave, n, t0 = mixed
    by = {i["id"]: i for i in log(owner)["items"]}
    a = by[n["a"]]
    assert (a["channel"], a["kind"], a["category"], a["status"], a["attempts"], a["type_name"], a["request_id"]) == ("slack", "approval_request", "decide", "delivered", 1, "Leave", leave)
    assert a["to"] == {"id": boss.uid, "name": "Bo Boss"} and a["sent_to"] == owner.me["user"]["name"]  # rehearsal: the admin got it, not the approver
    assert by[n["m_reminder"]]["channel"] == "email" and by[n["m_reminder"]]["error"] == "Gmail refused"
    assert by[n["c"]]["status"] == "pending" and by[n["c"]]["can_retry"] is False and by[n["c"]]["to"]["name"] == "Ann Employee"


# ---------------- filters and totals ----------------
def test_each_filter_on_its_own_and_combined(mixed):
    owner, boss, ann, bob, leave, n, t0 = mixed
    every = ids(owner, limit=200)
    assert every == sorted(every, reverse=True) and len(every) == 8
    assert ids(owner, status="failed") == [n["m_reminder"], n["b"]] and ids(owner, status="pending") == [n["c"]]
    assert set(ids(owner, status="delivered")) == {n["a"], n["m_approved"]} and ids(owner, status="sending") == []
    assert set(ids(owner, channel="email")) == {n["m_approval_request"], n["m_reminder"], n["m_approved"], n["m_parked"]} and n["a"] in ids(owner, channel="slack")
    assert set(ids(owner, person=boss.uid)) == {n["a"], n["b"], n["m_approval_request"], n["m_reminder"]} and ids(owner, person=bob.uid) == []
    assert ids(owner, kind="parked") == [n["m_parked"], n["d"]] and set(ids(owner, kind="approved")) == {n["c"], n["m_approved"]}
    assert len(ids(owner, request_id=leave)) == 8 and ids(owner, request_id=999) == []
    assert ids(owner, since=t0 + 3 * 86400 - 1) == [n["m_parked"], n["d"]] and set(ids(owner, until=t0 + 86400)) == {n["a"], n["m_approval_request"]}  # since inclusive, until exclusive
    assert ids(owner, status="failed", channel="email") == [n["m_reminder"]] and ids(owner, status="failed", person=ann.uid) == []
    assert ids(owner, status="failed", channel="slack", kind="reminder", person=boss.uid, request_id=leave) == [n["b"]]


def test_the_log_lists_the_kinds_that_exist_so_the_filter_offers_only_real_choices(mixed):
    assert log(mixed[0])["kinds"] == ["approval_request", "approved", "parked", "reminder"]


def test_the_totals_ignore_the_status_and_the_page_so_the_chips_show_what_each_would_find(mixed):
    owner, boss, ann, bob, leave, n, t0 = mixed
    assert log(owner)["counts"] == {"pending": 1, "sending": 0, "delivered": 2, "failed": 2, "skipped": 3}
    j = log(owner, status="failed", limit=1)
    assert len(j["items"]) == 1 and j["counts"]["delivered"] == 2 and j["counts"]["failed"] == 2  # filtered to failed, one per page: totals unchanged
    assert log(owner, channel="email")["counts"] == {"pending": 0, "sending": 0, "delivered": 1, "failed": 1, "skipped": 2}
    assert log(owner, person=bob.uid)["counts"] == {k: 0 for k in ("pending", "sending", "delivered", "failed", "skipped")}


def test_bad_values_are_refused_and_odd_ones_only_match_nothing(mixed):
    owner, boss, ann, bob, leave, n, t0 = mixed
    assert owner.get("/api/delivery/log", params={"status": "maybe"}).status_code == 422
    assert owner.get("/api/delivery/log", params={"channel": "pigeon"}).status_code == 422
    assert owner.get("/api/delivery/log", params={"since": "yesterday"}).status_code == 422
    assert owner.get("/api/delivery/log", params={"request_id": "x"}).status_code == 422
    for evil in ("' OR 1=1 --", "x'; DROP TABLE outbox; --"):
        assert log(owner, kind=evil)["items"] == [] and log(owner, person=evil)["items"] == []
    assert owner.engine.s.one("SELECT COUNT(*) c FROM outbox")["c"] > 0


def test_paging_covers_every_row_once_and_limits_are_clamped(mixed):
    owner, boss, ann, bob, leave, n, t0 = mixed
    seen, page = [], log(owner, limit=3)
    while True:
        seen += [i["id"] for i in page["items"]]
        if not page["next_before"]:
            break
        put(owner, leave, boss.uid, "escalation_1", n=len(seen))  # new rows arriving mid-walk do not shift later pages
        page = log(owner, limit=3, before=page["next_before"])
    assert len(seen) == 8 == len(set(seen)) and seen == sorted(seen, reverse=True)
    assert len(log(owner, limit=0)["items"]) == 1 and len(log(owner, limit=10 ** 6)["items"]) <= 200


# ---------------- retry (the existing action, reused) ----------------
def test_retry_works_from_a_failed_row_only_for_admins_and_is_audited(mixed):
    owner, boss, ann, bob, leave, n, t0 = mixed
    assert boss.post(f"/api/delivery/retry/{n['b']}").status_code == 403
    assert owner.post(f"/api/delivery/retry/{n['a']}").status_code == 409  # delivered: nothing to retry
    assert owner.post(f"/api/delivery/retry/{n['b']}").status_code == 200
    ev = [json.loads(r["data_json"]) for r in owner.engine.s.all("SELECT data_json FROM events WHERE type='delivery_replayed'")]
    assert ev == [{"outbox": n["b"]}]
    row = next(i for i in log(owner)["items"] if i["id"] == n["b"])
    assert row["status"] != "failed" or row["attempts"] > 3  # it went back into the queue (and may have been tried again at once)
    assert row["can_retry"] is (row["status"] == "failed")


# ---------------- isolation and the demo ----------------
def test_one_company_never_sees_another_companys_log(main, mixed):
    owner = mixed[0]
    other = apiutil.company(main, "o@two.test", "code-2", "Two Ltd")
    j = log(other)
    assert j["items"] == [] and sum(j["counts"].values()) == 0
    assert other.get(f"/api/delivery/log?person={mixed[1].uid}").json()["items"] == []
    assert other.post(f"/api/delivery/retry/{mixed[5]['b']}").status_code == 409  # that id means nothing there (and is not theirs)
    assert len([i for i in log(owner, limit=200)["items"] if i["request_id"] == 1]) == 8  # nothing of theirs was touched


def test_the_demo_admin_can_read_the_log_and_it_has_no_text_either(main):
    d = Browser(main)
    assert d.demo("e10").status_code == 200
    d.post("/api/ingest", {"text": "Need a Claude seat for my research project"})
    j = log(d)
    assert j["items"] and all(i["status"] == "skipped" for i in j["items"])
    assert "Claude seat" not in json.dumps(j)
