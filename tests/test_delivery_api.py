"""The delivery mode, Slack matching and each person's own mailbox through the HTTP API. Admin-only where it should be, audited, isolated. No network."""
import json

import pytest

from app import viasocket_adapter as vs
from tests import apiutil
from tests.apiutil import Browser
from tests.test_viasocket_adapter import AUTH, SCRIPT, SECRET, FakeTransport, env, ok  # noqa: F401 - env is an autouse fixture


@pytest.fixture
def main(tmp_path, monkeypatch):
    return apiutil.load_app(monkeypatch, tmp_path)


@pytest.fixture
def team(main):
    owner = apiutil.company(main)
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee")
    bob = apiutil.add_person(owner, "bob@acme.test", "Bob Employee")
    return owner, ann, bob


def connect_company(owner):
    cs = vs.ConnectionStore(owner.engine.s, owner.org_id)
    cs.save("slack", "slackauth000001", SCRIPT)
    cs.save("gmail", "gmailauth000001", "scriGMAILSCRIPT01")


# ---------------- the mode ----------------
def test_a_company_starts_in_rehearsal_and_only_an_admin_can_go_live_after_confirming(team):
    owner, ann, bob = team
    d = owner.get("/api/delivery").json()
    assert d["mode"] == "rehearsal" and d["rehearsal_person"]["id"] == owner.uid and d["people"] == 3 and d["slack_matched"] == 0 and "Rehearsal" in d["notes"][0]
    assert ann.get("/api/delivery").status_code == 403 and ann.post("/api/delivery/mode", {"mode": "live", "confirm": True}).status_code == 403
    assert owner.post("/api/delivery/mode", {"mode": "live"}).status_code == 422  # no confirmation
    assert owner.post("/api/delivery/mode", {"mode": "live", "confirm": True}).status_code == 409  # nothing connected: nothing to send with
    connect_company(owner)
    assert owner.post("/api/delivery/mode", {"mode": "sometimes", "confirm": True}).status_code == 422
    r = owner.post("/api/delivery/mode", {"mode": "live", "confirm": True})
    assert r.status_code == 200 and r.json()["mode"] == "live" and r.json()["notes"][0].startswith("3 people have no Slack account matched")
    assert owner.get("/api/clock").json()["mode"] == "live" and owner.engine.live.mode() == "live"
    ev = json.loads(owner.engine.s.all("SELECT data_json FROM events WHERE type='delivery_mode_changed'")[-1]["data_json"])
    assert ev["mode"] == "live" and ev["was"][0] == "rehearsal"
    assert owner.post("/api/delivery/mode", {"mode": "rehearsal"}).json()["mode"] == "rehearsal"  # and back, without needing confirmation


def test_the_rehearsal_recipient_must_be_an_admin_who_has_joined(team):
    owner, ann, bob = team
    assert owner.post("/api/delivery/mode", {"mode": "rehearsal", "rehearsal_person": ann.uid}).status_code == 422
    assert owner.post("/api/delivery/mode", {"mode": "rehearsal", "rehearsal_person": "nobody"}).status_code == 422
    owner.post(f"/api/people/{ann.uid}", {"access_role": "admin"})
    assert owner.post("/api/delivery/mode", {"mode": "rehearsal", "rehearsal_person": ann.uid}).json()["rehearsal_person"]["id"] == ann.uid


def test_admins_see_the_mode_banners_and_members_do_not_get_the_delivery_details(team):
    owner, ann, bob = team
    c = owner.get("/api/clock").json()
    assert c["notes"] and c["banners"] and c["mode"] == "rehearsal"
    m = ann.get("/api/clock").json()
    assert m["banners"] == [] and m["notes"] == [] and m["delivery"]["cap"] == 150  # delivery faults are for the people who can fix them


def test_the_demo_company_cannot_change_delivery_or_match_slack(main):
    demo = Browser(main)
    demo.demo("e10")
    assert demo.post("/api/delivery/mode", {"mode": "live", "confirm": True}).status_code == 403
    assert demo.post("/api/integrations/slack/match").status_code == 403
    assert demo.get("/api/clock").json()["notes"] == [] and demo.get("/api/clock").json()["banners"] == []


# ---------------- matching Slack accounts ----------------
class FakeSlack:
    def __init__(self, found, kind=vs.SUCCESS):
        self.found, self.kind, self.asked = found, kind, []

    def slack_lookup_user(self, email):
        self.asked.append(email)
        if self.kind != vs.SUCCESS:
            return vs.Result(self.kind, error="Slack is not connected", code="not_connected")
        if email in self.found:
            return vs.Result(vs.SUCCESS, data={"found": True, "id": self.found[email], "name": "x"})
        return vs.Result(vs.SUCCESS, data={"found": False}, code="not_found")


def test_matching_fills_slack_ids_from_directory_emails_and_reports_who_was_not_found(team, monkeypatch, main):
    owner, ann, bob = team
    fake = FakeSlack({"ann@acme.test": "U0ANN0001", "owner@acme.test": "U0OWN0001"})
    monkeypatch.setattr(vs, "get_integration", lambda store, tenant=None: fake)
    assert ann.post("/api/integrations/slack/match").status_code == 403
    r = owner.post("/api/integrations/slack/match").json()
    assert sorted(r["matched"]) == ["Ann Employee", "Owner of Acme Ltd"] and r["not_found"] == ["Bob Employee"] and r["unmatched_left"] == 1
    assert owner.engine.d.emp(ann.uid)["slack_id"] == "U0ANN0001" and owner.engine.d.emp(bob.uid)["slack_id"] == ""
    assert owner.get("/api/delivery").json()["slack_matched"] == 2
    assert set(fake.asked) == {"ann@acme.test", "bob@acme.test", "owner@acme.test"}  # only directory addresses were looked up
    again = owner.post("/api/integrations/slack/match").json()
    assert fake.asked.count("ann@acme.test") == 1 and again["matched"] == []  # people already matched are not asked again
    assert json.loads(owner.engine.s.all("SELECT data_json FROM events WHERE type='slack_matched'")[0]["data_json"]) == {"matched": 2, "not_found": 1, "problems": 0}


def test_an_odd_slack_answer_is_never_stored_and_a_refused_connection_stops_the_run(team, monkeypatch):
    owner, ann, bob = team
    monkeypatch.setattr(vs, "get_integration", lambda store, tenant=None: FakeSlack({"ann@acme.test": "not an id; DROP", "bob@acme.test": "<@U1>"}))
    r = owner.post("/api/integrations/slack/match").json()
    assert r["matched"] == [] and owner.engine.d.emp(ann.uid)["slack_id"] == ""  # only ids that look like Slack ids are kept
    fake = FakeSlack({}, kind=vs.PERMANENT)
    monkeypatch.setattr(vs, "get_integration", lambda store, tenant=None: fake)
    r = owner.post("/api/integrations/slack/match").json()
    assert len(fake.asked) == 1 and r["problems"] == ["Slack is not connected"]  # the rest would fail the same way


# ---------------- a person's own mailbox ----------------
def test_connecting_your_own_mailbox_uses_your_own_identity_and_never_returns_ids(team, monkeypatch):
    owner, ann, bob = team
    me = ann.get("/api/me/connections").json()
    assert me["demo"] is False and me["configured"] is True and me["app"]["connected"] is False and me["email"] == "ann@acme.test"
    tok = ann.get("/api/me/connections/token").json()
    assert tok["token"].count(".") == 2 and SECRET not in tok["token"] and tok["service_id"] == "rowo0bqrhj5g"
    t = FakeTransport(ok({"success": True, "data": [{"id": AUTH, "isExpired": False}]}))
    real = vs.VsClient
    monkeypatch.setattr(vs, "VsClient", lambda tenant, transport=None: real(tenant, t))
    assert ann.post("/api/me/connections/connected", {"auth_id": "someone-elses"}).status_code == 422  # not in my own connection list
    t.answers = [ok({"success": True, "data": [{"id": AUTH}]}), ok({"success": True, "data": {"flows": []}}), ok({"success": True, "data": {"script_id": SCRIPT}})]
    t.calls.clear()
    assert ann.post("/api/me/connections/connected", {"auth_id": AUTH}).json() == {"ok": True}
    personal = vs.personal_tenant(ann.org_id, ann.uid)
    assert vs.ConnectionStore(ann.engine.s, personal).get("gmail")["script_id"] == SCRIPT
    assert vs.ConnectionStore(ann.engine.s, ann.org_id).get("gmail") is None and vs.ConnectionStore(ann.engine.s, vs.personal_tenant(ann.org_id, bob.uid)).get("gmail") is None
    t.answers = [ok({"success": True, "data": [{"id": AUTH}]})]
    body = ann.get("/api/me/connections").text
    assert '"connected":true' in body.replace(" ", "") and AUTH not in body and SCRIPT not in body and SECRET not in body
    assert bob.get("/api/me/connections").json()["app"]["connected"] is False  # my mailbox is not anyone else's
    audit = json.dumps(ann.engine.s.all("SELECT type, data_json FROM events"))
    assert "personal_mailbox_connected" in audit and AUTH not in audit and SCRIPT not in audit


def test_a_personal_mailbox_test_only_emails_the_person_themselves(team, monkeypatch):
    owner, ann, bob = team
    vs.ConnectionStore(ann.engine.s, vs.personal_tenant(ann.org_id, ann.uid)).save("gmail", AUTH, SCRIPT)
    t = FakeTransport(ok({"success": True, "data": {"id": "m1", "threadId": "t1"}}))
    real = vs.VsClient
    monkeypatch.setattr(vs, "VsClient", lambda tenant, transport=None: real(tenant, t))
    r = ann.post("/api/me/connections/test", {"to": "someone-else@example.test"}).json()
    assert r["outcome"] == "success" and t.calls[-1][3]["inputData"]["to"] == "ann@acme.test"
    assert bob.post("/api/me/connections/test").status_code == 409  # nothing connected for Bob


def test_disconnecting_removes_only_your_own_mailbox(team, monkeypatch):
    owner, ann, bob = team
    vs.ConnectionStore(ann.engine.s, vs.personal_tenant(ann.org_id, ann.uid)).save("gmail", AUTH, SCRIPT)
    vs.ConnectionStore(ann.engine.s, vs.personal_tenant(ann.org_id, bob.uid)).save("gmail", "bobauth0000001", "scriBOBSCRIPT0001")
    t = FakeTransport(ok({"success": True, "data": {}}))
    real = vs.VsClient
    monkeypatch.setattr(vs, "VsClient", lambda tenant, transport=None: real(tenant, t))
    assert ann.post("/api/me/connections/gmail/disconnect").json() == {"ok": True}
    assert vs.ConnectionStore(ann.engine.s, vs.personal_tenant(ann.org_id, ann.uid)).get("gmail") is None
    assert vs.ConnectionStore(ann.engine.s, vs.personal_tenant(ann.org_id, bob.uid)).get("gmail")["script_id"] == "scriBOBSCRIPT0001"


def test_the_demo_has_no_personal_mailbox_and_signed_out_visitors_get_nothing(main):
    demo = Browser(main)
    demo.demo("e01")
    assert demo.get("/api/me/connections").json() == {"demo": True, "configured": False, "app": None}
    assert demo.get("/api/me/connections/token").status_code == 403 and demo.post("/api/me/connections/test").status_code == 403
    for url in ("/api/me/connections", "/api/me/connections/token"):
        assert Browser(main).get(url).status_code == 401


def test_one_companys_personal_mailbox_is_never_used_for_another_company(main):
    a, b = apiutil.company(main), apiutil.company(main, "o@else.test", "code-2", "Else Co")
    ann, ed = apiutil.add_person(a, "ann@acme.test", "Ann"), apiutil.add_person(b, "ed@else.test", "Ed")
    vs.ConnectionStore(ann.engine.s, vs.personal_tenant(ann.org_id, ann.uid)).save("gmail", AUTH, SCRIPT)
    assert ed.get("/api/me/connections").json()["app"]["connected"] is False and vs.personal_tenant(a.org_id, "x") != vs.personal_tenant(b.org_id, "x")


def test_a_hand_typed_slack_id_must_look_like_one(team):
    owner, ann, bob = team
    for bad in ("bogus", "u0ann0001", "U1", "U0ANN0001; DROP TABLE", "<@U0ANN0001>"):
        assert owner.post(f"/api/people/{ann.uid}", {"slack_id": bad}).status_code == 422, bad
    assert owner.post(f"/api/people/{ann.uid}", {"slack_id": "U0ANN0001"}).status_code == 200 and owner.engine.d.emp(ann.uid)["slack_id"] == "U0ANN0001"
    assert owner.post(f"/api/people/{ann.uid}", {"slack_id": ""}).status_code == 200 and owner.engine.d.emp(ann.uid)["slack_id"] == ""
