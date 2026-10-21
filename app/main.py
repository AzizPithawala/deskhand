import os
import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import chaos, viasocket_adapter as vs
from .engine import Engine, InvalidTransition, NotAuthorized, Refused
from .db import Store

vs.load_dotenv()
app = FastAPI(title="Deskhand prototype")
STATIC = os.path.join(os.path.dirname(__file__), "static")
engine = Engine(Store(os.environ.get("DESKHAND_DB", "deskhand.db")))


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
    return {"now": engine.now()}


@app.post("/api/chaos/replay/{rid}")
def chaos_replay(rid: int):
    return guard(chaos.replay_parked, engine, rid)


@app.post("/api/chaos/{name}")
def chaos_run(name: str):
    if name not in chaos.SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    return chaos.SCENARIOS[name](engine)


@app.post("/api/admin/breaker_reset")
def breaker_reset():
    admin = engine.d.role_holder("admin")
    engine.reset_breaker(admin["id"])
    return {"ok": True}


@app.post("/api/reset")
def reset():
    engine.reset()
    return {"ok": True}


# ---------------- Connections (viaSocket). Admin only. auth_id and script_id never leave the server. ----------------
TENANT = os.environ.get("DESKHAND_TENANT_ID", "acme-demo")


def _admin(actor_id):
    if not engine._is_admin(actor_id):
        engine.audit(None, actor_id or "unknown", "unauthorized_action_refused", {"action": "connections"})
        raise HTTPException(403, "only an admin can manage connections")


def _real():
    """The connection screens talk to viaSocket for real; they need the mode switched on and the embed values set."""
    client = vs.VsClient(TENANT)
    if os.environ.get("DESKHAND_INTEGRATION", "mock").lower() != "viasocket":
        raise HTTPException(409, "Integration is in mock mode. Start the app with DESKHAND_INTEGRATION=viasocket to connect real apps.")
    if not client.configured():
        raise HTTPException(409, "viaSocket is not configured. Set VIASOCKET_ORG_ID, VIASOCKET_PROJECT_ID and VIASOCKET_EMBED_SECRET in .env.")
    return client, vs.ConnectionStore(engine.s, TENANT)


def _fail(r):
    raise HTTPException(422 if r.kind == vs.PERMANENT else 503, vs.redact(r.error or "viaSocket call failed"))


class ConnAct(BaseModel):
    actor_id: str
    app: str | None = None
    auth_id: str | None = None
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
    mode = os.environ.get("DESKHAND_INTEGRATION", "mock").lower()
    client, store = vs.VsClient(TENANT), vs.ConnectionStore(engine.s, TENANT)
    live, note = {}, ""
    if mode == "viasocket" and client.configured():
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
        st = a["setting"]
        apps.append({"key": key, "name": a["name"], "icon": a["icon"], "status": status, "can_run": bool(row and row["script_id"]),
                     "setting": {"label": st["label"], "required": st["required"], "value": (row["settings"].get(st["key"]) if row else None)}})
    return {"mode": mode, "configured": client.configured(), "tenant": TENANT, "connect_js": vs.CONNECT_JS, "apps": apps, "note": note}


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
def connection_options(key: str, actor_id: str):
    _admin(actor_id)
    a = _app(key)
    client, store = _real()
    row = store.get(key)
    if not row:
        raise HTTPException(409, f"{a['name']} is not connected yet")
    st = a["setting"]
    r = client.options(a["actions"][st["action"]][0], st["field"], row["auth_id"], st["existing"])
    if not r.ok:
        raise HTTPException(422 if r.kind == vs.PERMANENT else 503, vs.redact(r.error, store.secrets()))
    return r.data


@app.post("/api/connections/{key}/setting")
def connection_setting(key: str, b: ConnAct):
    _admin(b.actor_id)
    a = _app(key)
    _, store = _real()
    if not store.get(key):
        raise HTTPException(409, f"{a['name']} is not connected yet")
    store.set_setting(key, a["setting"]["key"], b.value or "")
    engine.audit(None, b.actor_id, "connection_setting_changed", {"app": key, "setting": a["setting"]["key"]})
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
    """Admin-triggered proof calls. Texts are fixed in code; only the address is typed by the admin."""
    _admin(b.actor_id)
    integ = vs.get_integration(engine.s, TENANT)
    if b.action in ("slack_lookup", "gmail_send") and not EMAIL.match((b.email or "").strip()):
        raise HTTPException(422, "enter one valid email address")
    if b.action == "slack_lookup":
        r = integ.slack_lookup_user(b.email.strip())
    elif b.action == "slack_send":
        r = integ.slack_send("Deskhand connection test: this channel will receive request notifications.")
    elif b.action == "gmail_send":
        r = integ.gmail_send(b.email.strip(), "Deskhand connection test", "<p>This is a test email from Deskhand. Nothing else was done.</p>")
    else:
        raise HTTPException(404, "unknown test")
    engine.audit(None, b.actor_id, "integration_test", {"action": b.action, "mode": integ.mode, "outcome": r.kind, "code": r.code})
    return {"mode": integ.mode, "outcome": r.kind, "code": r.code, "error": vs.redact(r.error), "data": vs.scrub(r.data)}
