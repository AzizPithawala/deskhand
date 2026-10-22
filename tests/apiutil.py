"""Helpers for API tests: a browser-like client with its own cookie jar and CSRF token, and one-call company setup."""
import importlib

from fastapi.testclient import TestClient

PW = "a-long-password"


def load_app(monkeypatch, tmp_path, codes="code-1,code-2,code-3,code-4"):
    monkeypatch.setenv("DESKHAND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DESKHAND_SIGNUP_CODES", codes)
    monkeypatch.setenv("DESKHAND_DELIVERY_WORKER", "off")
    from app import main
    importlib.reload(main)
    return main


class Browser:
    def __init__(self, main):
        self.main, self.c, self.csrf, self.me = main, TestClient(main.app), None, None

    # state-changing calls carry the CSRF token the page learned when it signed in
    def post(self, url, json=None, **kw):
        return self.c.post(url, json=json, headers={"X-CSRF-Token": self.csrf} if self.csrf else {}, **kw)

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def _adopt(self, r):
        if r.status_code == 200 and isinstance(r.json(), dict) and "csrf" in r.json():
            self.csrf, self.me = r.json()["csrf"], r.json()
        return r

    @property
    def uid(self):
        return self.me["user"]["id"]

    @property
    def org_id(self):
        return self.me["org"]["id"]

    @property
    def engine(self):
        return self.main.registry.engine_for(self.org_id)

    def register(self, company="Acme Ltd", name="Olivia Owner", email="owner@acme.test", password=PW, code="code-1"):
        return self._adopt(self.c.post("/api/auth/register", json={"company": company, "name": name, "email": email, "password": password, "code": code, "timezone": "UTC"}))

    def login(self, email, password=PW):
        return self._adopt(self.c.post("/api/auth/login", json={"email": email, "password": password}))

    def demo(self, person="e01"):
        return self._adopt(self.c.post("/api/demo/enter", json={"person_id": person}))

    def act_as(self, person):
        r = self.post("/api/demo/act-as", {"person_id": person})
        if r.status_code == 200:
            self.me = r.json()
        return r


def company(main, email="owner@acme.test", code="code-1", name="Acme Ltd"):
    b = Browser(main)
    assert b.register(name, "Owner of " + name, email, PW, code).status_code == 200
    return b


def add_person(owner, email, name, role="member", manager_id=None, department=None):
    """Invite, then join with a password: returns a signed-in Browser for the new person."""
    r = owner.post("/api/people/invite", {"email": email, "name": name, "access_role": role, "manager_id": manager_id, "department": department})
    assert r.status_code == 200, r.text
    b = Browser(owner.main)
    j = b.c.post("/api/auth/join", json={"token": r.json()["join_token"], "password": PW})
    assert j.status_code == 200, j.text
    b._adopt(j)
    return b
