"""My leave, the leave allowance and admin corrections through the HTTP API. Own data only for people, admin-only for the rest, audited, isolated. No network."""
import json

import pytest

from tests import apiutil
from tests.apiutil import Browser


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


def test_everyone_sees_only_their_own_balance_and_history(team):
    owner, boss, ann, bob = team
    me = ann.get("/api/leave/me").json()
    assert me["demo"] is False and [(t["code"], t["balance"], t["yearly_days"]) for t in me["types"]] == [("casual", 12, 12), ("sick", 8, 8)] and [e["kind"] for e in me["entries"]] == ["grant", "grant"]
    rid = ann.post("/api/ingest", {"text": "I need Friday and Monday off, family function.", "source_event_id": "l1"}).json()["request_ids"][0]
    assert boss.post(f"/api/requests/{rid}/approve").status_code == 200
    me = ann.get("/api/leave/me").json()
    assert {t["code"]: t["balance"] for t in me["types"]} == {"casual": 10, "sick": 8}
    used = [e for e in me["entries"] if e["kind"] == "use"][0]
    assert used["days"] == -2 and used["request_id"] == rid and len(used["dates"]) == 2 and used["type_name"] == "Casual leave" and used["by"] == "Deskhand"
    assert {t["code"]: t["balance"] for t in bob.get("/api/leave/me").json()["types"]} == {"casual": 12, "sick": 8}  # Bob is untouched


def test_the_state_says_what_the_leave_record_is_called(team, main):
    owner, boss, ann, bob = team
    s = ann.get("/api/state").json()
    assert s["hr_label"] == "the leave record" and s["hr_applied"] == "recorded in your leave balance"
    demo = Browser(main)
    demo.demo("e01")
    assert demo.get("/api/state").json()["hr_label"] == "the HR system"


def test_only_admins_manage_allowances_and_corrections_and_members_are_refused(team):
    owner, boss, ann, bob = team
    for method, url, body in (("get", "/api/setup/leave", None), ("post", "/api/setup/leave/casual", {"yearly_days": 20}), ("post", "/api/leave/adjust", {"person_id": ann.uid, "leave_type": "casual", "days": 1, "note": "extra"}),
                              ("get", f"/api/leave/people/{ann.uid}", None)):
        r = bob.get(url) if method == "get" else bob.post(url, body)
        assert r.status_code == 403, url
    good = owner.csrf
    owner.csrf = None
    assert owner.post("/api/leave/adjust", {"person_id": ann.uid, "leave_type": "casual", "days": 1, "note": "extra"}).status_code == 403  # no CSRF token
    owner.csrf = good
    assert Browser(owner.main).get("/api/leave/me").status_code == 401


def test_an_admin_adjusts_a_balance_with_a_reason_and_it_is_recorded_and_audited(team):
    owner, boss, ann, bob = team
    r = owner.post("/api/leave/adjust", {"person_id": ann.uid, "leave_type": "casual", "days": 3, "note": "joined mid-year,  extra days agreed"})
    assert r.status_code == 200
    people = {p["name"]: p["balances"] for p in r.json()["people"]}
    assert people["Ann Employee"] == {"casual": 15, "sick": 8} and people["Bob Employee"] == {"casual": 12, "sick": 8}
    hist = owner.get(f"/api/leave/people/{ann.uid}").json()
    adj = [e for e in hist["entries"] if e["kind"] == "adjust"][0]
    assert adj["days"] == 3 and adj["note"] == "joined mid-year, extra days agreed" and adj["by"] == "Owner of Acme Ltd" and hist["person"] == "Ann Employee"
    ev = json.loads(owner.engine.s.all("SELECT data_json FROM events WHERE type='leave_adjusted'")[-1]["data_json"])
    assert ev == {"person": ann.uid, "type": "casual", "days": 3, "note": "joined mid-year, extra days agreed", "balance": 15}
    assert {t["code"]: t["balance"] for t in ann.get("/api/leave/me").json()["types"]}["casual"] == 15  # and Ann sees it


@pytest.mark.parametrize("body", [{"person_id": "nobody", "leave_type": "casual", "days": 1, "note": "fine reason"}, {"person_id": "{ann}", "leave_type": "nope", "days": 1, "note": "fine reason"},
                                  {"person_id": "{ann}", "leave_type": "casual", "days": 0, "note": "fine reason"}, {"person_id": "{ann}", "leave_type": "casual", "days": 0.3, "note": "fine reason"},
                                  {"person_id": "{ann}", "leave_type": "casual", "days": 999, "note": "fine reason"}, {"person_id": "{ann}", "leave_type": "casual", "days": 1, "note": ""},
                                  {"person_id": "{ann}", "leave_type": "casual", "days": 1, "note": "x" * 201}, {"person_id": "{ann}", "leave_type": "casual", "days": 1, "note": "ab"}])
def test_bad_corrections_are_refused_and_leave_no_trace(team, body):
    owner, boss, ann, bob = team
    body = {k: (v.replace("{ann}", ann.uid) if isinstance(v, str) else v) for k, v in body.items()}
    before = owner.engine.s.all("SELECT id FROM leave_ledger")
    assert owner.post("/api/leave/adjust", body).status_code == 422
    assert owner.engine.s.all("SELECT id FROM leave_ledger") == before and not owner.engine.s.all("SELECT * FROM events WHERE type='leave_adjusted'")


def test_the_yearly_allowance_is_validated_audited_and_applies_to_people_not_yet_granted(team):
    owner, boss, ann, bob = team
    assert ann.get("/api/leave/me").json()["types"][0]["balance"] == 12  # Ann's grant for this year exists now
    r = owner.post("/api/setup/leave/casual", {"yearly_days": 15})
    assert r.status_code == 200 and r.json()["types"][0]["yearly_days"] == 15
    assert ann.get("/api/leave/me").json()["types"][0]["balance"] == 12 and bob.get("/api/leave/me").json()["types"][0]["balance"] == 15
    ev = json.loads(owner.engine.s.all("SELECT data_json FROM events WHERE type='setup_changed'")[-1]["data_json"])
    assert ev == {"area": "leave", "code": "casual", "changes": {"yearly_days": [12, 15]}}
    for bad in (-1, 400, 1.3):
        assert owner.post("/api/setup/leave/casual", {"yearly_days": bad}).status_code == 422
    assert owner.post("/api/setup/leave/bogus", {"yearly_days": 5}).status_code == 422


def test_the_demo_company_can_be_read_but_has_no_ledger_to_change(main):
    demo = Browser(main)
    demo.demo("e10")
    me = demo.get("/api/leave/me").json()
    assert me["demo"] is True and me["entries"] == [] and me["types"]
    assert demo.get("/api/setup/leave").json() == {"demo": True, "types": [], "people": []}
    assert demo.post("/api/leave/adjust", {"person_id": "e01", "leave_type": "casual", "days": 1, "note": "demo note"}).status_code == 403
    assert demo.post("/api/setup/leave/casual", {"yearly_days": 20}).status_code == 403


def test_one_company_cannot_adjust_or_read_another_companys_leave(main, team):
    owner, boss, ann, bob = team
    other = apiutil.company(main, "o@else.test", "code-2", "Else Co")
    ed = apiutil.add_person(other, "ed@else.test", "Ed Else")
    assert other.post("/api/leave/adjust", {"person_id": ann.uid, "leave_type": "casual", "days": 5, "note": "not mine to change"}).status_code == 422
    assert other.get(f"/api/leave/people/{ann.uid}").status_code == 404
    assert "Ann" not in json.dumps(other.get("/api/setup/leave").json()) and {p["name"] for p in other.get("/api/setup/leave").json()["people"]} == {"Owner of Else Co", "Ed Else"}
    owner.post("/api/setup/leave/casual", {"yearly_days": 30})
    assert ed.get("/api/leave/me").json()["types"][0]["yearly_days"] == 12
