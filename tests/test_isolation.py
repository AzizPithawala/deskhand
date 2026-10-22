"""Two companies side by side: nothing one does or knows is reachable from the other. And the demo company is fenced off. No network."""
import json

import pytest

from tests import apiutil
from tests.apiutil import Browser


@pytest.fixture
def two(tmp_path, monkeypatch):
    main = apiutil.load_app(monkeypatch, tmp_path)
    a = apiutil.company(main, "a@one.test", "code-1", "One Ltd")
    b = apiutil.company(main, "b@two.test", "code-2", "Two Ltd")
    a_emp = apiutil.add_person(a, "ann@one.test", "Ann One")
    b_emp = apiutil.add_person(b, "bob@two.test", "Bob Two")
    return main, a, b, a_emp, b_emp


def test_each_company_has_separate_data_and_the_same_request_number_means_different_things(two):
    main, a, b, a_emp, b_emp = two
    ra = a_emp.post("/api/ingest", {"text": "Need a Claude seat for my research project"}).json()["request_ids"][0]
    rb = b_emp.post("/api/ingest", {"text": "I need a second monitor for my desk."}).json()["request_ids"][0]
    assert ra == rb == 1
    assert a.get("/api/requests/1").json()["request"]["type_code"] == "software_seat"
    assert b.get("/api/requests/1").json()["request"]["type_code"] == "equipment"
    assert a_emp.get("/api/requests/1").json()["request"]["employee_id"] == a_emp.uid
    assert b_emp.get("/api/requests/1").json()["request"]["employee_id"] == b_emp.uid


def test_one_company_cannot_read_or_touch_anything_of_another(two):
    main, a, b, a_emp, b_emp = two
    a_emp.post("/api/ingest", {"text": "Need a Claude seat for my research project"})
    b_emp.post("/api/ingest", {"text": "I need a second monitor for my desk."})
    blob = json.dumps([a.get("/api/people").json(), a.get("/api/audit").json(), a.get("/api/requests").json(), a_emp.get("/api/outbox").json(), a.get("/api/state").json(),
                       a.get("/api/dashboard").json(), a.get("/api/connections").json()])
    for secret in ("b@two.test", "bob@two.test", "Bob Two", "Two Ltd", b.uid, b_emp.uid):
        assert secret not in blob
    assert a.post(f"/api/people/{b_emp.uid}", {"name": "Hacked"}).status_code == 404
    assert a.post(f"/api/people/{b_emp.uid}/link").status_code == 404
    assert a.post("/api/people/invite", {"email": "n@one.test", "name": "N", "manager_id": b.uid}).status_code == 404
    assert a.post(f"/api/people/{ann_id(a_emp)}", {"manager_id": b_emp.uid}).status_code == 404
    assert a.post("/api/people/invite", {"email": "bob@two.test", "name": "Dup"}).status_code == 409  # emails are unique across companies
    assert b.get("/api/people").json()[0]["email"] == "b@two.test" and "ann@one.test" not in json.dumps(b.get("/api/people").json())


def ann_id(browser):
    return browser.uid


def test_each_company_has_its_own_connections_ai_switch_and_engine(two):
    main, a, b, a_emp, b_emp = two
    from app import viasocket_adapter as vs
    vs.ConnectionStore(a.engine.s, a.org_id).save("slack", "auth-slack-aaaa11", "scriAAAAAAAAAA")
    assert vs.ConnectionStore(b.engine.s, b.org_id).get("slack") is None
    assert a.engine is not b.engine and a.engine.s.path != b.engine.s.path and a.engine.live.tenant == a.org_id != b.engine.live.tenant


def test_demo_company_is_separate_fenced_and_has_no_way_into_real_ones(two):
    main, a, b, a_emp, b_emp = two
    demo = Browser(main)
    assert demo.demo("e10").status_code == 200 and demo.me["org"]["is_demo"] is True
    assert demo.post("/api/people/invite", {"email": "z@demo.test", "name": "Z"}).status_code == 403
    assert demo.get("/api/connections").json()["configured"] is False and demo.get("/api/connections/token").status_code == 403
    assert demo.post("/api/connections/connected", {"app": "slack", "auth_id": "x"}).status_code == 403
    assert demo.post("/api/auth/password", {"current": "x", "new": "a-brand-new-password"}).status_code == 403
    assert demo.act_as(a.uid).status_code == 404 and demo.act_as(a_emp.uid).status_code == 404  # no hopping into a real person
    assert Browser(main).demo(a.uid).status_code == 404  # the demo button cannot sign in as a real person
    assert a.act_as("e01").status_code == 403 and a.post("/api/reset").status_code == 403
    demo.post("/api/ingest", {"text": "Need a Claude seat for my research project"})
    assert a.get("/api/requests").json() == [] and a.engine.s.path != demo.engine.s.path
    assert demo.get("/api/clock").json()["banners"] == []  # no "Slack not connected" noise in the demo
    assert demo.demo("e01").status_code == 200 and demo.act_as("e04").json()["user"]["id"] == "e04"


def test_the_demo_cannot_log_in_with_a_password_and_has_no_hash(two):
    main, *_ = two
    assert Browser(main).login("priya.sharma@acme-demo.example", "anything-long-1").status_code == 401
    assert main.accounts.get_user("e01")["password_hash"] is None


def test_chaos_in_the_demo_never_touches_real_companies_and_never_sends(two):
    main, a, b, a_emp, b_emp = two
    demo = Browser(main)
    demo.demo("e10")
    for name in ("duplicate_x4", "burst", "hr_down_park", "ai_budget"):
        assert demo.post(f"/api/chaos/{name}").status_code == 200
    assert a.get("/api/requests").json() == [] and b.get("/api/dashboard").json()["total"] == 0
    assert not demo.engine.s.all("SELECT * FROM outbox WHERE delivery_status='pending'")
