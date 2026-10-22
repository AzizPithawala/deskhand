import contextlib
import os
import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chaos, delivery as dl, viasocket_adapter as vs, viasocket_ai as vsai
from .engine import Engine, InvalidTransition, NotAuthorized, Refused
from .db import Store

vs.load_dotenv()
app = FastAPI(title="Deskhand prototype")

# ---------------- Password gate for the hosted demo (Embarko has no login of its own) ----------------
# HTTP Basic, password from DESKHAND_DEMO_PASSWORD (any user name). Applies to EVERY route, including /api and /docs.
# Fails closed: when hosted (Embarko sets APP_NAME) or DESKHAND_REQUIRE_PASSWORD=on and no password is set, nothing is served.
_gate_failures = []


def _gate_required():
    return bool(os.environ.get("DESKHAND_DEMO_PASSWORD")) or bool(os.environ.get("APP_NAME")) or os.environ.get("DESKHAND_REQUIRE_PASSWORD") == "on"


@app.middleware("http")
async def password_gate(request, call_next):
    if not _gate_required():
        return await call_next(request)
    from fastapi.responses import PlainTextResponse
    import base64
    import hashlib
    import hmac
    import time
    pw = os.environ.get("DESKHAND_DEMO_PASSWORD", "")
    if not pw:
        return PlainTextResponse("Deskhand is locked: no demo password is configured.", status_code=503)
    now = time.time()
    _gate_failures[:] = [t for t in _gate_failures if now - t < 60]
    if len(_gate_failures) >= 20:  # crude brute-force brake: too many wrong tries in the last minute
        return PlainTextResponse("Too many attempts. Wait a minute.", status_code=429)
    given = ""
    h = request.headers.get("authorization", "")
    if h.lower().startswith("basic "):
        try:
            given = base64.b64decode(h[6:]).decode("utf-8", "ignore").partition(":")[2]
        except Exception:
            given = ""
    ok = hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(pw.encode()).digest())
    if not ok:
        if h:
            _gate_failures.append(now)
        return PlainTextResponse("Password required.", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Deskhand demo"'})
    return await call_next(request)

STATIC = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC), name="static")  # css, js, fonts; the password gate middleware covers it too
engine = Engine(Store(os.environ.get("DESKHAND_DB", "deskhand.db")))
_ai = vsai.ViaSocketClassifier(engine.s, os.environ.get("DESKHAND_TENANT_ID", "acme-demo"))
engine.ai_classify, engine.ai_enabled = _ai, _ai.enabled  # viaSocket AI understands requests only when an admin has switched it on; otherwise the rules do
engine.live = dl.Delivery(engine, tenant=os.environ.get("DESKHAND_TENANT_ID", "acme-demo"))  # live-only: delivery starts by itself once Slack and Gmail are set up


class Ingest(BaseModel):
    channel: str = "chat"
    sender: str
    text: str
    source_event_id: str | None = None
    is_bot: bool = False
    reply_to: int | None = None


class Act(BaseModel):
    actor_id: str
    reason: str | None = None
    note: str | None = None


class Hours(BaseModel):
    hours: float


def guard(fn, *a):
    try:
        return fn(*a)
    except Refused as e:
        raise HTTPException(422, str(e))
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    except NotAuthorized as e:
        raise HTTPException(403, str(e))


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/state")
def state():
    return {"employees": list(engine.d.employees.values()), "types": list(engine.d.types.values()), "now": engine.now(),
            "ai": "anthropic (key set)" if engine.api_key else "rules (no API key, simulated understanding)"}


@app.post("/api/ingest")
def ingest(b: Ingest):
    return engine.ingest(b.channel, b.sender, b.text, b.source_event_id, b.is_bot, b.reply_to)


@app.get("/api/requests")
def requests(status: str | None = None, employee: str | None = None):
    return engine.list_requests(status, employee)


@app.get("/api/requests/{rid}")
def request_detail(rid: int):
    r = engine.req(rid)
    if not r:
        raise HTTPException(404, "not found")
    r["status_text"] = engine.status_text(r)
    return {"request": r, "timeline": engine.timeline(rid)}


@app.post("/api/requests/{rid}/approve")
def approve(rid: int, b: Act):
    return guard(engine.approve, rid, b.actor_id)


@app.post("/api/requests/{rid}/reject")
def reject(rid: int, b: Act):
    return guard(engine.reject, rid, b.actor_id, b.reason or "")


@app.post("/api/requests/{rid}/pending")
def pending(rid: int, b: Act):
    return guard(engine.mark_pending, rid, b.actor_id, b.note or "")


@app.post("/api/requests/{rid}/done")
def done(rid: int, b: Act):
    return guard(engine.done, rid, b.actor_id)


@app.post("/api/requests/{rid}/cancel")
def cancel(rid: int, b: Act):
    return guard(engine.cancel, rid, b.actor_id)


@app.post("/api/requests/{rid}/replay")
def replay(rid: int, b: Act):
    return guard(engine.replay, rid, b.actor_id)


@app.get("/api/inbox/{pid}")
def inbox(pid: str):
    return engine.inbox(pid)


@app.get("/api/outbox")
def outbox(to: str | None = None, limit: int = 100):
    sql, args = "SELECT * FROM outbox", []
    if to:
        sql += " WHERE to_person=?"
        args.append(to)
    return engine.s.all(sql + " ORDER BY id DESC LIMIT ?", args + [limit])


@app.get("/api/audit")
def audit(limit: int = 150):
    return engine.s.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/dashboard")
def dashboard():
    return engine.dashboard()


@app.get("/api/digest/{pid}")
def digest(pid: str):
    return engine.digest(pid)


@app.post("/api/clock/advance")
def advance(b: Hours):
    engine.advance_hours(b.hours)
    return {"now": engine.now(), "sweep": engine.sweep()}


@app.post("/api/sweep")
def sweep():
    return engine.sweep()


@app.get("/api/clock")
def clock():
    d = engine.live.summary()
    banners = list(d["problems"]) + ([f"Delivery paused: the daily cap of {d['cap']} messages is reached ({d['used_today']}). Messages stay in the outbox until tomorrow (UTC)."] if d["capped"] else [])
    return {"now": engine.now(), "banners": banners, "delivery": {"used_today": d["used_today"], "cap": d["cap"], "capped": d["capped"]}}


@app.post("/api/chaos/replay/{rid}")
def chaos_replay(rid: int):
    with dl.forced_mock(engine):
        return guard(chaos.replay_parked, engine, rid)


@app.post("/api/chaos/{name}")
def chaos_run(name: str):
    if name not in chaos.SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    with dl.forced_mock(engine):  # chaos never sends anything live
        return chaos.SCENARIOS[name](engine)


@app.post("/api/admin/breaker_reset")
def breaker_reset():
    admin = engine.d.role_holder("admin")
    engine.reset_breaker(admin["id"])
    return {"ok": True}


@app.post("/api/reset")
def reset():
    with dl.forced_mock(engine):
        engine.reset()  # connections, their settings and the daily cap ledger survive a reset on purpose
    return {"ok": True}


# ---------------- Connections (viaSocket). Admin only. auth_id and script_id never leave the server. ----------------
TENANT = os.environ.get("DESKHAND_TENANT_ID", "acme-demo")


def _admin(actor_id):
    if not engine._is_admin(actor_id):
        engine.audit(None, actor_id or "unknown", "unauthorized_action_refused", {"action": "connections"})
        raise HTTPException(403, "only an admin can manage connections")


def _real():
    """The connection screens talk to viaSocket for real; they need the embed values set on the server."""
    client = vs.VsClient(TENANT)
    if not client.configured():
        raise HTTPException(409, "viaSocket is not configured on the server. See the README.")
    return client, vs.ConnectionStore(engine.s, TENANT)


def _fail(r):
    raise HTTPException(422 if r.kind == vs.PERMANENT else 503, vs.redact(r.error or "viaSocket call failed"))


class ConnAct(BaseModel):
    actor_id: str
    app: str | None = None
    auth_id: str | None = None
    setting: str | None = None
    value: str | None = None
    action: str | None = None
    email: str | None = None


def _app(key):
    if key not in vs.APPS:
        raise HTTPException(404, "unknown app")
    return vs.APPS[key]


@app.get("/api/connections")
def connections(actor_id: str):
    _admin(actor_id)
    client, store = vs.VsClient(TENANT), vs.ConnectionStore(engine.s, TENANT)
    live, note = {}, ""
    if client.configured():
        r = client.connections()
        if r.ok:
            live = {str(c.get("id")): c for c in r.data if isinstance(c, dict)}
        else:
            note = vs.redact(r.error, store.secrets())
    apps = []
    for key, a in vs.APPS.items():
        row = store.get(key)
        entry = live.get(row["auth_id"]) if row else None
        status = "connected" if row else "not_connected"
        if row and live and not entry:
            status = "unknown"
        if entry and entry.get("isExpired"):
            status = "expired"
        apps.append({"key": key, "name": a["name"], "icon": a["icon"], "status": status, "can_run": bool(row and row["script_id"]),
                     "settings": [{"key": st["key"], "label": st["label"], "kind": st["kind"], "required": st["required"],
                                   "value": (row["settings"].get(st["key"]) if row else None) or ""} for st in a["settings"]]})
    return {"configured": client.configured(), "tenant": TENANT, "connect_js": vs.CONNECT_JS, "apps": apps, "note": note, "problems": engine.live.problems()}


@app.get("/api/connections/token")
def connections_token(actor_id: str):
    _admin(actor_id)
    client, _ = _real()
    return {"token": client.token()}  # for the Connect popup only; the secret stays here


@app.get("/api/connections/service/{key}")
def connections_service(key: str, actor_id: str):
    _admin(actor_id)
    a = _app(key)
    return {"service_id": a["service_id"], "action_ids": [v[1] for v in a["actions"].values()]}


@app.post("/api/connections/connected")
def connection_connected(b: ConnAct):
    _admin(b.actor_id)
    a = _app(b.app or "")
    client, store = _real()
    if not b.auth_id:
        raise HTTPException(422, "no connection id was returned")
    r = client.connections()
    if not r.ok:
        _fail(r)
    if str(b.auth_id) not in {str(c.get("id")) for c in r.data if isinstance(c, dict)}:
        raise HTTPException(422, "that connection does not belong to this company")  # a forged or stale id
    en = client.enable(a["service_id"], b.auth_id)
    if not en.ok:
        _fail(en)
    store.save(b.app, b.auth_id, en.data)
    engine.audit(None, b.actor_id, "connection_added", {"app": b.app, "reused_runner": en.meta.get("reused")})
    return {"ok": True}


@app.get("/api/connections/{key}/options")
def connection_options(key: str, actor_id: str, setting: str):
    _admin(actor_id)
    a = _app(key)
    st = next((x for x in a["settings"] if x["key"] == setting and x["kind"] == "picker"), None)
    if not st:
        raise HTTPException(404, "no such picker")
    client, store = _real()
    row = store.get(key)
    if not row:
        raise HTTPException(409, f"{a['name']} is not connected yet")
    r = client.options(a["actions"][st["action"]][0], st["field"], row["auth_id"], st["existing"])
    if not r.ok:
        raise HTTPException(422 if r.kind == vs.PERMANENT else 503, vs.redact(r.error, store.secrets()))
    return r.data


@app.post("/api/connections/{key}/setting")
def connection_setting(key: str, b: ConnAct):
    _admin(b.actor_id)
    a = _app(key)
    st = next((x for x in a["settings"] if x["key"] == b.setting), None)
    if not st:
        raise HTTPException(404, "no such setting")
    store = vs.ConnectionStore(engine.s, TENANT)
    if not store.get(key):
        raise HTTPException(409, f"{a['name']} is not connected yet")
    value = (b.value or "").strip()
    if st["kind"] == "email" and not dl.ONE_EMAIL.match(value):
        raise HTTPException(422, "enter exactly one email address")
    if st["required"] and not value:
        raise HTTPException(422, f"{st['label']} is required")
    store.set_setting(key, st["key"], value)
    engine.audit(None, b.actor_id, "connection_setting_changed", {"app": key, "setting": st["key"]})  # never the value: it may be an address
    return {"ok": True}


@app.post("/api/connections/{key}/disconnect")
def connection_disconnect(key: str, b: ConnAct):
    _admin(b.actor_id)
    _app(key)
    client, store = _real()
    row = store.get(key)
    if row:
        client.api("PUT", f"/embed/updatestatus/{row['script_id']}?status=0")  # remove the runner first, then the connection
        r = client.revoke(row["auth_id"])
        if not r.ok:
            raise HTTPException(422, vs.redact(r.error, store.secrets()))
        store.delete(key)
        engine.audit(None, b.actor_id, "connection_removed", {"app": key})
    return {"ok": True}


EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


@app.post("/api/connections/test")
def connection_test(b: ConnAct):
    """Admin-triggered proof calls. Texts are fixed in code. The test email goes only to the stored notification address."""
    _admin(b.actor_id)
    integ = vs.get_integration(engine.s, TENANT)
    if b.action == "slack_lookup":
        if not EMAIL.match((b.email or "").strip()):
            raise HTTPException(422, "enter one valid email address")
        r = integ.slack_lookup_user(b.email.strip())
    elif b.action == "slack_send":
        r = integ.slack_send("Deskhand connection test: this channel will receive request notifications.")
    elif b.action == "gmail_send":
        to = engine.live.notify_email()
        if not to:
            raise HTTPException(409, "Set the notification email first.")
        r = integ.gmail_send(to, "Deskhand connection test", "<p>This is a test email from Deskhand. Nothing else was done.</p>")
    else:
        raise HTTPException(404, "unknown test")
    engine.audit(None, b.actor_id, "integration_test", {"action": b.action, "outcome": r.kind, "code": r.code})
    return {"outcome": r.kind, "code": r.code, "error": vs.redact(r.error), "data": vs.scrub(r.data)}


# ---------------- Delivery status (no switch: delivery runs by itself once Slack and Gmail are set up) ----------------
def _mask(addr):
    return (addr[0] + "***@" + addr.split("@", 1)[1]) if addr else ""


@app.get("/api/delivery")
def delivery_status(actor_id: str):
    _admin(actor_id)
    failed = engine.s.all("SELECT id, request_id, kind, channel, delivery_error FROM outbox WHERE delivery_status='failed' ORDER BY id DESC LIMIT 20")
    return {**engine.live.summary(), "email_to": _mask(engine.live.notify_email()), "base_url": engine.live.base_url, "failed": failed}


@app.post("/api/delivery/sweep")
def delivery_sweep(b: Act):
    _admin(b.actor_id)
    return engine.live.deliver_pending()


@app.post("/api/delivery/retry/{oid}")
def delivery_retry(oid: int, b: Act):
    _admin(b.actor_id)
    try:
        engine.live.retry_failed(oid, b.actor_id)
    except ValueError as ex:
        raise HTTPException(409, str(ex))
    return engine.live.deliver_pending()


def _worker():
    import threading
    import time

    def loop():
        while True:
            time.sleep(10)
            try:
                engine.live.deliver_pending()
            except Exception:  # the worker must never die or log message text
                pass

    threading.Thread(target=loop, daemon=True, name="delivery-worker").start()


@contextlib.asynccontextmanager
async def _lifespan(_app):
    if os.environ.get("DESKHAND_DELIVERY_WORKER", "on") != "off":
        _worker()
    yield


app.router.lifespan_context = _lifespan


# ---------------- Understanding requests with viaSocket AI (admin switch, off by default) ----------------
class AISet(BaseModel):
    actor_id: str
    enabled: bool


@app.get("/api/ai")
def ai_status(actor_id: str):
    _admin(actor_id)
    configured = vs.VsClient(TENANT).configured()
    return {"enabled": _ai.enabled(), "switch": vs.ConnectionStore(engine.s, TENANT).ai_enabled(), "configured": configured,
            "calls_today": int(engine.s.get_setting("ai_calls_today", 0)), "budget": int(engine.s.get_setting("ai_budget_per_day", 0)), "timeout_s": vsai.TIMEOUT_S}


@app.post("/api/ai")
def ai_set(b: AISet):
    _admin(b.actor_id)
    if b.enabled and not vs.VsClient(TENANT).configured():
        raise HTTPException(409, "viaSocket is not configured on the server. See the README.")
    vs.ConnectionStore(engine.s, TENANT).set_ai_enabled(b.enabled)
    engine.audit(None, b.actor_id, "ai_setting_changed", {"enabled": b.enabled})
    return {"ok": True, "enabled": _ai.enabled()}
