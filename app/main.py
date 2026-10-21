import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import chaos
from .engine import Engine, InvalidTransition, NotAuthorized, Refused
from .db import Store

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
