"""The optional shared password for a private demo deployment, and the fact that data is closed by login either way. No network."""
import base64

import pytest

from tests import apiutil

PW = "correct horse battery staple 42"


def basic(pw, user="demo"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def make(tmp_path, monkeypatch):
    def build(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        main = apiutil.load_app(monkeypatch, tmp_path)
        return apiutil.Browser(main).c, main
    return build


ROUTES = ["/static/app.css", "/static/app.js", "/static/fonts/AtkinsonHyperlegibleNext-latin.woff2", "/", "/api/auth/options", "/api/state", "/api/dashboard",
          "/api/outbox", "/api/requests", "/api/audit", "/api/clock", "/api/connections", "/api/connections/token", "/api/delivery", "/docs", "/openapi.json"]


def test_with_a_shared_password_every_route_needs_it_and_leaks_nothing(make):
    c, _ = make(DESKHAND_DEMO_PASSWORD=PW)
    for r in ROUTES:
        resp = c.get(r)
        assert resp.status_code == 401, r
        assert resp.text == "Password required." and "WWW-Authenticate" in resp.headers
    for bad in ("", "wrong", PW + "x"):
        assert c.get("/api/auth/options", headers=basic(bad)).status_code == 401
    assert c.post("/api/auth/login", json={"email": "a@b.co", "password": "x"}).status_code == 401
    assert c.post("/api/demo/enter").status_code == 401 and c.post("/api/reset").status_code == 401


def test_the_right_password_opens_the_shell_any_user_name_and_colons_in_it(make):
    c, _ = make(DESKHAND_DEMO_PASSWORD="a:b:c")
    assert c.get("/api/auth/options", headers=basic("a:b:c", user="anyone")).status_code == 200
    assert c.get("/", headers=basic("a:b:c")).status_code == 200
    assert c.get("/api/state", headers=basic("a:b:c")).status_code == 401  # the shared password is not a login


def test_without_a_shared_password_the_shell_is_public_but_all_data_needs_a_login(make):
    c, _ = make(APP_NAME="deskhand")  # hosted, no shared password: closed by login, not by a blanket lock
    assert c.get("/").status_code == 200 and c.get("/api/auth/options").status_code == 200 and c.get("/static/app.js").status_code == 200
    for r in ("/api/state", "/api/dashboard", "/api/outbox", "/api/requests", "/api/audit", "/api/clock", "/api/connections", "/api/people", "/api/inbox", "/api/digest", "/api/auth/me"):
        resp = c.get(r)
        assert resp.status_code == 401 and resp.json()["detail"] == "Sign in to continue.", r
    for r in ("/api/ingest", "/api/reset", "/api/chaos/burst", "/api/clock/advance", "/api/people/invite", "/api/requests/1/approve"):
        assert c.post(r, json={}).status_code == 401, r


def test_local_development_stays_open_by_default(make):
    c, _ = make()
    assert c.get("/api/auth/options").status_code == 200


def test_brute_force_brake(make):
    c, main = make(DESKHAND_DEMO_PASSWORD=PW)
    main._gate_failures.clear()
    for i in range(20):
        assert c.get("/api/auth/options", headers=basic(f"guess{i}")).status_code == 401
    assert c.get("/api/auth/options", headers=basic("guess")).status_code == 429
    assert c.get("/api/auth/options", headers=basic(PW)).status_code == 429  # even the right one waits out the minute
    main._gate_failures.clear()
    assert c.get("/api/auth/options", headers=basic(PW)).status_code == 200


def test_static_files_are_behind_the_gate_and_served_with_it(make):
    c, _ = make(DESKHAND_DEMO_PASSWORD=PW)
    for r in ("/static/app.css", "/static/app.js", "/static/fonts/AtkinsonHyperlegibleNext-latin.woff2", "/static/fonts/OFL.txt"):
        assert c.get(r).status_code == 401
        assert c.get(r, headers=basic(PW)).status_code == 200


def test_a_gate_refusal_of_a_signed_in_browser_is_logged_without_the_password(monkeypatch, tmp_path, caplog):
    import logging
    from tests import apiutil
    main = apiutil.load_app(monkeypatch, tmp_path)
    b = apiutil.company(main)
    monkeypatch.setenv("DESKHAND_DEMO_PASSWORD", "gate-secret-for-tests")
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        assert b.get("/api/state").status_code == 401  # has the session cookie but no password
        assert b.c.get("/api/state", headers={"authorization": "Basic " + __import__("base64").b64encode(b"x:wrong-one").decode()}).status_code == 401
        assert apiutil.Browser(main).get("/api/state").status_code == 401  # a stranger with no cookie and no header: not logged
    lines = [r.getMessage() for r in caplog.records if "gate refused" in r.getMessage()]
    assert lines == ["gate refused GET /api/state: no_authorization_header", "gate refused GET /api/state: wrong_password"]
    assert "gate-secret-for-tests" not in caplog.text and "wrong-one" not in caplog.text
