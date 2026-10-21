"""The hosted demo is behind a password gate on every route, and fails closed. No network."""
import base64
import importlib

import pytest
from fastapi.testclient import TestClient

PW = "correct horse battery staple 42"


def basic(pw, user="demo"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKHAND_DB", str(tmp_path / "t.db"))

    def build(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from app import main
        importlib.reload(main)
        return TestClient(main.app)
    return build


ROUTES = ["/static/app.css", "/static/app.js", "/static/fonts/Geist-Variable.woff2", "/", "/api/state", "/api/dashboard", "/api/outbox", "/api/requests", "/api/audit", "/api/clock", "/api/connections?actor_id=e10",
          "/api/connections/token?actor_id=e10", "/api/delivery?actor_id=e10", "/docs", "/openapi.json"]


def test_every_route_needs_the_password_and_leaks_nothing(make):
    c = make(DESKHAND_DEMO_PASSWORD=PW)
    for r in ROUTES:
        resp = c.get(r)
        assert resp.status_code == 401, r
        assert resp.text == "Password required." and "WWW-Authenticate" in resp.headers
    for bad in ("", "wrong", PW + "x"):
        assert c.get("/api/state", headers=basic(bad)).status_code == 401
    assert c.post("/api/reset").status_code == 401 and c.post("/api/delivery/toggle", json={"actor_id": "e10", "on": True}).status_code == 401


def test_right_password_works_with_any_user_name_and_colons_in_the_password(make):
    c = make(DESKHAND_DEMO_PASSWORD="a:b:c")
    assert c.get("/api/state", headers=basic("a:b:c", user="anyone")).status_code == 200
    assert c.get("/", headers=basic("a:b:c")).status_code == 200


def test_hosted_without_a_password_fails_closed(make):
    c = make(APP_NAME="deskhand")  # Embarko sets APP_NAME; forgetting the password must never leave the app public
    for r in ROUTES:
        assert c.get(r).status_code == 503
    assert c.get("/api/state", headers=basic("")).status_code == 503


def test_local_development_stays_open_by_default(make):
    assert make().get("/api/state").status_code == 200


def test_brute_force_brake(make):
    c = make(DESKHAND_DEMO_PASSWORD=PW)
    from app import main
    main._gate_failures.clear()
    for i in range(20):
        assert c.get("/api/state", headers=basic(f"guess{i}")).status_code == 401
    assert c.get("/api/state", headers=basic("guess")).status_code == 429
    assert c.get("/api/state", headers=basic(PW)).status_code == 429  # even the right one waits out the minute
    main._gate_failures.clear()
    assert c.get("/api/state", headers=basic(PW)).status_code == 200


def test_static_files_are_behind_the_gate_and_served_with_it(make):
    c = make(DESKHAND_DEMO_PASSWORD=PW)
    for r in ("/static/app.css", "/static/app.js", "/static/fonts/Geist-Variable.woff2", "/static/fonts/OFL.txt"):
        assert c.get(r).status_code == 401
        assert c.get(r, headers=basic(PW)).status_code == 200
