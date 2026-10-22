"""Sign in, register, join, sessions, CSRF, roles and visibility through the HTTP API. No network."""
import pytest

from tests import apiutil
from tests.apiutil import PW, Browser


@pytest.fixture
def main(tmp_path, monkeypatch):
    return apiutil.load_app(monkeypatch, tmp_path)


def test_registration_needs_a_code_and_is_closed_without_any(main, tmp_path, monkeypatch):
    b = Browser(main)
    assert b.c.get("/api/auth/options").json() == {"registration_open": True, "demo_available": True}
    assert b.register(code="wrong").status_code == 403
    assert b.register(email="x@acme.test", password="short").status_code == 422
    r = b.register()
    assert r.status_code == 200 and r.json()["user"]["access_role"] == "owner" and "password" not in r.text and "hash" not in r.text.lower()
    assert Browser(main).register(email="OWNER@acme.test", code="code-2").status_code == 409  # one email, one company
    closed = apiutil.load_app(monkeypatch, tmp_path / "closed", codes="")
    assert Browser(closed).c.get("/api/auth/options").json()["registration_open"] is False
    assert Browser(closed).register().status_code == 403


def test_session_cookie_flags_logout_and_forged_cookies(main):
    b = Browser(main)
    r = b.register()
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie.split(";")[1:] and "dh_session=" in cookie
    secure = Browser(main).c.post("/api/auth/login", json={"email": "owner@acme.test", "password": PW}, headers={"x-forwarded-proto": "https"})
    assert "secure" in secure.headers["set-cookie"].lower()
    assert b.get("/api/auth/me").json()["user"]["email"] == "owner@acme.test"
    token = b.c.cookies.get("dh_session")
    assert b.post("/api/auth/logout").status_code == 200
    forged = Browser(main)
    forged.c.cookies.set("dh_session", token)
    assert forged.get("/api/auth/me").status_code == 401  # the old cookie is dead after logout
    forged.c.cookies.set("dh_session", "not-a-real-token")
    assert forged.get("/api/state").status_code == 401


def test_every_state_change_needs_the_csrf_token(main):
    b = apiutil.company(main)
    ok_csrf = b.csrf
    b.csrf = None
    assert b.post("/api/ingest", {"text": "Need a Claude seat for my research project"}).status_code == 403
    b.csrf = "x" * len(ok_csrf)
    assert b.post("/api/ingest", {"text": "Need a Claude seat for my research project"}).status_code == 403
    b.csrf = ok_csrf
    assert b.post("/api/ingest", {"text": "Need a Claude seat for my research project"}).status_code == 200
    assert b.get("/api/requests").status_code == 200  # reads need no token


def test_login_is_generic_and_locks_out_after_five_failures(main):
    apiutil.company(main)
    b = Browser(main)
    msgs = {b.login("owner@acme.test", "wrong-password").json()["detail"], b.login("nobody@acme.test", "wrong-password").json()["detail"]}
    assert msgs == {"The email or password is not right."}
    for _ in range(4):
        b.login("owner@acme.test", "wrong-password")
    assert b.login("owner@acme.test", PW).status_code == 429
    assert b.login("owner@acme.test", PW).status_code == 429


def test_invite_join_flow_is_single_use_and_shows_the_link_once(main):
    owner = apiutil.company(main)
    r = owner.post("/api/people/invite", {"email": "mia@acme.test", "name": "Mia", "access_role": "member"})
    token = r.json()["join_token"]
    assert r.json()["user"]["status"] == "invited" and token not in owner.get("/api/people").text
    anon = Browser(main)
    assert anon.get(f"/api/auth/invite/{token}").json()["company"] == "Acme Ltd"
    assert anon.login("mia@acme.test", PW).status_code == 401  # cannot sign in before joining
    assert anon.c.post("/api/auth/join", json={"token": token, "password": "short"}).status_code == 422
    j = anon.c.post("/api/auth/join", json={"token": token, "password": PW})
    assert j.status_code == 200 and j.json()["user"]["name"] == "Mia" and j.json()["user"]["access_role"] == "member"
    assert Browser(main).c.post("/api/auth/join", json={"token": token, "password": PW}).status_code == 410
    assert Browser(main).get(f"/api/auth/invite/{token}").status_code == 410
    assert owner.post("/api/people/invite", {"email": "mia@acme.test", "name": "Again", "access_role": "member"}).status_code == 409
    assert owner.post("/api/people/invite", {"email": "x@acme.test", "name": "X", "access_role": "owner"}).status_code == 422


def test_members_are_refused_every_admin_route_and_it_is_audited(main):
    owner = apiutil.company(main)
    mia = apiutil.add_person(owner, "mia@acme.test", "Mia")
    for url in ("/api/people", "/api/audit", "/api/dashboard", "/api/connections", "/api/connections/token", "/api/delivery", "/api/ai"):
        assert mia.get(url).status_code == 403, url
    for url, body in (("/api/people/invite", {"email": "z@acme.test", "name": "Z"}), (f"/api/people/{owner.uid}", {"name": "Hacked"}),
                      (f"/api/people/{owner.uid}/link", {}), ("/api/admin/breaker_reset", {}), ("/api/delivery/sweep", {}), ("/api/ai", {"enabled": True})):
        assert mia.post(url, body).status_code == 403, url
    assert "unauthorized_action_refused" in [e["type"] for e in owner.engine.s.all("SELECT type FROM events")]
    assert owner.get("/api/audit").status_code == 200


def test_demo_tools_do_not_exist_for_real_companies(main):
    owner = apiutil.company(main)
    for url in ("/api/reset", "/api/chaos/burst", "/api/clock/advance", "/api/chaos/replay/1", "/api/demo/act-as"):
        assert owner.post(url, {"hours": 1, "person_id": "e01"}).status_code == 403, url
    assert owner.engine.d.emp(owner.uid) and not owner.engine.is_demo


def test_employees_see_only_their_own_and_assigned_requests(main):
    owner = apiutil.company(main)
    boss = apiutil.add_person(owner, "boss@acme.test", "Boss")
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann", manager_id=boss.uid)
    bob = apiutil.add_person(owner, "bob@acme.test", "Bob")
    rid = ann.post("/api/ingest", {"text": "I need leave tomorrow, family function"}).json()["request_ids"][0]
    assert [r["id"] for r in ann.get("/api/requests").json()] == [rid]
    assert [r["id"] for r in boss.get("/api/requests").json()] == [rid] and boss.get("/api/inbox").json()[0]["id"] == rid  # the manager from the reporting line
    assert bob.get("/api/requests").json() == [] and bob.get(f"/api/requests/{rid}").status_code == 404
    assert bob.post(f"/api/requests/{rid}/approve").status_code == 404 and bob.get("/api/requests", params={"employee": ann.uid}).status_code == 403
    assert [r["id"] for r in owner.get("/api/requests").json()] == [rid]  # admins see everything
    assert bob.get("/api/outbox").json() == [] and bob.get("/api/inbox").json() == []
    assert bob.get("/api/state").json()["employees"][0].keys() == {"id", "name", "role", "roles", "department", "manager_id", "active"}  # no emails for colleagues
    assert "email" in owner.get("/api/state").json()["employees"][0]
    assert boss.post(f"/api/requests/{rid}/approve").status_code == 200
    assert ann.get(f"/api/requests/{rid}").json()["request"]["status"] == "CLOSED"


def test_the_sender_is_always_the_signed_in_person_never_the_body(main):
    owner = apiutil.company(main)
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann")
    r = ann.post("/api/ingest", {"text": "Need a Claude seat for my research project", "sender": owner.uid, "channel": "email", "is_bot": True})
    rid = r.json()["request_ids"][0]
    req = owner.engine.req(rid)
    assert req["employee_id"] == ann.uid and req["channel"] == "chat"


def test_deactivating_a_person_signs_them_out_at_once(main):
    owner = apiutil.company(main)
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann")
    assert ann.get("/api/state").status_code == 200
    assert owner.post(f"/api/people/{ann.uid}", {"status": "disabled"}).status_code == 200
    assert ann.get("/api/state").status_code == 401 and Browser(main).login("ann@acme.test").status_code == 401
    assert owner.post(f"/api/people/{owner.uid}", {"status": "disabled"}).status_code == 422  # the owner stays


def test_people_edits_drive_routing_and_refuse_loops(main):
    owner = apiutil.company(main)
    boss = apiutil.add_person(owner, "boss@acme.test", "Boss")
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann")
    first = ann.post("/api/ingest", {"text": "I need leave tomorrow, family function", "source_event_id": "a"}).json()["request_ids"][0]
    assert owner.engine.req(first)["chain"] == [owner.uid]  # no manager yet: falls back to an admin
    assert owner.post(f"/api/people/{ann.uid}", {"manager_id": boss.uid, "department": "Sales"}).json()["manager_id"] == boss.uid
    second = ann.post("/api/ingest", {"text": "I need leave tomorrow, family function", "source_event_id": "b"}).json()["request_ids"][0]
    assert owner.engine.req(second)["chain"] == [boss.uid]
    assert owner.post(f"/api/people/{boss.uid}", {"manager_id": ann.uid}).status_code == 422  # a loop
    assert owner.post(f"/api/people/{owner.uid}", {"access_role": "member"}).status_code == 422
    link = owner.post(f"/api/people/{ann.uid}/link").json()["join_token"]
    assert Browser(main).c.post("/api/auth/join", json={"token": link, "password": "a-brand-new-password"}).status_code == 200
    assert ann.get("/api/state").status_code == 401  # the reset signed the old session out
    assert Browser(main).login("ann@acme.test", "a-brand-new-password").status_code == 200


def test_changing_your_own_password(main):
    owner = apiutil.company(main)
    assert owner.post("/api/auth/password", {"current": "wrong-password", "new": "a-brand-new-password"}).status_code == 401
    assert owner.post("/api/auth/password", {"current": PW, "new": "short"}).status_code == 422
    r = owner.post("/api/auth/password", {"current": PW, "new": "a-brand-new-password"})
    assert r.status_code == 200
    owner._adopt(r)
    assert owner.get("/api/auth/me").status_code == 200  # this browser stays signed in
    assert Browser(main).login("owner@acme.test", PW).status_code == 401 and Browser(main).login("owner@acme.test", "a-brand-new-password").status_code == 200


def test_security_headers_and_no_api_caching(main):
    b = Browser(main)
    r = b.get("/api/auth/options")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert b.get("/docs").status_code == 404 and b.get("/openapi.json").status_code == 404



def test_a_refused_session_is_logged_with_the_reason_and_never_the_cookie(main, caplog):
    import logging
    b = apiutil.company(main)
    token = b.c.cookies.get("dh_session")
    assert b.get("/api/state").status_code == 200
    b.post("/api/auth/logout")
    ghost = Browser(main)
    ghost.c.cookies.set("dh_session", token)  # the cookie of a session that no longer exists
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        assert ghost.get("/api/state").status_code == 401
        assert Browser(main).get("/api/state").status_code == 401  # no cookie at all: still recorded, so a browser that stops sending it shows up
    lines = [r.getMessage() for r in caplog.records if "session refused" in r.getMessage()]
    assert lines == ["session refused: unknown_session on GET /api/state", "session refused: no_cookie on GET /api/state"] and token not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        assert Browser(main).get("/api/auth/me").status_code == 401  # a signed-out visitor's normal first call is not news
    assert "session refused" not in caplog.text


def test_parallel_requests_after_sign_in_and_act_as_all_succeed(main):
    """What the browser does: several GETs at once right after entering the demo and after switching person."""
    from concurrent.futures import ThreadPoolExecutor
    demo = Browser(main)
    assert demo.demo("e01").status_code == 200
    urls = ["/api/state", "/api/clock", "/api/inbox", "/api/outbox?limit=200", "/api/requests?employee=e01"]
    for round_ in range(15):
        with ThreadPoolExecutor(5) as ex:
            codes = list(ex.map(lambda u: demo.get(u).status_code, urls))
        assert codes == [200] * 5, (round_, codes)
        who = ["e10", "e04", "e01"][round_ % 3]
        assert demo.act_as(who).status_code == 200
        urls[4] = f"/api/requests?employee={who}"
