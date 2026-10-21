"""viaSocket adapter and Connections API. No network: a fake transport stands in for viaSocket (rule 7)."""
import base64
import hashlib
import hmac
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import viasocket_adapter as vs
from app.db import Store

SECRET = "test-secret-value-0123456789abcdef-test"
SCRIPT = "scriABCDEFGH12345"
AUTH = "authrowbu58rc_slackconn1"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("VIASOCKET_ORG_ID", "org1")
    monkeypatch.setenv("VIASOCKET_PROJECT_ID", "proj1")
    monkeypatch.setenv("VIASOCKET_EMBED_SECRET", SECRET)
    monkeypatch.setenv("VIASOCKET_API_URL", "https://api.test.invalid")
    monkeypatch.setenv("VIASOCKET_RUN_URL", "https://run.test.invalid")
    monkeypatch.delenv("DESKHAND_INTEGRATION", raising=False)


class FakeTransport:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def request(self, method, url, headers=None, body=None, timeout=25):
        self.calls.append((method, url, headers, body))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def ok(payload, status=200):
    return status, json.dumps(payload)


# ---- rule 10: HTTP 200 is not success ----
@pytest.mark.parametrize("status,body,kind,code", [
    (200, {"success": True, "data": {"ts": "1.0"}}, vs.SUCCESS, ""),
    (200, {"ts": "1.0", "channel": "C1"}, vs.SUCCESS, ""),
    (200, {"success": False, "message": "bad input"}, vs.PERMANENT, "success_false"),
    (200, {"status": 400, "data": {"message": "Missing field"}}, vs.PERMANENT, "refused_400"),
    (200, {"response": {"status": 400, "data": {"message": "Auth data not found"}}}, vs.PERMANENT, "app_400"),
    (200, {"response": {"status": 429, "data": {"message": "slow down"}}}, vs.RETRYABLE, "app_429"),
    (200, {"ok": False, "error": "channel_not_found"}, vs.PERMANENT, "app_ok_false"),
    (200, {"ok": False, "error": "ratelimited"}, vs.RETRYABLE, "app_ok_false"),
    (200, {"success": True, "data": {"ok": False, "error": "not_in_channel"}}, vs.PERMANENT, "app_ok_false"),
    (502, {"message": "bad gateway"}, vs.RETRYABLE, "http_502"),
    (401, {"message": "no"}, vs.PERMANENT, "http_401"),
    (429, {}, vs.RETRYABLE, "http_429"),
])
def test_every_response_is_normalised(status, body, kind, code):
    r = vs.normalise(status, json.dumps(body))
    assert r.kind == kind and r.code == code


def test_non_json_and_transport_errors():
    assert vs.normalise(200, "<html>").kind == vs.PERMANENT
    assert vs.normalise(503, "<html>").kind == vs.RETRYABLE
    c = vs.VsClient("t", FakeTransport(vs.TransportError("ConnectTimeout")))
    assert c.run(SCRIPT, "v", {}).kind == vs.RETRYABLE


def test_expectation_catches_a_200_without_the_effect():
    r = vs.normalise(200, json.dumps({"success": True, "data": {"nothing": 1}}), expect=lambda b: "" if b.get("id") else "no id")
    assert r.kind == vs.PERMANENT and r.code == "unexpected_body"


# ---- rule 8: nothing secret leaks ----
def test_redaction_covers_secret_ids_and_tokens():
    token = vs.sign_embed_token("acme-demo")
    text = f"failed for {SECRET} at https://flow.sokt.io/func/{SCRIPT} with {token} and {AUTH}"
    out = vs.redact(text, extra=(AUTH,))
    for leaked in (SECRET, SCRIPT, token, AUTH):
        assert leaked not in out


def test_errors_never_contain_the_script_id():
    c = vs.VsClient("t", FakeTransport(ok({"success": False, "message": f"script {SCRIPT} exploded"})))
    r = c.run(SCRIPT, "v", {})
    assert SCRIPT not in r.error and r.kind == vs.PERMANENT


def test_embed_token_shape_and_signature():
    tok = vs.sign_embed_token("acme-demo")
    h, p, sig = tok.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)
    assert json.loads(base64.urlsafe_b64decode(pad(h))) == {"alg": "HS256", "typ": "JWT"}
    claims = json.loads(base64.urlsafe_b64decode(pad(p)))
    assert claims == {"org_id": "org1", "project_id": "proj1", "unique_identifier": "acme-demo"}  # no exp, tenant id not an email
    want = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert sig == want


def test_client_sends_bare_token_not_bearer():
    t = FakeTransport(ok({"success": True, "data": []}))
    vs.VsClient("acme-demo", t).connections()
    assert not t.calls[0][2]["authorization"].startswith("Bearer") and t.calls[0][2]["authorization"].count(".") == 2


# ---- adapter behaviour (mock mode is the default) ----
def _integ(*answers, settings=None):
    store = Store(":memory:")
    cs = vs.ConnectionStore(store, "acme-demo")
    cs.save("slack", AUTH, SCRIPT)
    cs.save("gmail", "gmailauth000001", "scriGMAILSCRIPT01")
    for k, v in (settings or {}).items():
        cs.set_setting(k[0], k[1], v)
    t = FakeTransport(*answers)
    return vs.ViaSocketIntegration(store, "acme-demo", vs.VsClient("acme-demo", t)), t


def test_slack_send_uses_chosen_channel_and_fixed_action():
    integ, t = _integ(ok({"success": True, "data": {"ok": True, "ts": "1.2", "channel": "C1"}}), settings={("slack", "channel_id"): "C123"})
    r = integ.slack_send("hello")
    assert r.ok
    method, url, _, body = t.calls[0]
    assert url.endswith("/func/" + SCRIPT) and body["action_version_id"] == "rowj2u3wc8h5"
    assert body["inputData"]["destination"] == {"messageto": "channel", "channel_id": ["C123"]}


def test_slack_send_needs_a_channel_and_a_connection():
    integ, _ = _integ(ok({}))
    assert integ.slack_send("x").code == "no_channel"
    empty = vs.ViaSocketIntegration(Store(":memory:"), "acme-demo", vs.VsClient("acme-demo", FakeTransport(ok({}))))
    assert empty.gmail_send("a@b.co", "s", "b").code == "not_connected"


def test_slack_lookup_not_found_is_a_clean_answer_and_found_is_parsed():
    integ, _ = _integ(ok({"message": "User not found"}))
    assert integ.slack_lookup_user("x@y.co").data == {"found": False}
    integ, t = _integ(ok({"id": "U1", "real_name": "Priya", "name": "priya"}))
    assert integ.slack_lookup_user("x@y.co").data == {"found": True, "id": "U1", "name": "Priya"}
    assert t.calls[0][3]["inputData"]["search_by"] == "email_search"


def test_gmail_200_without_message_id_is_a_failure():
    integ, _ = _integ(ok({"success": True, "data": {"labelIds": []}}))
    assert integ.gmail_send("a@b.co", "s", "b").kind == vs.PERMANENT
    integ, t = _integ(ok({"success": True, "data": {"id": "m1", "threadId": "t1"}}))
    assert integ.gmail_send("a@b.co", "s", "b").ok and t.calls[0][3]["action_version_id"] == "rowwj0sfmhub"


# ---- Connections API ----
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKHAND_DB", str(tmp_path / "t.db"))
    import importlib
    from app import main
    importlib.reload(main)
    return TestClient(main.app), main


def test_connections_are_admin_only_and_list_every_setting(client):
    c, _ = client
    assert c.get("/api/connections", params={"actor_id": "e01"}).status_code == 403
    r = c.get("/api/connections", params={"actor_id": "e10"}).json()
    assert [a["key"] for a in r["apps"]] == ["slack", "gmail"] and "mode" not in r
    assert [x["key"] for x in r["apps"][1]["settings"]] == ["from", "notify_email"] and r["problems"]
    tok = c.get("/api/connections/token", params={"actor_id": "e10"}).json()["token"]
    assert tok.count(".") == 2 and SECRET not in tok


def test_server_without_viasocket_values_says_so_instead_of_connecting(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("VIASOCKET_EMBED_SECRET")
    assert c.get("/api/connections/token", params={"actor_id": "e10"}).status_code == 409
    assert c.get("/api/connections", params={"actor_id": "e10"}).json()["configured"] is False


def test_connect_flow_never_returns_ids_and_rejects_foreign_connections(client, monkeypatch):
    c, main = client
    t = FakeTransport(ok({"success": True, "data": [{"id": AUTH, "isExpired": False}]}))
    real = vs.VsClient
    monkeypatch.setattr(vs, "VsClient", lambda tenant: real(tenant, t))
    bad = c.post("/api/connections/connected", json={"actor_id": "e10", "app": "slack", "auth_id": "someone-elses"})
    assert bad.status_code == 422
    t.answers = [ok({"success": True, "data": [{"id": AUTH}]}), ok({"success": True, "data": {"flows": []}}), ok({"success": True, "data": {"script_id": SCRIPT}})]
    t.calls.clear()
    assert c.post("/api/connections/connected", json={"actor_id": "e10", "app": "slack", "auth_id": AUTH}).json() == {"ok": True}
    assert any(u.endswith(f"/embed/enable/rowbu58rc/{AUTH}") for _, u, _, _ in t.calls)
    t.answers = [ok({"success": True, "data": [{"id": AUTH}]})]
    body = c.get("/api/connections", params={"actor_id": "e10"}).text
    assert AUTH not in body and SCRIPT not in body and SECRET not in body
    assert '"status":"connected"' in body.replace(" ", "")
    audit = json.dumps(main.engine.s.all("SELECT data_json FROM events"))
    assert AUTH not in audit and SCRIPT not in audit and "connection_added" in json.dumps(main.engine.s.all("SELECT type FROM events"))


def test_reset_demo_data_keeps_connections(client):
    _, main = client
    vs.ConnectionStore(main.engine.s, main.TENANT).save("slack", AUTH, SCRIPT)
    main.engine.reset()
    assert vs.ConnectionStore(main.engine.s, main.TENANT).get("slack")["script_id"] == SCRIPT


def test_test_calls_validate_input_and_email_only_the_stored_address(client, monkeypatch):
    c, main = client
    assert c.post("/api/connections/test", json={"actor_id": "e01", "action": "slack_send"}).status_code == 403
    assert c.post("/api/connections/test", json={"actor_id": "e10", "action": "slack_lookup", "email": "a@b.co, c@d.co"}).status_code == 422
    assert c.post("/api/connections/test", json={"actor_id": "e10", "action": "gmail_send"}).status_code == 409  # no notification email yet
    cs = vs.ConnectionStore(main.engine.s, main.TENANT)
    cs.save("gmail", "gmailauth000001", "scriGMAILSCRIPT01")
    cs.set_setting("gmail", "notify_email", "stored@example.test")
    t = FakeTransport(ok({"success": True, "data": {"id": "m1", "threadId": "t1"}}))
    real = vs.VsClient
    monkeypatch.setattr(vs, "VsClient", lambda tenant, transport=None: real(tenant, t))
    r = c.post("/api/connections/test", json={"actor_id": "e10", "action": "gmail_send", "email": "someone-else@example.test"}).json()
    assert r["outcome"] == "success" and t.calls[0][3]["inputData"]["to"] == "stored@example.test"  # a typed address is ignored


def test_no_secret_or_viasocket_ids_in_client_code():
    html = open("app/static/index.html", encoding="utf-8").read()
    for bad in ("script_id", "proxy_auth", "EMBED_SECRET"):
        assert bad not in html
    assert not re.search(r"\bscri[A-Za-z0-9]{6,}", html)
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}\.", html)


def test_slack_reply_that_echoes_the_runner_id_is_never_passed_on():
    reply = {"ok": True, "ts": "1.2", "channel": "C1", "message": {"metadata": {"event_payload": {"script_id": SCRIPT}}}}
    integ, _ = _integ(ok(reply), settings={("slack", "channel_id"): "C123"})
    r = integ.slack_send("hello")
    assert r.ok and SCRIPT not in json.dumps(r.data) and set(r.data) == {"ts", "channel"}
    assert SCRIPT not in json.dumps(vs.scrub(reply)) and "[REDACTED]" in json.dumps(vs.scrub(reply))
