"""Building the approval automation, starting a run, and the public callback route (open past the shared password, protected by the token). Fake transport; no network."""
import json

import pytest

from app import slack_decisions as sd, viasocket_adapter as vs
from app.db import Store
from tests import apiutil
from tests.apiutil import Browser
from tests.test_viasocket_adapter import AUTH, SCRIPT, FakeTransport, env, ok  # noqa: F401 - env is an autouse fixture

FLOW = "scriFLOWSCRIPT0001"
CALLBACK = "https://deskhand.example.test/api/hooks/slack-decision"
TOKEN = "automation-token-secret-value-123456"


def integ_with(*answers, connected=True):
    st = Store(":memory:")
    cs = vs.ConnectionStore(st, "acme-demo")
    if connected:
        cs.save("slack", AUTH, SCRIPT)
    t = FakeTransport(*answers)
    return vs.ViaSocketIntegration(st, "acme-demo", vs.VsClient("acme-demo", t)), t, st


def good_sequence():
    return [ok({"data": {"token": TOKEN}}), ok({"success": True, "data": {"script_id": FLOW}}), ok({"success": True, "data": {}}), ok({"success": True, "data": {}})]


def test_the_approval_automation_is_built_in_the_documented_order_with_the_callback_written_into_it():
    integ, t, st = integ_with(*good_sequence())
    r = integ.create_approval_flow(CALLBACK)
    assert r.ok and r.data == {"created": True} and FLOW not in json.dumps(r.data)
    urls = [c[1] for c in t.calls]
    assert urls[0].endswith("/users/register") and [u.rsplit("/", 1)[1] for u in urls[1:]] == [vs.FLOW_FUNCTIONS["create"], vs.FLOW_FUNCTIONS["trigger"], vs.FLOW_FUNCTIONS["steps"]]
    assert t.calls[0][2]["authorization"].count(".") == 2  # the company's own identity signs the request for the automation token
    create, trig, steps = (c[3] for c in t.calls[1:])
    assert create["proxy_auth_token"] == TOKEN and create["projectId"] == "proj1" and trig == {"scriptId": FLOW, "projectId": "proj1", "triggerType": "webhook", "proxy_auth_token": TOKEN}
    ask, report = steps["steps"]
    assert [s["stepname"] for s in steps["steps"]] == ["ask", "report"] and ask["service_action_functions"] == [{"name": "sendInteractive", "auth_id": AUTH, "action_id": vs.INTERACTIVE_ACTION_ID}]
    assert 'messageto: "user"' in ask["code"] and '"Approve": "approve"' in ask["code"] and "b.slack_user" in ask["code"] and report["service_action_functions"] == []
    assert CALLBACK in report["code"] and "b.callback_url" not in report["code"] and "callback" not in json.dumps(ask)  # the address is fixed in the code, not taken from a run's input
    assert 'decision: a.error ? "error"' in report["code"] and "b.token" in report["code"]
    assert st.one("SELECT script_id FROM connections WHERE app='slack_flow'")["script_id"] == FLOW  # kept on the server like any runner id


@pytest.mark.parametrize("bad", ["http://deskhand.example.test/api/hooks/slack-decision", "https://deskhand.example.test/api/other", "https://evil.test/x?y=https://a/api/hooks/slack-decision",
                                 "https://a b.test/api/hooks/slack-decision", "", "javascript:alert(1)", 'https://x.test/api/hooks/slack-decision"; steal()//'])
def test_the_automation_is_only_built_for_a_public_https_callback_on_the_fixed_path(bad):
    integ, t, st = integ_with(*good_sequence())
    r = integ.create_approval_flow(bad)
    assert not r.ok and r.code == "no_public_url" and not t.calls and st.one("SELECT * FROM connections WHERE app='slack_flow'") is None


def test_every_failure_while_building_stops_the_build_and_stores_nothing():
    for stage in range(4):
        answers = good_sequence()
        answers[stage] = ok({"status": 400, "data": {"message": "refused by viaSocket"}}) if stage else ok({"message": "no"}, 401)
        integ, t, st = integ_with(*answers)
        r = integ.create_approval_flow(CALLBACK)
        assert not r.ok and st.one("SELECT * FROM connections WHERE app='slack_flow'") is None and len(t.calls) == stage + 1, stage
    integ, t, st = integ_with(*good_sequence(), connected=False)
    assert integ.create_approval_flow(CALLBACK).code == "not_connected" and not t.calls


def test_starting_a_run_posts_only_the_three_fields_and_waiting_for_the_click_is_success():
    integ, t, st = integ_with((200, "Flow is currently paused, waiting for human intervention"))
    assert integ.slack_approval("U0IMRAN01", "hello", "tok.en").code == "no_flow"  # not switched on yet
    st.q("INSERT INTO connections(tenant,app,auth_id,script_id,settings_json,updated_at) VALUES('acme-demo','slack_flow',NULL,?,'{}',0)", (FLOW,))
    r = integ.slack_approval("U0IMRAN01", "hello", "tok.en")
    method, url, _, body = t.calls[0]
    assert r.ok and r.data == {"waiting": True} and url.endswith("/func/" + FLOW) and body == {"slack_user": "U0IMRAN01", "text": "hello", "token": "tok.en"}
    for answer, kind in ((ok({"success": False, "message": "flow is deleted"}), vs.PERMANENT), ((502, "bad gateway"), vs.RETRYABLE), ((200, "<html>"), vs.PERMANENT)):
        integ2, _, st2 = integ_with(answer)
        st2.q("INSERT INTO connections(tenant,app,auth_id,script_id,settings_json,updated_at) VALUES('acme-demo','slack_flow',NULL,?,'{}',0)", (FLOW,))
        assert integ2.slack_approval("U1", "x", "t").kind == kind
    integ3, _, st3 = integ_with(vs.TransportError("ReadTimeout"))
    st3.q("INSERT INTO connections(tenant,app,auth_id,script_id,settings_json,updated_at) VALUES('acme-demo','slack_flow',NULL,?,'{}',0)", (FLOW,))
    r = integ3.slack_approval("U1", "x", "t")
    assert r.kind == vs.RETRYABLE and not r.safe_retry  # a read timeout may have started the run: never retried by itself


# ---------------- the public callback ----------------
@pytest.fixture
def main(tmp_path, monkeypatch):
    return apiutil.load_app(monkeypatch, tmp_path)


@pytest.fixture
def team(main):
    owner = apiutil.company(main)
    boss = apiutil.add_person(owner, "boss@acme.test", "Bo Boss")
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee", manager_id=boss.uid)
    for who, sid in ((boss, "U0BOSS001"), (ann, "U0ANN0001")):
        main.accounts.update_user(owner.org_id, who.uid, owner.uid, slack_id=sid)
    main.registry.refresh_directory(owner.org_id)
    return owner, boss, ann


def hook(browser, **body):
    return browser.c.post("/api/hooks/slack-decision", json=body)


def test_the_callback_needs_no_session_and_no_shared_password_but_every_other_route_still_does(team, main, monkeypatch):
    owner, boss, ann = team
    rid = ann.post("/api/ingest", {"text": "I need Friday and Monday off, family function.", "source_event_id": "h1"}).json()["request_ids"][0]
    tok = sd.issue(owner.engine, owner.org_id, "approval", rid, boss.uid)
    monkeypatch.setenv("DESKHAND_DEMO_PASSWORD", "gate-secret-for-tests")
    stranger = Browser(main)
    assert stranger.get("/api/state").status_code == 401 and stranger.c.get("/").status_code == 401  # the shared password still guards everything else
    r = hook(stranger, token=tok, slack_user="U0BOSS001", decision="approve")
    assert r.status_code == 200 and r.json() == {"status": "approved"} and owner.engine.req(rid)["status"] == "CLOSED"
    assert owner.engine.ledger.balance(ann.uid, "casual") == 10  # the click did the whole job, including the ledger


def test_every_outcome_is_http_200_with_a_status_word_and_a_repeat_changes_nothing(team, main):
    owner, boss, ann = team
    rid = ann.post("/api/ingest", {"text": "Need a Claude seat for my research project", "source_event_id": "h2"}).json()["request_ids"][0]
    tok = sd.issue(owner.engine, owner.org_id, "approval", rid, owner.uid)
    s = Browser(main)
    got = [hook(s, token=t, slack_user=u, decision=d) for t, u, d in (("", "", ""), ("garbage", "U0BOSS001", "approve"), (tok, "U0NOBODY1", "approve"), (tok, "U0BOSS001", "approve"), (tok, "U0BOSS001", "maybe"))]
    assert [r.status_code for r in got] == [200] * 5 and [r.json()["status"] for r in got] == ["invalid", "invalid", "unknown_person", "not_allowed", "bad_decision"]
    assert hook(s).json() == {"status": "invalid"} and s.c.post("/api/hooks/slack-decision", content=b"not json", headers={"content-type": "application/json"}).status_code == 422
    assert owner.engine.req(rid)["status"] == "PENDING_APPROVAL"
    owner_slack = "U0OWNER01"
    main.accounts.update_user(owner.org_id, owner.uid, owner.uid, slack_id=owner_slack)
    main.registry.refresh_directory(owner.org_id)
    assert hook(s, token=tok, slack_user=owner_slack, decision="approve").json() == {"status": "approved"}
    assert hook(s, token=tok, slack_user=owner_slack, decision="approve").json() == {"status": "already_used"}
    assert len([e for e in owner.engine.s.all("SELECT * FROM events WHERE type='approved_stage'")]) == 1


def test_a_token_works_only_in_its_own_company_and_never_in_the_demo(team, main):
    owner, boss, ann = team
    other = apiutil.company(main, "o@else.test", "code-2", "Else Co")
    rid = ann.post("/api/ingest", {"text": "Need a Claude seat for my research project", "source_event_id": "h3"}).json()["request_ids"][0]
    tok = sd.issue(owner.engine, owner.org_id, "approval", rid, owner.uid)
    forged = other.org_id + "." + tok.split(".", 1)[1]  # the right secret under another company's name
    s = Browser(main)
    assert hook(s, token=forged, slack_user="U0BOSS001", decision="approve").json() == {"status": "invalid"}
    assert hook(s, token="acme-demo." + tok.split(".", 1)[1], slack_user="U0BOSS001", decision="approve").json() == {"status": "invalid"}  # the demo company never takes a click
    assert owner.engine.req(rid)["status"] == "PENDING_APPROVAL"


def test_the_callback_is_rate_limited_and_logs_only_a_status_word(team, main, caplog):
    import logging
    owner, boss, ann = team
    s = Browser(main)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        hook(s, token="acme.secrettokenvalue", slack_user="U0BOSS001", decision="approve")
    assert "slack decision: invalid" in caplog.text and "secrettokenvalue" not in caplog.text and "U0BOSS001" not in caplog.text
    codes = [hook(s, token="x", decision="approve").status_code for _ in range(125)]
    assert 429 in codes and codes[0] == 200


# ---------------- the admin's controls ----------------
def test_enabling_the_buttons_and_testing_them_are_admin_only_and_need_a_public_address(team, main, monkeypatch):
    owner, boss, ann = team
    assert boss.post("/api/integrations/slack/buttons").status_code == 403 and boss.post("/api/integrations/slack/test-approval").status_code == 403
    assert owner.post("/api/integrations/slack/buttons").status_code == 409  # Slack not connected
    vs.ConnectionStore(owner.engine.s, owner.org_id).save("slack", AUTH, SCRIPT)
    assert owner.post("/api/integrations/slack/buttons").status_code == 409  # base address is http://localhost:8000: viaSocket could never reach it
    owner.engine.live.base_url = "https://deskhand.example.test"
    seen = {}

    class Fake:
        def create_approval_flow(self, url):
            seen["url"] = url
            return vs.Result(vs.SUCCESS, data={"created": True})

    monkeypatch.setattr(vs, "get_integration", lambda store, tenant=None: Fake())
    assert owner.post("/api/integrations/slack/buttons").status_code == 200 and seen["url"] == CALLBACK
    assert "slack_buttons_enabled" in [e["type"] for e in owner.engine.s.all("SELECT type FROM events")]


def test_the_test_approval_goes_only_to_the_admin_and_changes_nothing(team, main, monkeypatch):
    owner, boss, ann = team
    assert owner.post("/api/integrations/slack/test-approval").status_code == 409  # the admin has no Slack account matched
    main.accounts.update_user(owner.org_id, owner.uid, owner.uid, slack_id="U0OWNER01")
    main.registry.refresh_directory(owner.org_id)
    sent = {}

    class Fake:
        def slack_approval(self, user_id, text, token):
            sent.update(user=user_id, text=text, token=token)
            return vs.Result(vs.SUCCESS, data={"waiting": True})

    monkeypatch.setattr(vs, "get_integration", lambda store, tenant=None: Fake())
    assert owner.post("/api/integrations/slack/test-approval").json()["state"] == "waiting" and sent["user"] == "U0OWNER01" and "Nothing will change" in sent["text"]
    s = Browser(main)
    assert hook(s, token=sent["token"], slack_user="U0BOSS001", decision="approve").json() == {"status": "not_allowed"}
    assert hook(s, token=sent["token"], slack_user="U0OWNER01", decision="approve").json() == {"status": "test_ok", "decision": "approve"}
    assert owner.get("/api/integrations/slack/test-approval").json()["state"] == "received" and boss.get("/api/integrations/slack/test-approval").status_code == 403
    assert owner.engine.s.all("SELECT * FROM requests") == []


def test_switching_the_buttons_on_again_updates_the_same_automation_instead_of_making_another():
    integ, t, st = integ_with(*good_sequence())
    assert integ.create_approval_flow(CALLBACK).data == {"created": True}
    t.answers, t.calls = [ok({"data": {"token": TOKEN}}), ok({"success": True, "data": {}})], []
    r = integ.create_approval_flow("https://new-address.example.test/api/hooks/slack-decision")
    assert r.ok and r.data == {"created": False} and [c[1].rsplit("/", 1)[1] for c in t.calls] == ["register", vs.FLOW_FUNCTIONS["steps"]]
    assert t.calls[1][3]["scriptId"] == FLOW and "new-address.example.test" in json.dumps(t.calls[1][3]["steps"]) and CALLBACK not in json.dumps(t.calls[1][3]["steps"])
    assert st.one("SELECT COUNT(*) c FROM connections WHERE app='slack_flow'")["c"] == 1
    t.answers, t.calls = [ok({"data": {"token": TOKEN}}), ok({"status": 400, "data": {"message": "flow has been deleted"}})] + good_sequence()[1:], []
    r = integ.create_approval_flow(CALLBACK)  # the automation was deleted in viaSocket: a new one is built
    assert r.ok and r.data == {"created": True} and len(t.calls) == 5


def test_disconnecting_slack_also_removes_the_approval_automation(team, main, monkeypatch):
    owner, boss, ann = team
    cs = vs.ConnectionStore(owner.engine.s, owner.org_id)
    cs.save("slack", AUTH, SCRIPT)
    owner.engine.s.q("INSERT INTO connections(tenant,app,auth_id,script_id,settings_json,updated_at) VALUES(?,?,NULL,?,'{}',0)", (owner.org_id, "slack_flow", FLOW))
    t = FakeTransport(ok({"success": True, "data": {}}))
    real = vs.VsClient
    monkeypatch.setattr(vs, "VsClient", lambda tenant, transport=None: real(tenant, t))
    assert owner.post("/api/connections/slack/disconnect").json() == {"ok": True}
    deleted = [c[1] for c in t.calls if "updatestatus" in c[1]]
    assert len(deleted) == 2 and deleted[0].endswith(f"{FLOW}?status=0") and deleted[1].endswith(f"{SCRIPT}?status=0")  # the automation first, then the runner
    assert cs.get("slack") is None and cs.get("slack_flow") is None
