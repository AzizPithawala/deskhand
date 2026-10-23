"""viaSocket adapter (spike M1.5, phase 1: actions only). Python over HTTP, no handlers, no automations.

Our code picks every action (rule 9). Every response body is inspected and normalised to success, retryable failure or
permanent failure (rule 10): HTTP 200 never means success. The embed secret, every script_id, auth_id and token are
redacted from anything that is logged, audited, raised or returned (rule 8). The app is LIVE-ONLY: there is no mock
fallback here; tests inject a fake transport."""
import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass, field

import httpx

CONNECT_JS = "https://flow.viasocket.com/connect.js"
USER_AGENT = "deskhand/0.1 (+viasocket-adapter)"  # Python's default urllib agent was answered 403 by the API edge

# Catalog ids (public; copied from each app's viaSocket document, never typed). Version id runs and picks, action id goes to the popup.
APPS = {
    "slack": {
        "service_id": "rowbu58rc", "name": "Slack", "icon": "https://stuff.thingsofbrand.com/slack.com/images/img668216333e_slack.jpg",
        # send_dm: catalog ids from viaSocket's public Slack document (action rowovgwtt9u4, V3). Verified with a real call on 2026-10-22 (spike S1).
        "actions": {"send_message": ("rowj2u3wc8h5", "row1jrbor"), "get_user": ("rowrubgppa5r", "rowtstp3fqlh"), "send_dm": ("rowa5mg9fywy", "rowovgwtt9u4")},
        "settings": [],  # people are messaged one by one by Slack user id; no shared channel to choose any more
    },
    "gmail": {
        "service_id": "rowo0bqrhj5g", "name": "Gmail", "icon": "https://stuff.thingsofbrand.com/gmail.com/images/imge_idrA5FDGTH_1763454052978.svg",
        "actions": {"send_email": ("rowwj0sfmhub", "rowogelpy0h6")},
        "settings": [{"key": "from", "label": "Send from mailbox", "kind": "picker", "action": "send_email", "field": "from", "existing": {}, "required": False}],
    },
}

# viaSocket's own, public "automations from code" functions (https://flow.viasocket.com/documentation/automations.md). They are not ours and not secret.
FLOW_FUNCTIONS = {"create": "scrisgf5H0hN", "trigger": "scrij2DSRMlP", "steps": "scrigkyMNOEC", "read": "scriiZYPU6I3"}
INTERACTIVE_ACTION_ID = "rowbevmtsy7h"  # Slack, Send Interactive Message (V61): catalog id from viaSocket's Slack document

_ID_LIKE = re.compile(r"\b(scri[A-Za-z0-9]{6,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*|emb_[0-9a-f]{20,})\b")


def load_dotenv(path=".env"):
    """Tiny .env reader (no new dependency). Never overrides real environment variables, never logs values."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_base():
    """Hosts come from .env (VIASOCKET_API_URL, VIASOCKET_RUN_URL), as in viaSocket's own examples; see .env.example."""
    v = os.environ.get("VIASOCKET_API_URL", "").rstrip("/")
    if not v:
        raise RuntimeError("VIASOCKET_API_URL is not set")
    return v


def run_base():
    v = os.environ.get("VIASOCKET_RUN_URL", "").rstrip("/")
    if not v:
        raise RuntimeError("VIASOCKET_RUN_URL is not set")
    return v


def sensitive_values(extra=()):
    vals = [os.environ.get("VIASOCKET_EMBED_SECRET", ""), *extra]
    return [v for v in vals if v and len(v) >= 6]


def redact(text, extra=()):
    """Remove the secret, known ids and anything shaped like a script id or token from text."""
    out = str(text)
    for v in sensitive_values(extra):
        out = out.replace(v, "[REDACTED]")
    return _ID_LIKE.sub("[REDACTED]", out)


def scrub(obj, extra=()):
    """Deep-redact any structure before it is returned, logged or shown. Real Slack replies echo the runner id in message metadata."""
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if k in ("script_id", "auth_id", "proxy_auth_token") else scrub(v, extra)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, extra) for v in obj]
    return redact(obj, extra) if isinstance(obj, str) else obj


# ---------------- results ----------------
SUCCESS, RETRYABLE, PERMANENT = "success", "retryable_failure", "permanent_failure"


@dataclass
class Result:
    kind: str
    data: object = None
    error: str = ""
    code: str = ""  # short machine-readable reason, never an id
    safe_retry: bool = False  # True only when the call certainly did not take effect (429, could not connect); else a retry could send twice
    meta: dict = field(default_factory=dict)

    @property
    def ok(self):
        return self.kind == SUCCESS


def _failure_kind(status, message=""):
    m = (message or "").lower()
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0
    if status in (408, 425, 429) or status >= 500 or any(w in m for w in ("ratelimited", "rate limit", "timeout", "timed out", "temporarily", "try again")):
        return RETRYABLE
    return PERMANENT


def _message(payload):
    if isinstance(payload, dict):
        d = payload.get("data")
        for src in (d if isinstance(d, dict) else {}, payload):
            for k in ("message", "error"):
                v = src.get(k)
                if isinstance(v, dict):
                    v = v.get("message")
                if isinstance(v, str) and v:
                    return v
    return ""


def normalise(status, text, expect=None):
    """One response in, one Result out. `expect` is an optional check on the success body returning an error string or ''."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return Result(_failure_kind(status), error=f"non-JSON answer (HTTP {status})", code="bad_body", safe_retry=int(status) == 429)
    if status >= 400:
        return Result(_failure_kind(status, _message(payload)), error=redact(_message(payload) or f"HTTP {status}"), code=f"http_{status}", safe_retry=int(status) == 429)
    if isinstance(payload, dict):
        # refused call: HTTP 200 with "status": 400 and the reason in data.message
        inner = payload.get("response") if isinstance(payload.get("response"), dict) else None
        if inner and inner.get("status") and int(inner["status"]) >= 400:
            msg = (inner.get("data") or {}).get("message") if isinstance(inner.get("data"), dict) else ""
            return Result(_failure_kind(inner["status"], msg), error=redact(msg or f"app answered {inner['status']}"), code=f"app_{inner['status']}",
                          safe_retry=int(inner["status"]) == 429)
        if "status" in payload and isinstance(payload["status"], int) and payload["status"] >= 400:
            return Result(_failure_kind(payload["status"], _message(payload)), error=redact(_message(payload) or f"refused ({payload['status']})"), code=f"refused_{payload['status']}")
        if payload.get("success") is False:
            return Result(_failure_kind(0, _message(payload)), error=redact(_message(payload) or "action failed"), code="success_false")
        body = payload["data"] if "data" in payload and isinstance(payload.get("success"), bool) else payload
        # the app's own failure inside the body (Slack: {"ok": false, "error": "..."})
        if isinstance(body, dict) and body.get("ok") is False:
            return Result(_failure_kind(0, str(body.get("error", ""))), error=redact(str(body.get("error") or "app refused")), code="app_ok_false",
                          safe_retry="ratelimited" in str(body.get("error", "")).lower())
    else:
        body = payload
    if expect:
        problem = expect(body)
        if problem:
            return Result(PERMANENT, data=body, error=problem, code="unexpected_body")
    return Result(SUCCESS, data=body)


# ---------------- transport and client ----------------
class TransportError(Exception):
    pass


class HttpxTransport:
    def request(self, method, url, headers=None, body=None, timeout=25):
        try:
            r = httpx.request(method, url, headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **(headers or {})},
                              content=None if body is None else json.dumps(body), timeout=timeout)
            return r.status_code, r.text
        except httpx.HTTPError as ex:  # connection reset, timeout, DNS: retryable by definition
            raise TransportError(type(ex).__name__)


def _b64(o):
    raw = o if isinstance(o, bytes) else json.dumps(o, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def sign_embed_token(unique_identifier, org_id=None, project_id=None, secret=None):
    """HS256 JWT, claims org_id, project_id, unique_identifier; no exp, by design. unique_identifier is the tenant id, never an email."""
    secret = secret or os.environ.get("VIASOCKET_EMBED_SECRET", "")
    if not secret:
        raise RuntimeError("VIASOCKET_EMBED_SECRET is not set")
    h = _b64({"alg": "HS256", "typ": "JWT"})
    p = _b64({"org_id": org_id or os.environ.get("VIASOCKET_ORG_ID"), "project_id": project_id or os.environ.get("VIASOCKET_PROJECT_ID"),
              "unique_identifier": str(unique_identifier)})
    sig = _b64(hmac.new(secret.encode(), h + b"." + p, hashlib.sha256).digest())
    return (h + b"." + p + b"." + sig).decode()


class VsClient:
    def __init__(self, tenant, transport=None):
        self.tenant, self.t = tenant, transport or HttpxTransport()

    def configured(self):
        return all(os.environ.get(k) for k in ("VIASOCKET_ORG_ID", "VIASOCKET_PROJECT_ID", "VIASOCKET_EMBED_SECRET", "VIASOCKET_API_URL", "VIASOCKET_RUN_URL"))

    def token(self):
        return sign_embed_token(self.tenant)

    def _send(self, method, url, headers=None, body=None, expect=None, extra=(), timeout=None):
        try:
            status, text = self.t.request(method, url, headers, body, **({'timeout': timeout} if timeout else {}))
        except TransportError as ex:
            # only "could not even connect" proves nothing was sent; a read timeout may have been delivered already
            return Result(RETRYABLE, error=f"network error: {ex}", code="transport", safe_retry=str(ex) in ("ConnectError", "ConnectTimeout", "PoolTimeout"))
        r = normalise(status, text, expect)
        r.error = redact(r.error, extra)
        return r

    def api(self, method, path, body=None, expect=None, timeout=None):
        return self._send(method, api_base() + path, {"authorization": self.token()}, body, expect, timeout=timeout)

    def connections(self):
        r = self.api("GET", "/embed/authentications")
        if r.ok and not isinstance(r.data, list):
            return Result(PERMANENT, error="connections list has an unexpected shape", code="unexpected_body")
        return r

    def enable(self, service_id, auth_id):
        """Find the app's runner for this tenant, or enable it. Returns Result(data=script_id). The script_id is a credential."""
        flows = self.api("GET", f"/projects/{os.environ.get('VIASOCKET_PROJECT_ID')}/integrations")
        if flows.ok:
            lst = (flows.data.get("flows") if isinstance(flows.data, dict) else None) or []
            for f in lst:
                if isinstance(f, dict) and f.get("service_id") == service_id and f.get("status") in ("active", 1, "1") and f.get("id"):
                    return Result(SUCCESS, data=f["id"], meta={"reused": True})
        r = self.api("POST", f"/embed/enable/{service_id}/{auth_id}", None)
        if not r.ok:
            return r
        sid = r.data.get("script_id") if isinstance(r.data, dict) else None
        return Result(SUCCESS, data=sid, meta={"reused": False}) if sid else Result(PERMANENT, error="enable returned no script_id", code="unexpected_body")

    def options(self, version_id, field_key, auth_id, existing=None):
        r = self.api("POST", f"/embed/list-options/{version_id}", {"fieldKey": field_key, "auth_id": auth_id, "existingFields": existing or {}})
        if not r.ok:
            return r
        d = r.data
        items = d if isinstance(d, list) else (d.get("data") if isinstance(d, dict) else None)
        if isinstance(d, dict) and isinstance(d.get("response"), dict) and int(d["response"].get("status") or 0) >= 400:
            return Result(PERMANENT, error="the connection cannot be read; reconnect it", code="auth")
        if not isinstance(items, list):
            return Result(PERMANENT, error="picker returned an unexpected shape", code="unexpected_body")
        return Result(SUCCESS, data=[{"label": str(i.get("label", i.get("value"))), "value": i.get("value")} for i in items if isinstance(i, dict)])

    def picker(self, version_id, field_key, auth_id, existing=None, search=None):
        """What list-options answers for one field, reduced to plain items. Most pickers give choices {label, value}. A group without fixed children (Google Sheets column values)
        gives field definitions {key, label, type, required, help}: the key is what goes in inputData. Search text is passed on but some apps ignore it, so callers filter too."""
        fields = {**(existing or {}), **({"_searchText": search} if search else {})}
        r = self.api("POST", f"/embed/list-options/{version_id}", {"fieldKey": field_key, "auth_id": auth_id, "existingFields": fields})
        if not r.ok:
            return r
        d = r.data
        if isinstance(d, dict) and isinstance(d.get("response"), dict) and int(d["response"].get("status") or 0) >= 400:
            return Result(PERMANENT, error="the connection cannot be read; reconnect it", code="auth")
        items = d if isinstance(d, list) else (d.get("data") if isinstance(d, dict) else None)
        if not isinstance(items, list):
            return Result(PERMANENT, error="picker returned an unexpected shape", code="unexpected_body")
        out, kind = [], "options"
        for i in items[:500]:
            if not isinstance(i, dict):
                continue
            if "value" in i:
                out.append({"label": str(i.get("label", i["value"]))[:120], "value": i["value"]})
            elif isinstance(i.get("key"), str):
                kind = "fields"
                out.append({"key": i["key"], "label": str(i.get("label", i["key"]))[:120], "type": str(i.get("type") or "string"), "required": str(i.get("required")).lower() == "true",
                            "help": str(i.get("help") or "")[:200]})
        return Result(SUCCESS, data={"kind": kind, "items": out, "more": isinstance(d, dict) and d.get("offset") is not None})

    def revoke(self, auth_id):
        return self.api("DELETE", f"/embed/authentications/revoke/{auth_id}")

    def run(self, script_id, version_id, input_data, expect=None):
        return self._send("POST", f"{run_base()}/func/{script_id}", None, {"action_version_id": version_id, "inputData": input_data}, expect, extra=(script_id,))

    # ---- automations (viaSocket runs the steps; a refused call still answers HTTP 200 with the refusal in the body, which normalise reads) ----
    def automation_token(self):
        r = self.api("GET", "/users/register")
        d = r.data if isinstance(r.data, dict) else {}
        tok = d.get("token") or (d.get("data") or {}).get("token") if r.ok else None
        if r.ok and not tok:
            return Result(PERMANENT, error="viaSocket gave no automation token", code="unexpected_body")
        return Result(SUCCESS, data=tok) if tok else r

    def flow_call(self, fn, body, token, extra=()):
        return self._send("POST", f"{run_base()}/func/{FLOW_FUNCTIONS[fn]}", None, {**body, "proxy_auth_token": token}, extra=(token, *extra))

    def webhook(self, script_id, body):
        """Start one run of an automation (a webhook trigger). A run that now waits for a person's click answers that it is paused, and that is success."""
        try:
            status, text = self.t.request("POST", f"{run_base()}/func/{script_id}", None, body)
        except TransportError as ex:
            return Result(RETRYABLE, error=f"network error: {ex}", code="transport", safe_retry=str(ex) in ("ConnectError", "ConnectTimeout", "PoolTimeout"))
        if "waiting for human intervention" in (text or "").lower():
            return Result(SUCCESS, data={"waiting": True})
        r = normalise(status, text)
        r.error = redact(r.error, (script_id,))
        return r


# ---------------- connection store (per tenant and app; ids are secrets and never leave the server) ----------------
class ConnectionStore:
    def __init__(self, store, tenant):
        self.s, self.tenant = store, tenant

    def get(self, app):
        row = self.s.one("SELECT * FROM connections WHERE tenant=? AND app=?", (self.tenant, app))
        if row:
            row["settings"] = json.loads(row["settings_json"] or "{}")
        return row

    def save(self, app, auth_id, script_id):
        old = self.get(app)
        self.s.q("INSERT INTO connections(tenant,app,auth_id,script_id,settings_json,updated_at) VALUES(?,?,?,?,?,?) "
                 "ON CONFLICT(tenant,app) DO UPDATE SET auth_id=excluded.auth_id, script_id=excluded.script_id, updated_at=excluded.updated_at",
                 (self.tenant, app, auth_id, script_id, json.dumps(old["settings"] if old else {}), time.time()))

    def set_setting(self, app, key, value):
        row = self.get(app)
        st = row["settings"] if row else {}
        st[key] = value
        self.s.q("UPDATE connections SET settings_json=?, updated_at=? WHERE tenant=? AND app=?", (json.dumps(st), time.time(), self.tenant, app))

    def ai_enabled(self):
        """Whether viaSocket AI may be used to understand requests. Off unless an admin switched it on; kept in the connections table so Reset demo keeps it."""
        row = self.get("ai")
        return bool(row and row["settings"].get("enabled") == "on")

    def set_ai_enabled(self, on):
        self.s.q("INSERT INTO connections(tenant,app,auth_id,script_id,settings_json,updated_at) VALUES(?,?,?,?,?,?) "
                 "ON CONFLICT(tenant,app) DO UPDATE SET settings_json=excluded.settings_json, updated_at=excluded.updated_at",
                 (self.tenant, "ai", None, None, json.dumps({"enabled": "on" if on else "off"}), time.time()))

    def delete(self, app):
        self.s.q("DELETE FROM connections WHERE tenant=? AND app=?", (self.tenant, app))

    def secrets(self):
        return [v for r in self.s.all("SELECT auth_id, script_id FROM connections WHERE tenant=?", (self.tenant,)) for v in r.values() if v]


# ---------------- the three actions behind one interface ----------------
class Integration:
    """What the rest of Deskhand may ask of the outside world. Each method returns a Result."""

    def slack_lookup_user(self, email):
        raise NotImplementedError

    def slack_send(self, text):
        raise NotImplementedError

    def slack_dm(self, user_id, text):
        raise NotImplementedError

    def slack_approval(self, user_id, text, token):
        raise NotImplementedError

    def create_approval_flow(self, callback_url):
        raise NotImplementedError

    def gmail_send(self, to, subject, body):
        raise NotImplementedError


def _slack_sent_ok(body):
    return "" if isinstance(body, dict) and body.get("ts") else "Slack did not return a message timestamp"


def _gmail_sent_ok(body):
    return "" if isinstance(body, dict) and body.get("id") else "Gmail did not return a message id"


class ViaSocketIntegration(Integration):
    def __init__(self, store, tenant, client=None):
        self.conns, self.client = ConnectionStore(store, tenant), client or VsClient(tenant)

    def _conn(self, app):
        c = self.conns.get(app)
        if not c or not c["script_id"]:
            return None, Result(PERMANENT, error=f"{APPS[app]['name']} is not connected. An admin connects it on the Connections tab.", code="not_connected")
        return c, None

    def _run(self, app, action, input_data, expect=None):
        c, err = self._conn(app)
        if err:
            return err
        r = self.client.run(c["script_id"], APPS[app]["actions"][action][0], input_data, expect)
        r.error = redact(r.error, self.conns.secrets())
        r.data = scrub(r.data, self.conns.secrets())
        return r

    def slack_lookup_user(self, email):
        # fixed in code: search by email; the email comes from our directory, never from message text
        r = self._run("slack", "get_user", {"search_by": "email_search", "email": email, "response_fields": ["id", "name", "real_name"]})
        if not r.ok:
            return r
        d = r.data if isinstance(r.data, dict) else {}
        if d.get("message") == "User not found" or not d.get("id"):
            return Result(SUCCESS, data={"found": False}, code="not_found")
        return Result(SUCCESS, data={"found": True, "id": d.get("id"), "name": d.get("real_name") or d.get("name")})

    def slack_send(self, text):
        c, err = self._conn("slack")
        if err:
            return err
        channel = c["settings"].get("channel_id")
        if not channel:
            return Result(PERMANENT, error="No notification channel is chosen. Pick one on the Connections tab.", code="no_channel")
        r = self._run("slack", "send_message", {"destination": {"messageto": "channel", "channel_id": [channel]},
                                                "markdown_content": text[:3000], "bot_details": {"bot_name": "Deskhand"}}, _slack_sent_ok)
        if r.ok:  # keep only what we need; the raw reply carries ids we must not pass on
            r.data = {"ts": r.data.get("ts"), "channel": r.data.get("channel")}
        return r

    def slack_dm(self, user_id, text):
        """A direct message to one workspace member by Slack user id (from our directory, never from message text). UNVERIFIED until spike S1."""
        r = self._run("slack", "send_dm", {"userId": user_id, "content": text[:3000], "isScheduledMessage": "no", "bot_details": {"bot_name": "Deskhand"}}, _slack_sent_ok)
        if r.ok:
            r.data = {"ts": r.data.get("ts"), "channel": r.data.get("channel")}
        elif isinstance(r.data, str) and re.fullmatch(r"[a-z_]{3,40}", r.data):  # an unknown or deactivated user comes back as Slack's bare error word, e.g. channel_not_found
            r.error, r.code = f"Slack refused the message: {r.data}", r.data
        return r

    def approval_flow(self):
        row = self.conns.get("slack_flow")
        return row["script_id"] if row and row["script_id"] else None

    def slack_approval(self, user_id, text, token):
        """Start one run of the company's approval automation: it DMs the person with Approve and Reject buttons and waits for the click. UNVERIFIED end to end until spike S2."""
        flow = self.approval_flow()
        if not flow:
            return Result(PERMANENT, error="Slack buttons are not switched on yet. An admin does that on Integrations.", code="no_flow")
        r = self.client.webhook(flow, {"slack_user": user_id, "text": text[:2500], "token": token})
        r.error = redact(r.error, self.conns.secrets())
        r.data = scrub(r.data, self.conns.secrets())
        return r

    def create_approval_flow(self, callback_url):
        """Build the company's approval automation once: a webhook starts it, step one DMs the buttons and waits for the click, step two calls Deskhand back with the click.
        The callback address is written into the step code here, never taken from the request that starts a run."""
        c, err = self._conn("slack")
        if err:
            return err
        if not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?/api/hooks/slack-decision", callback_url or ""):
            return Result(PERMANENT, error="Slack buttons need the public https address of Deskhand (DESKHAND_BASE_URL).", code="no_public_url")
        tok = self.client.automation_token()
        if not tok.ok:
            return tok
        proj = os.environ.get("VIASOCKET_PROJECT_ID")
        extra = self.conns.secrets()
        old = self.approval_flow()
        if old:  # already built: switching the buttons on again only rewrites its steps (a new callback address, say), so there is never a second automation
            again = self.client.flow_call("steps", {"scriptId": old, "steps": _approval_steps(c["auth_id"], callback_url)}, tok.data, (old, *extra))
            if again.ok:
                return Result(SUCCESS, data={"created": False})
        made = self.client.flow_call("create", {"title": "Deskhand approvals", "description": "Sends an approval request to a person on Slack with Approve and Reject buttons and tells Deskhand which they pressed.", "projectId": proj}, tok.data, extra)
        flow = ((made.data or {}).get("script_id") if isinstance(made.data, dict) else None) if made.ok else None
        if not flow:
            return made if not made.ok else Result(PERMANENT, error="viaSocket created no automation", code="unexpected_body")
        trig = self.client.flow_call("trigger", {"scriptId": flow, "projectId": proj, "triggerType": "webhook"}, tok.data, (flow, *extra))
        if not trig.ok:  # one call at a time: a refused trigger must not be followed by the steps
            return trig
        steps = self.client.flow_call("steps", {"scriptId": flow, "steps": _approval_steps(c["auth_id"], callback_url)}, tok.data, (flow, *extra))
        if not steps.ok:
            return steps
        self.conns.s.q("INSERT INTO connections(tenant,app,auth_id,script_id,settings_json,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(tenant,app) DO UPDATE SET script_id=excluded.script_id, updated_at=excluded.updated_at",
                       (self.conns.tenant, "slack_flow", None, flow, "{}", time.time()))
        return Result(SUCCESS, data={"created": True})

    def gmail_send(self, to, subject, body):
        c, err = self._conn("gmail")
        if err:
            return err
        data = {"to": to, "subject": subject[:200], "messageBody": body}
        if c["settings"].get("from"):
            data["from"] = c["settings"]["from"]
        r = self._run("gmail", "send_email", data, _gmail_sent_ok)
        if r.ok:
            r.data = {"id": r.data.get("id"), "threadId": r.data.get("threadId")}
        return r


def _approval_steps(slack_auth_id, callback_url):
    ask = ('const b = context?.req?.body || {};\n'
           'return await sendInteractive({ inputData: { messageto: "user", userId: b.slack_user, content: b.text, operation: "Approval", '
           'buttons: { "Approve": "approve", "Reject": "reject" }, bot_details: { bot_name: "Deskhand" } } })')
    report = ('const b = context?.req?.body || {};\nconst a = context?.res?.ask || {};\nconst act = (a.actions && a.actions[0]) || {};\n'
              'const body = { token: b.token, slack_user: a.error ? "" : ((a.user && a.user.id) || ""), decision: a.error ? "error" : (act.value || "") };\n'
              f'const r = await axios.post("{callback_url}", body, {{ headers: {{ "Content-Type": "application/json" }}, timeout: 20000 }});\n'
              "console.log('### Deskhand answered HTTP ' + (r && r.status) + ' to the ' + (body.decision || 'empty') + ' click');\n"
              'return { status: r && r.status, answer: r && r.data };')
    return [{"stepname": "ask", "code": ask, "service_action_functions": [{"name": "sendInteractive", "auth_id": slack_auth_id, "action_id": INTERACTIVE_ACTION_ID}]},
            {"stepname": "report", "code": report, "service_action_functions": []}]


def personal_tenant(org_id, person_id):
    """The viaSocket identity of one person's own connections (their mailbox) inside a company."""
    return f"{org_id}:{person_id}"


def get_integration(store, tenant=None):
    return ViaSocketIntegration(store, tenant or os.environ.get("DESKHAND_TENANT_ID", "acme-demo"))
