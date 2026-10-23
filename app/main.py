"""Deskhand web app. Identity comes from the signed-in session (never from the request body); each company has its own engine and
database file; admin, demo-only and visibility rules are enforced here, in code."""
import contextlib
import hmac
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from . import accounts as ac, chaos, delivery as dl, setup as st, viasocket_adapter as vs, viasocket_ai as vsai
from . import notifications as nt, slack_decisions as sd, steps as sp, viasocket_catalog as vcat
from .ledger import num
from .engine import InvalidTransition, NotAuthorized, Refused
from .registry import DEMO_ORG_ID, open_platform

vs.load_dotenv()
DATA_DIR = os.environ.get("DESKHAND_DATA_DIR", ".deskhand")
SIGNUP_CODES = [c.strip() for c in os.environ.get("DESKHAND_SIGNUP_CODES", "").split(",") if c.strip()]
accounts, registry = open_platform(DATA_DIR, SIGNUP_CODES)
COOKIE = "dh_session"
log = logging.getLogger("uvicorn.error")  # shows in the hosted log next to the access lines

app = FastAPI(title="Deskhand", docs_url=None, redoc_url=None, openapi_url=None)
STATIC = os.path.join(os.path.dirname(__file__), "static")

# ---------------- Optional shared password for a private demo deployment (off unless DESKHAND_DEMO_PASSWORD is set) ----------------
_gate_failures = []


@app.middleware("http")
async def password_gate(request, call_next):
    pw = os.environ.get("DESKHAND_DEMO_PASSWORD", "")
    if not pw:
        return await call_next(request)
    import base64
    import hashlib
    from fastapi.responses import PlainTextResponse
    if request.url.path == "/api/hooks/slack-decision":  # viaSocket calls this: it is protected by a one-time token and the clicker's Slack identity, not by the shared password
        return await call_next(request)
    now = time.time()
    _gate_failures[:] = [t for t in _gate_failures if now - t < 60]
    if len(_gate_failures) >= 20:
        return PlainTextResponse("Too many attempts. Wait a minute.", status_code=429)
    given = ""
    h = request.headers.get("authorization", "")
    if h.lower().startswith("basic "):
        try:
            given = base64.b64decode(h[6:]).decode("utf-8", "ignore").partition(":")[2]
        except Exception:
            given = ""
    if not hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(pw.encode()).digest()):
        if h:
            _gate_failures.append(now)
        if h or request.cookies.get(COOKIE):  # a browser that had signed in was refused by the shared password, which is not a session problem
            log.warning("gate refused %s %s: %s", request.method, request.url.path, "wrong_password" if h else "no_authorization_header")
        return PlainTextResponse("Password required.", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Deskhand"'})
    return await call_next(request)


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


# ---------------- Who is asking ----------------
@dataclass
class Ctx:
    user: dict
    org: dict
    engine: object
    csrf: str
    token: str

    @property
    def uid(self):
        return self.user["id"]

    @property
    def is_admin(self):
        return self.user["access_role"] in ("owner", "admin")

    @property
    def is_demo(self):
        return bool(self.org["is_demo"])


def _ip(request):
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else ""))[:64]


def auth(request: Request) -> Ctx:
    token = request.cookies.get(COOKIE)
    sess, why = accounts.session_state(token)
    if not sess:
        if request.url.path != "/api/auth/me":  # that one is how a signed-out visitor finds out; anything else refused is a signed-in browser losing its session
            log.warning("session refused: %s on %s %s", why, request.method, request.url.path)  # a fixed word and the path, never the cookie
        raise HTTPException(401, "Sign in to continue.")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), sess["csrf"]):
        raise HTTPException(403, "This request did not come from the Deskhand page. Reload and try again.")
    try:
        eng = registry.engine_for(sess["org"]["id"])
    except KeyError:
        log.warning("session refused: company_unavailable on %s %s", request.method, request.url.path)
        raise HTTPException(401, "Sign in to continue.")
    if not eng.d.emp(sess["user"]["id"]) and not sess["org"]["is_demo"]:
        registry.refresh_directory(sess["org"]["id"])
    return Ctx(sess["user"], sess["org"], eng, sess["csrf"], token)


def _refuse(ctx, what):
    ctx.engine.audit(None, ctx.uid, "unauthorized_action_refused", {"action": what})
    raise HTTPException(403, "You do not have access to this.")


def admin(ctx: Ctx = Depends(auth)) -> Ctx:
    if not ctx.is_admin:
        _refuse(ctx, "admin area")
    return ctx


def real_admin(ctx: Ctx = Depends(admin)) -> Ctx:
    if ctx.is_demo:
        raise HTTPException(403, "This is switched off in the demo company.")
    return ctx


def demo_only(ctx: Ctx = Depends(auth)) -> Ctx:
    if not ctx.is_demo:
        _refuse(ctx, "demo-only tool")
    return ctx


_limits = {}


def _limit(key, n, window):
    now = time.time()
    hits = [t for t in _limits.get(key, []) if now - t < window]
    if len(hits) >= n:
        raise HTTPException(429, "Too many attempts. Try again later.")
    hits.append(now)
    _limits[key] = hits


def _cookie(resp, request, token):
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=secure, max_age=ac.SESSION_ABSOLUTE_S, path="/")


def _me(sess_ctx):
    u = sess_ctx.user
    return {"user": {k: u.get(k) for k in ("id", "name", "email", "access_role", "department", "manager_id")},
            "org": {"id": sess_ctx.org["id"], "name": sess_ctx.org["name"], "is_demo": bool(sess_ctx.org["is_demo"])},
            "csrf": sess_ctx.csrf, "is_admin": sess_ctx.is_admin}


def _signed_in(request, user):
    token, csrf = accounts.create_session(user, _ip(request), request.headers.get("user-agent"))
    sess = accounts.get_session(token)
    resp = JSONResponse(_me(Ctx(sess["user"], sess["org"], None, csrf, token)))
    _cookie(resp, request, token)
    return resp


def _fail(e: ac.AccountError):
    return JSONResponse({"detail": str(e)}, status_code=e.status)


# ---------------- Sign in, register, join, demo ----------------
class Register(BaseModel):
    company: str
    name: str
    email: str
    password: str
    code: str
    timezone: str | None = None


class Login(BaseModel):
    email: str
    password: str


class Join(BaseModel):
    token: str
    password: str


class ChangePassword(BaseModel):
    current: str
    new: str


class ActAs(BaseModel):
    person_id: str


@app.get("/api/auth/options")
def auth_options():
    return {"registration_open": bool(SIGNUP_CODES), "demo_available": bool(accounts.get_org(DEMO_ORG_ID))}


@app.post("/api/auth/register")
def register(b: Register, request: Request):
    _limit("register:" + _ip(request), 10, 3600)
    if not SIGNUP_CODES:
        raise HTTPException(403, "Registration is closed. Ask us for an invite code.")
    try:
        org, user = accounts.register_company(b.company, b.name, b.email, b.password, b.code, b.timezone)
    except ac.AccountError as e:
        return _fail(e)
    registry.engine_for(org["id"]).audit(None, user["id"], "company_registered", {})
    return _signed_in(request, user)


@app.post("/api/auth/login")
def login(b: Login, request: Request):
    _limit("login:" + _ip(request), 60, 600)
    try:
        user = accounts.authenticate(b.email, b.password, _ip(request))
    except ac.AccountError as e:
        return _fail(e)
    return _signed_in(request, user)


@app.post("/api/auth/logout")
def logout(request: Request, ctx: Ctx = Depends(auth)):
    accounts.end_session(ctx.token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def me(ctx: Ctx = Depends(auth)):
    return _me(ctx)


@app.get("/api/auth/invite/{token}")
def invite_info(token: str):
    info = accounts.peek_invite(token)
    if not info:
        raise HTTPException(410, "This link has expired or was already used. Ask your admin for a new one.")
    return info


@app.post("/api/auth/join")
def join(b: Join, request: Request):
    _limit("join:" + _ip(request), 20, 3600)
    try:
        user = accounts.accept_invite(b.token, b.password, _ip(request))
    except ac.AccountError as e:
        return _fail(e)
    registry.refresh_directory(user["org_id"])
    return _signed_in(request, user)


@app.post("/api/auth/password")
def change_password(b: ChangePassword, request: Request, ctx: Ctx = Depends(auth)):
    if ctx.is_demo:
        raise HTTPException(403, "This is switched off in the demo company.")
    u = accounts.get_user(ctx.uid)
    if not ac.verify_password(b.current, u["password_hash"] or ""):
        raise HTTPException(401, "The current password is not right.")
    try:
        accounts.set_password(ctx.uid, b.new)  # ends every session, so sign this one in again
    except ac.AccountError as e:
        return _fail(e)
    return _signed_in(request, accounts.get_user(ctx.uid))


@app.post("/api/demo/enter")
def demo_enter(request: Request, b: ActAs | None = None):
    _limit("demo:" + _ip(request), 60, 3600)
    person = (b.person_id if b else None) or "e01"
    user = accounts.get_user(person)
    if not user or user["org_id"] != DEMO_ORG_ID or user["status"] != "demo":
        raise HTTPException(404, "Unknown demo person.")
    return _signed_in(request, user)


@app.post("/api/demo/act-as")
def demo_act_as(b: ActAs, ctx: Ctx = Depends(demo_only)):
    try:
        accounts.switch_demo_user(ctx.token, b.person_id)
    except ac.AccountError as e:
        return _fail(e)
    return _me(Ctx(accounts.get_user(b.person_id), ctx.org, ctx.engine, ctx.csrf, ctx.token))


# ---------------- Requests ----------------
class Ingest(BaseModel):
    text: str
    source_event_id: str | None = None
    reply_to: int | None = None


class Act(BaseModel):
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


def _visible(ctx, r):
    """Employees see their own requests and the ones assigned to them; admins see all."""
    return (ctx.is_admin or r["employee_id"] == ctx.uid or ctx.uid in (r.get("chain") or [])
            or r.get("escalated_to") == ctx.uid or r.get("fulfiller_id") == ctx.uid)


def _get_visible(ctx, rid):
    r = ctx.engine.req(rid)
    if not r or not _visible(ctx, r):
        raise HTTPException(404, "Not found.")
    return r


@app.get("/api/state")
def state(ctx: Ctx = Depends(auth)):
    people = list(ctx.engine.d.employees.values())
    if not ctx.is_admin:  # colleagues' contact details are for admins
        people = [{k: p.get(k) for k in ("id", "name", "role", "roles", "department", "manager_id", "active")} for p in people]
    hr = ctx.engine.hr
    return {"employees": people, "types": list(ctx.engine.d.types.values()), "now": ctx.engine.now(), "hr_label": hr.label, "hr_applied": hr.applied}


@app.post("/api/ingest")
def ingest(b: Ingest, ctx: Ctx = Depends(auth)):
    return ctx.engine.ingest("chat", ctx.uid, b.text, b.source_event_id, False, b.reply_to)  # the sender is the signed-in person, never the body


@app.get("/api/requests")
def requests(status: str | None = None, employee: str | None = None, ctx: Ctx = Depends(auth)):
    if employee and employee != ctx.uid and not ctx.is_admin:
        raise HTTPException(403, "You can only list your own requests.")
    return [r for r in ctx.engine.list_requests(status, employee) if _visible(ctx, r)]


@app.get("/api/requests/{rid}")
def request_detail(rid: int, ctx: Ctx = Depends(auth)):
    r = _get_visible(ctx, rid)
    r["status_text"] = ctx.engine.status_text(r)
    return {"request": r, "timeline": ctx.engine.timeline(rid), "steps": ctx.engine.stepper.view(rid, with_input=ctx.is_admin)}


@app.post("/api/requests/{rid}/approve")
def approve(rid: int, ctx: Ctx = Depends(auth)):
    _get_visible(ctx, rid)
    return guard(ctx.engine.approve, rid, ctx.uid)


@app.post("/api/requests/{rid}/reject")
def reject(rid: int, b: Act, ctx: Ctx = Depends(auth)):
    _get_visible(ctx, rid)
    return guard(ctx.engine.reject, rid, ctx.uid, b.reason or "")


@app.post("/api/requests/{rid}/pending")
def pending(rid: int, b: Act, ctx: Ctx = Depends(auth)):
    _get_visible(ctx, rid)
    return guard(ctx.engine.mark_pending, rid, ctx.uid, b.note or "")


@app.post("/api/requests/{rid}/done")
def done(rid: int, ctx: Ctx = Depends(auth)):
    _get_visible(ctx, rid)
    return guard(ctx.engine.done, rid, ctx.uid)


@app.post("/api/requests/{rid}/cancel")
def cancel(rid: int, ctx: Ctx = Depends(auth)):
    _get_visible(ctx, rid)
    return guard(ctx.engine.cancel, rid, ctx.uid)


@app.post("/api/requests/{rid}/replay")
def replay(rid: int, ctx: Ctx = Depends(auth)):
    _get_visible(ctx, rid)
    return guard(ctx.engine.replay, rid, ctx.uid)


class StepAct(BaseModel):
    reason: str | None = None


@app.post("/api/requests/{rid}/steps/{pos}/run")
def step_run(rid: int, pos: int, ctx: Ctx = Depends(auth)):
    _get_visible(ctx, rid)
    return guard(ctx.engine.stepper.confirm, rid, pos, ctx.uid)


@app.post("/api/requests/{rid}/steps/{pos}/again")
def step_again(rid: int, pos: int, ctx: Ctx = Depends(auth)):
    _get_visible(ctx, rid)
    return guard(ctx.engine.stepper.replay, rid, ctx.uid, pos, "again")


@app.post("/api/requests/{rid}/steps/{pos}/mark-done")
def step_mark_done(rid: int, pos: int, b: StepAct, ctx: Ctx = Depends(auth)):
    _get_visible(ctx, rid)
    return guard(ctx.engine.stepper.replay, rid, ctx.uid, pos, "mark_done", b.reason or "")


@app.get("/api/inbox")
def inbox(ctx: Ctx = Depends(auth)):
    return ctx.engine.inbox(ctx.uid)


@app.get("/api/outbox")
def outbox(limit: int = 100, ctx: Ctx = Depends(auth)):
    cols = "id, request_id, channel, to_person, subject, body, kind, idempotency_key, status, created_at, delivery_status, delivery_attempts, delivery_error, delivery_next_at, delivered_at, delivery_note, from_person, read_at, sent_to"  # not email_json: the stored layout data is for the sender
    return ctx.engine.s.all(f"SELECT {cols} FROM outbox WHERE to_person=? ORDER BY id DESC LIMIT ?", (ctx.uid, min(max(limit, 1), 500)))


@app.get("/api/audit")
def audit(limit: int = 150, ctx: Ctx = Depends(admin)):
    return ctx.engine.s.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 1000),))


@app.get("/api/dashboard")
def dashboard(ctx: Ctx = Depends(admin)):
    return ctx.engine.dashboard()


@app.get("/api/digest")
def digest(ctx: Ctx = Depends(auth)):
    return ctx.engine.digest(ctx.uid)


@app.get("/api/clock")
def clock(ctx: Ctx = Depends(auth)):
    d = ctx.engine.live.summary()
    banners = [] if (ctx.is_demo or not ctx.is_admin) else list(d["problems"]) + (
        [f"Delivery paused: the daily cap of {d['cap']} messages is reached ({d['used_today']}). Messages stay in the outbox until tomorrow (UTC)."] if d["capped"] else [])
    return {"now": ctx.engine.now(), "banners": banners, "notes": d["notes"] if (ctx.is_admin and not ctx.is_demo) else [], "demo": ctx.is_demo, "mode": d["mode"],
            "unread": nt.unread_count(ctx.engine, ctx.uid), "delivery": {"used_today": d["used_today"], "cap": d["cap"], "capped": d["capped"]}}


@app.post("/api/admin/breaker_reset")
def breaker_reset(ctx: Ctx = Depends(admin)):
    ctx.engine.reset_breaker(ctx.uid)
    return {"ok": True}


# ---------------- Demo-only tools (they would corrupt a real company) ----------------
@app.post("/api/clock/advance")
def advance(b: Hours, ctx: Ctx = Depends(demo_only)):
    ctx.engine.advance_hours(b.hours)
    return {"now": ctx.engine.now(), "sweep": ctx.engine.sweep()}


@app.post("/api/chaos/replay/{rid}")
def chaos_replay(rid: int, ctx: Ctx = Depends(demo_only)):
    with dl.forced_mock(ctx.engine):
        return guard(chaos.replay_parked, ctx.engine, rid)


@app.post("/api/chaos/{name}")
def chaos_run(name: str, ctx: Ctx = Depends(demo_only)):
    if name not in chaos.SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    with dl.forced_mock(ctx.engine):  # chaos never sends anything live
        return chaos.SCENARIOS[name](ctx.engine)


@app.post("/api/reset")
def reset(ctx: Ctx = Depends(demo_only)):
    with dl.forced_mock(ctx.engine):
        ctx.engine.reset()  # connections, their settings and the daily cap ledger survive a reset on purpose
    return {"ok": True}


# ---------------- People (admin) ----------------
class Invite(BaseModel):
    email: str
    name: str
    access_role: str = "member"
    department: str | None = None
    manager_id: str | None = None


class PersonEdit(BaseModel):
    name: str | None = None
    department: str | None = None
    manager_id: str | None = None
    access_role: str | None = None
    status: str | None = None
    slack_id: str | None = None


@app.get("/api/people")
def people(ctx: Ctx = Depends(admin)):
    held = {}
    for r in st.list_roles(ctx.engine.d):
        if r["holder_id"]:
            held.setdefault(r["holder_id"], []).append(r["name"])
    return [{**u, "roles": held.get(u["id"], [])} for u in accounts.list_users(ctx.org["id"])]


@app.post("/api/people/invite")
def invite(b: Invite, ctx: Ctx = Depends(real_admin)):
    try:
        user, token = accounts.create_invite(ctx.org["id"], b.email, b.name, b.access_role, ctx.uid, b.department, b.manager_id)
    except ac.AccountError as e:
        return _fail(e)
    registry.refresh_directory(ctx.org["id"])
    ctx.engine.audit(None, ctx.uid, "person_invited", {"user": user["id"]})
    return {"user": user, "join_token": token}  # the raw token is shown once; only its hash is stored


@app.post("/api/people/{uid}")
def edit_person(uid: str, b: PersonEdit, ctx: Ctx = Depends(real_admin)):
    try:
        user = accounts.update_user(ctx.org["id"], uid, ctx.uid, **b.model_dump(exclude_unset=True))
    except ac.AccountError as e:
        return _fail(e)
    registry.refresh_directory(ctx.org["id"])
    ctx.engine.audit(None, ctx.uid, "person_updated", {"user": uid, "fields": sorted(b.model_dump(exclude_unset=True))})
    return user


@app.post("/api/people/{uid}/link")
def person_link(uid: str, ctx: Ctx = Depends(real_admin)):
    try:
        token = accounts.create_reset_link(ctx.org["id"], uid, ctx.uid)
    except ac.AccountError as e:
        return _fail(e)
    ctx.engine.audit(None, ctx.uid, "reset_link_created", {"user": uid})
    return {"join_token": token}


# ---------------- Company setup (admin). Reads work in the demo company (read-only); every write is validated, audited and refused in the demo. ----------------
class RoleAdd(BaseModel):
    name: str


class RoleHolder(BaseModel):
    holder_id: str | None = None


class PolicyIn(BaseModel):
    title: str
    text: str


class TypePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown or non-editable field is refused, never ignored
    enabled: bool | None = None
    requires_approval: bool | None = None
    approver_rule: str | None = None
    backup_rule: str | None = None
    extra_approvers: list[dict] | None = None
    auto_allow_up_to: float | None = None
    reminder_hours: int | None = None
    escalate_hours: int | None = None
    fulfiller_rule: str | None = None
    details_needed: list[str] | None = None
    redirect: str | None = None


class Simulate(BaseModel):
    person_id: str
    type_code: str
    estimated_cost: float | None = None
    resource: str | None = None


def _setup(fn, *a):
    try:
        return fn(*a)
    except st.SetupError as e:
        raise HTTPException(e.status, str(e))


def _setup_changed(ctx, area, code, changes):
    """Every setup write leaves an audit event (who, what, from and to) and refreshes the company's directory so the next request uses it."""
    ctx.engine.audit(None, ctx.uid, "setup_changed", {"area": area, "code": code, "changes": changes})
    registry.refresh_directory(ctx.org["id"])


def _pickable(ctx):
    d = ctx.engine.d
    return {"people": [{"id": e["id"], "name": e["name"]} for e in d.employees.values() if e["active"]], "role_options": [{"code": r["code"], "name": r["name"], "holder_name": r["holder_name"]} for r in st.list_roles(d)]}


@app.get("/api/setup/roles")
def setup_roles(ctx: Ctx = Depends(admin)):
    return {"roles": st.list_roles(ctx.engine.d), **_pickable(ctx), "demo": ctx.is_demo}


@app.post("/api/setup/roles")
def setup_role_add(b: RoleAdd, ctx: Ctx = Depends(real_admin)):
    code = _setup(st.add_role, ctx.engine.s, ctx.engine.d, b.name)
    _setup_changed(ctx, "roles", code, {"added": True})
    return {"code": code, "roles": st.list_roles(ctx.engine.d)}


@app.post("/api/setup/roles/{code}")
def setup_role_holder(code: str, b: RoleHolder, ctx: Ctx = Depends(real_admin)):
    changes = _setup(st.set_holder, ctx.engine.s, ctx.engine.d, code, b.holder_id)
    _setup_changed(ctx, "roles", code, changes)
    return {"roles": st.list_roles(ctx.engine.d)}


@app.post("/api/setup/roles/{code}/delete")
def setup_role_delete(code: str, ctx: Ctx = Depends(real_admin)):
    _setup(st.delete_role, ctx.engine.s, ctx.engine.d, code)
    _setup_changed(ctx, "roles", code, {"deleted": True})
    return {"roles": st.list_roles(ctx.engine.d)}


@app.get("/api/setup/types")
def setup_types(ctx: Ctx = Depends(admin)):
    types = st.list_types(ctx.engine.d, ctx.engine.s)
    counts = {r["type_code"]: r for r in ctx.engine.s.all("SELECT type_code, COUNT(*) n, SUM(enabled) on_ FROM type_steps GROUP BY type_code")}
    for t in types:  # whether a type can have steps in other apps, and how many it has
        c = counts.get(t["code"]) or {"n": 0, "on_": 0}
        t["steps"] = {"eligible": sp.eligible(ctx.engine.d, t["code"])[0], "count": c["n"], "on": int(c["on_"] or 0)}
    return {"types": types, **_pickable(ctx), "demo": ctx.is_demo}


@app.post("/api/setup/types/{code}")
def setup_type_edit(code: str, b: TypePatch, ctx: Ctx = Depends(real_admin)):
    changes = _setup(st.patch_type, ctx.engine.s, ctx.engine.d, code, b.model_dump(exclude_unset=True), ctx.uid)
    _setup_changed(ctx, "types", code, changes)
    return st.type_view(ctx.engine.d, ctx.engine.s, code)


@app.post("/api/setup/types/{code}/reset")
def setup_type_reset(code: str, ctx: Ctx = Depends(real_admin)):
    changes = _setup(st.reset_type, ctx.engine.s, ctx.engine.d, code, ctx.uid)
    _setup_changed(ctx, "types", code, {"reset_to_default": changes})
    return st.type_view(ctx.engine.d, ctx.engine.s, code)


@app.post("/api/setup/types/{code}/reviewed")
def setup_type_reviewed(code: str, ctx: Ctx = Depends(real_admin)):
    _setup(st.mark_reviewed, ctx.engine.s, code, ctx.uid)
    _setup_changed(ctx, "types", code, {"reviewed": True})
    return st.type_view(ctx.engine.d, ctx.engine.s, code)


@app.get("/api/setup/policies")
def setup_policies(ctx: Ctx = Depends(admin)):
    return {"policies": st.list_policies(ctx.engine.d, ctx.engine.s), "demo": ctx.is_demo}


@app.post("/api/setup/policies")
def setup_policy_set(b: PolicyIn, ctx: Ctx = Depends(real_admin)):
    key = _setup(st.set_policy, ctx.engine.s, ctx.engine.d, b.title, b.text)
    _setup_changed(ctx, "policies", key, {"saved": True})
    return {"policies": st.list_policies(ctx.engine.d, ctx.engine.s)}


@app.post("/api/setup/policies/{key}/delete")
def setup_policy_delete(key: str, ctx: Ctx = Depends(real_admin)):
    _setup(st.delete_policy, ctx.engine.s, ctx.engine.d, key)
    _setup_changed(ctx, "policies", key, {"deleted": True})
    return {"policies": st.list_policies(ctx.engine.d, ctx.engine.s)}


@app.post("/api/setup/simulate")
def setup_simulate(b: Simulate, ctx: Ctx = Depends(admin)):
    return _setup(st.simulate, ctx.engine.d, b.person_id, b.type_code, b.estimated_cost, b.resource)


@app.get("/api/setup/checklist")
def setup_checklist(ctx: Ctx = Depends(admin)):
    if ctx.is_demo:
        return {"demo": True, "steps": [], "done": 0, "total": 0, "dismissed": True}
    store = vs.ConnectionStore(ctx.engine.s, ctx.org["id"])
    connected = {a: bool((store.get(a) or {}).get("script_id")) for a in ("slack", "gmail")}
    n = ctx.engine.s.one("SELECT COUNT(*) c FROM requests")["c"]
    live = ctx.engine.live
    return {"demo": False, **st.checklist(ctx.engine.d, ctx.engine.s, accounts.list_users(ctx.org["id"]), connected, n, live.mode(), (live.rehearsal_person() or {}).get("name", ""))}


@app.post("/api/setup/checklist/dismiss")
def setup_checklist_dismiss(ctx: Ctx = Depends(real_admin)):
    ctx.engine.s.set_setting("checklist_dismissed", 1)
    return {"ok": True}


# ---------------- Leave: the company's own ledger. A person sees their own; admins manage allowances and corrections. ----------------
class LeaveAllowance(BaseModel):
    yearly_days: float


class LeaveAdjust(BaseModel):
    person_id: str
    leave_type: str
    days: float
    note: str


def _entry(e, r):
    return {"id": r["id"], "type": r["leave_type"], "year": r["year"], "days": num(r["days"]), "kind": r["kind"], "request_id": r["request_id"],
            "dates": json.loads(r["dates_json"] or "[]"), "note": r["note"] or "", "by": e.name(r["actor"]) if r["actor"] not in (None, "system") else "Deskhand", "at": r["created_at"]}


def _ledger(ctx):
    led = getattr(ctx.engine, "ledger", None)
    if led is None:
        raise HTTPException(409, "The demo company uses prepared figures, not a ledger.")
    return led


def _val(fn, *a):
    try:
        return fn(*a)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/leave/me")
def leave_me(ctx: Ctx = Depends(auth)):
    led = getattr(ctx.engine, "ledger", None)
    if led is None:  # the demo: its prepared balances, no history
        return {"demo": True, "year": None, "types": [{"code": k, "name": k.capitalize() + " leave", "yearly_days": None, "balance": v} for k, v in ctx.engine.hr.list_leave_balances(ctx.uid).items()], "entries": []}
    names = {t["code"]: t["name"] for t in led.types()}
    return {"demo": False, "year": led.year(), "types": [{"code": t["code"], "name": t["name"], "yearly_days": num(t["yearly_days"]), "balance": num(led.balance(ctx.uid, t["code"]))} for t in led.types()],
            "entries": [{**_entry(ctx.engine, r), "type_name": names.get(r["leave_type"], r["leave_type"])} for r in led.entries(ctx.uid)]}


@app.get("/api/setup/leave")
def setup_leave(ctx: Ctx = Depends(admin)):
    led = getattr(ctx.engine, "ledger", None)
    if led is None:
        return {"demo": True, "types": [], "people": []}
    people = [{"id": e["id"], "name": e["name"], "balances": {k: num(v) for k, v in led.balances(e["id"]).items()}} for e in ctx.engine.d.employees.values() if e["active"]]
    return {"demo": False, "year": led.year(), "types": [{"code": t["code"], "name": t["name"], "yearly_days": num(t["yearly_days"])} for t in led.types()], "people": people}


@app.post("/api/setup/leave/{code}")
def setup_leave_allowance(code: str, b: LeaveAllowance, ctx: Ctx = Depends(real_admin)):
    old = _val(_ledger(ctx).set_yearly, code, b.yearly_days)
    ctx.engine.audit(None, ctx.uid, "setup_changed", {"area": "leave", "code": code, "changes": {"yearly_days": [num(old), num(b.yearly_days)]}})
    return setup_leave(ctx)


@app.post("/api/leave/adjust")
def leave_adjust(b: LeaveAdjust, ctx: Ctx = Depends(real_admin)):
    led = _ledger(ctx)
    person = ctx.engine.d.emp(b.person_id)
    if not person or not person["active"]:
        raise HTTPException(422, "Choose someone from this company who has joined and is active.")
    note = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", b.note or "")).strip()
    if not 3 <= len(note) <= 200:
        raise HTTPException(422, "Say why in 3 to 200 characters: the record keeps the reason.")
    balance = _val(led.adjust, b.person_id, b.leave_type, b.days, note, ctx.uid)
    ctx.engine.audit(None, ctx.uid, "leave_adjusted", {"person": b.person_id, "type": b.leave_type, "days": num(b.days), "note": note, "balance": num(balance)})
    return setup_leave(ctx)


@app.get("/api/leave/people/{pid}")
def leave_person(pid: str, ctx: Ctx = Depends(admin)):
    led = _ledger(ctx)
    if not ctx.engine.d.emp(pid):
        raise HTTPException(404, "Not found.")
    return {"person": ctx.engine.name(pid), "entries": [_entry(ctx.engine, r) for r in led.entries(pid)]}


# ---------------- Connections (viaSocket). Admin only. auth_id and script_id never leave the server. ----------------
def _real(ctx):
    """The connection screens talk to viaSocket for real; they need the embed values set on the server."""
    client = vs.VsClient(ctx.org["id"])
    if not client.configured():
        raise HTTPException(409, "viaSocket is not configured on the server. See the README.")
    return client, vs.ConnectionStore(ctx.engine.s, ctx.org["id"])


def _vfail(r):
    raise HTTPException(422 if r.kind == vs.PERMANENT else 503, vs.redact(r.error or "viaSocket call failed"))


class ConnAct(BaseModel):
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
def connections(ctx: Ctx = Depends(admin)):
    tenant = ctx.org["id"]
    client, store = vs.VsClient(tenant), vs.ConnectionStore(ctx.engine.s, tenant)
    configured = client.configured() and not ctx.is_demo
    live, note = {}, ""
    if configured:
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
    return {"configured": configured, "demo": ctx.is_demo, "tenant": tenant, "connect_js": vs.CONNECT_JS, "apps": apps, "note": note,
            "problems": ctx.engine.live.problems()}


@app.get("/api/connections/token")
def connections_token(ctx: Ctx = Depends(real_admin)):
    client, _ = _real(ctx)
    return {"token": client.token()}  # for the Connect popup only; the secret stays here


@app.get("/api/connections/service/{key}")
def connections_service(key: str, ctx: Ctx = Depends(real_admin)):
    a = _app(key)
    return {"service_id": a["service_id"], "action_ids": [v[1] for v in a["actions"].values()]}


@app.post("/api/connections/connected")
def connection_connected(b: ConnAct, ctx: Ctx = Depends(real_admin)):
    a = _app(b.app or "")
    client, store = _real(ctx)
    if not b.auth_id:
        raise HTTPException(422, "no connection id was returned")
    r = client.connections()
    if not r.ok:
        _vfail(r)
    if str(b.auth_id) not in {str(c.get("id")) for c in r.data if isinstance(c, dict)}:
        raise HTTPException(422, "that connection does not belong to this company")  # a forged or stale id
    en = client.enable(a["service_id"], b.auth_id)
    if not en.ok:
        _vfail(en)
    store.save(b.app, b.auth_id, en.data)
    ctx.engine.audit(None, ctx.uid, "connection_added", {"app": b.app, "reused_runner": en.meta.get("reused")})
    return {"ok": True}


@app.get("/api/connections/{key}/options")
def connection_options(key: str, setting: str, ctx: Ctx = Depends(real_admin)):
    a = _app(key)
    st = next((x for x in a["settings"] if x["key"] == setting and x["kind"] == "picker"), None)
    if not st:
        raise HTTPException(404, "no such picker")
    client, store = _real(ctx)
    row = store.get(key)
    if not row:
        raise HTTPException(409, f"{a['name']} is not connected yet")
    r = client.options(a["actions"][st["action"]][0], st["field"], row["auth_id"], st["existing"])
    if not r.ok:
        raise HTTPException(422 if r.kind == vs.PERMANENT else 503, vs.redact(r.error, store.secrets()))
    return r.data


@app.post("/api/connections/{key}/setting")
def connection_setting(key: str, b: ConnAct, ctx: Ctx = Depends(real_admin)):
    a = _app(key)
    st = next((x for x in a["settings"] if x["key"] == b.setting), None)
    if not st:
        raise HTTPException(404, "no such setting")
    store = vs.ConnectionStore(ctx.engine.s, ctx.org["id"])
    if not store.get(key):
        raise HTTPException(409, f"{a['name']} is not connected yet")
    value = (b.value or "").strip()
    if st["kind"] == "email" and not dl.ONE_EMAIL.match(value):
        raise HTTPException(422, "enter exactly one email address")
    if st["required"] and not value:
        raise HTTPException(422, f"{st['label']} is required")
    store.set_setting(key, st["key"], value)
    ctx.engine.audit(None, ctx.uid, "connection_setting_changed", {"app": key, "setting": st["key"]})  # never the value: it may be an address
    return {"ok": True}


@app.post("/api/connections/{key}/disconnect")
def connection_disconnect(key: str, ctx: Ctx = Depends(real_admin)):
    _app(key)
    client, store = _real(ctx)
    row = store.get(key)
    if row:
        if key == "slack" and store.get("slack_flow"):  # the approval automation lives on this connection: remove it first, as viaSocket advises
            client.api("PUT", f"/embed/updatestatus/{store.get('slack_flow')['script_id']}?status=0")
            store.delete("slack_flow")
        client.api("PUT", f"/embed/updatestatus/{row['script_id']}?status=0")  # remove the runner first, then the connection
        r = client.revoke(row["auth_id"])
        if not r.ok:
            raise HTTPException(422, vs.redact(r.error, store.secrets()))
        store.delete(key)
        ctx.engine.audit(None, ctx.uid, "connection_removed", {"app": key})
    return {"ok": True}


EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


@app.post("/api/connections/test")
def connection_test(b: ConnAct, ctx: Ctx = Depends(real_admin)):
    """Admin-triggered proof calls. Texts are fixed in code. The test email goes only to the stored notification address."""
    integ = vs.get_integration(ctx.engine.s, ctx.org["id"])
    if b.action == "slack_lookup":
        if not EMAIL.match((b.email or "").strip()):
            raise HTTPException(422, "enter one valid email address")
        r = integ.slack_lookup_user(b.email.strip())
    elif b.action == "slack_send":  # a direct message to the signed-in admin, never to anyone else
        me = ctx.engine.d.emp(ctx.uid)
        if not (me or {}).get("slack_id"):
            raise HTTPException(409, "Your Slack account is not matched yet. Press Match Slack accounts first.")
        r = integ.slack_dm(me["slack_id"], "Deskhand connection test: Slack direct messages from Deskhand will arrive here.")
    elif b.action == "gmail_send":  # an email to the signed-in admin's own address
        r = integ.gmail_send(ctx.user["email"], "Deskhand connection test", "<p>This is a test email from Deskhand. Nothing else was done.</p>")
    else:
        raise HTTPException(404, "unknown test")
    ctx.engine.audit(None, ctx.uid, "integration_test", {"action": b.action, "outcome": r.kind, "code": r.code})
    return {"outcome": r.kind, "code": r.code, "error": vs.redact(r.error), "data": vs.scrub(r.data)}


# ---------------- After-approval steps (admin). The owner picks any app from viaSocket's catalog and what to do in it once a request type is approved (rule 21). ----------------
class StepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int | None = None
    app: str
    action_id: str
    label: str | None = None
    run_mode: str
    input: dict = {}


class StepsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[StepIn]
    close_when_done: bool | None = None


class StepOn(BaseModel):
    enabled: bool


class StepTest(BaseModel):
    confirm: bool = False
    typed: str | None = None


class FormState(BaseModel):
    type_code: str
    app: str
    action_id: str
    values: dict = {}


class PickerIn(BaseModel):
    app: str
    action_id: str
    field_key: str
    existing: dict = {}
    search: str | None = None


def _step_action(slug, action_id):
    """The catalog's app and a described action, or an HTTP error the admin can read."""
    try:
        return sp.find_action(vcat.get_catalog(), slug, action_id)
    except sp.StepError as e:
        raise HTTPException(e.status, str(e))


def _step_app(slug):
    try:
        app_ = vcat.get_catalog().app(slug)
    except vcat.CatalogError as e:
        raise HTTPException(e.status, str(e))
    if not app_:
        raise HTTPException(404, "That app is not in the catalog.")
    return app_


def _connected(ctx, slug):
    row = vs.ConnectionStore(ctx.engine.s, ctx.org["id"]).get(slug)
    return bool(row and row["script_id"])


def _steps_view(ctx, code):
    v = sp.view(ctx.engine.d, ctx.engine.s, code)
    if not v["eligible"] and v["reason"].startswith("No such"):
        raise HTTPException(404, v["reason"])
    for s_ in v["steps"]:
        s_["connected"] = _connected(ctx, s_["app"])
    return {**v, "demo": ctx.is_demo}


@app.get("/api/setup/steps/{code}")
def setup_steps(code: str, ctx: Ctx = Depends(admin)):
    return _steps_view(ctx, code)


@app.post("/api/setup/steps/{code}")
def setup_steps_save(code: str, b: StepsIn, ctx: Ctx = Depends(real_admin)):
    changes = _setup(sp.save_steps, ctx.engine.s, ctx.engine.d, code, [x.model_dump() for x in b.steps], vcat.get_catalog(), ctx.uid, b.close_when_done)
    if changes:
        _setup_changed(ctx, "steps", code, changes)
    return _steps_view(ctx, code)


@app.post("/api/setup/steps/{code}/{step_id}/enabled")
def setup_step_enabled(code: str, step_id: int, b: StepOn, ctx: Ctx = Depends(real_admin)):
    changes = _setup(sp.set_enabled, ctx.engine.s, code, step_id, b.enabled)
    _setup_changed(ctx, "steps", code, changes)
    return _steps_view(ctx, code)


@app.post("/api/setup/steps/{code}/{step_id}/test")
def setup_step_test(code: str, step_id: int, b: StepTest, ctx: Ctx = Depends(real_admin)):
    """The only real call before a company goes live: it runs this step once in the app, with made-up request details."""
    if not b.confirm:
        raise HTTPException(422, "Confirm that this really runs in the app.")
    _limit(f"steptest:{ctx.org['id']}", 10, 600)
    result = _setup(ctx.engine.stepper.test, code, step_id, ctx.uid, b.typed)
    return {**result, "view": _steps_view(ctx, code)}


@app.get("/api/steps/apps")
def steps_apps(q: str = "", ctx: Ctx = Depends(real_admin)):
    try:
        found = vcat.get_catalog().search(q)
    except vcat.CatalogError as e:
        raise HTTPException(e.status, str(e))
    return {"apps": [{**a, "connected": _connected(ctx, a["slug"])} for a in found]}


@app.get("/api/steps/apps/{slug}")
def steps_app(slug: str, ctx: Ctx = Depends(real_admin)):
    app_ = _step_app(slug)
    actions = []
    for raw in app_["actions"]:
        d_ = sp.describe_action(raw)
        actions.append({k: d_[k] for k in ("action_id", "name", "description", "destructive", "supported", "reasons")})
    return {"slug": slug, "name": app_["name"], "auth_type": app_["auth_type"], "requires_auth": app_["requires_auth"], "connected": _connected(ctx, slug), "actions": actions}


@app.get("/api/steps/apps/{slug}/actions/{action_id}")
def steps_action(slug: str, action_id: str, ctx: Ctx = Depends(real_admin)):
    app_, a = _step_action(slug, action_id)
    raw = next(x for x in app_["actions"] if x["action_id"] == action_id)
    sample = vs.scrub(raw.get("sample_output"))
    return {"app": {"slug": slug, "name": app_["name"]}, "action": {k: a[k] for k in ("action_id", "name", "description", "destructive", "supported", "reasons", "version")},
            "schema": a["schema"], "sample": sample if len(json.dumps(sample, default=str)) <= 3000 else None}


@app.post("/api/steps/form-state")
def steps_form_state(b: FormState, ctx: Ctx = Depends(real_admin)):
    ok, why = sp.eligible(ctx.engine.d, b.type_code)
    if not ok:
        raise HTTPException(422, why)
    _, a = _step_action(b.app, b.action_id)
    cleaned, errors, dropped, unreadable = sp.validate_values(a["schema"], b.values, sp.tokens_for(b.type_code))
    return {"visible": sp.visible_keys(a["schema"], b.values if isinstance(b.values, dict) else {}), "errors": errors, "dropped": dropped, "unreadable": unreadable, "cleaned": cleaned,
            "preview": sp.render(cleaned, sp.sample_context(b.type_code))}  # what a made-up request would send: the same code that fills a real one


@app.post("/api/steps/options")
def steps_options(b: PickerIn, ctx: Ctx = Depends(real_admin)):
    app_, a = _step_action(b.app, b.action_id)
    if b.field_key not in sp.field_keys(a["schema"]):
        raise HTTPException(404, "That field is not part of the action's form.")
    if len(json.dumps(b.existing, default=str)) > 8000:
        raise HTTPException(422, "Too many values were sent.")
    client, store = _real(ctx)
    row = store.get(app_["slug"])
    if not row or not row["auth_id"]:
        raise HTTPException(409, f"{app_['name']} is not connected yet.")
    r = client.picker(a["action_version_id"], b.field_key, row["auth_id"], b.existing, (b.search or "")[:80])
    if not r.ok:
        raise HTTPException(422 if r.kind == vs.PERMANENT else 503, vs.redact(r.error, store.secrets()))
    return r.data


@app.get("/api/steps/apps/{slug}/connect")
def steps_connect(slug: str, actions: str = "", ctx: Ctx = Depends(real_admin)):
    """Everything the Connect popup needs for this app. The popup asks consent only for the actions named here."""
    app_ = _step_app(slug)
    if not app_["requires_auth"]:
        raise HTTPException(422, "Apps that need no sign-in are not supported yet.")
    known = {a["action_id"] for a in app_["actions"]}
    wanted = [x for x in actions.split(",") if x][:20]
    if not wanted or any(x not in known for x in wanted):
        raise HTTPException(422, "Choose which actions you will use before connecting.")
    client, _ = _real(ctx)
    return {"token": client.token(), "service_id": app_["service_id"], "action_ids": wanted, "connect_js": vs.CONNECT_JS}


@app.post("/api/steps/apps/{slug}/connected")
def steps_connected(slug: str, b: ConnAct, ctx: Ctx = Depends(real_admin)):
    app_ = _step_app(slug)
    client, store = _real(ctx)
    if not b.auth_id:
        raise HTTPException(422, "no connection id was returned")
    r = client.connections()
    if not r.ok:
        _vfail(r)
    if str(b.auth_id) not in {str(c.get("id")) for c in r.data if isinstance(c, dict)}:
        raise HTTPException(422, "that connection does not belong to this company")  # a forged or stale id
    en = client.enable(app_["service_id"], b.auth_id)
    if not en.ok:
        _vfail(en)
    store.save(slug, b.auth_id, en.data)
    ctx.engine.audit(None, ctx.uid, "connection_added", {"app": slug, "reused_runner": en.meta.get("reused")})
    return {"ok": True}


@app.post("/api/steps/apps/{slug}/disconnect")
def steps_disconnect(slug: str, ctx: Ctx = Depends(real_admin)):
    if slug in vs.APPS:
        raise HTTPException(422, "Slack and Gmail are disconnected on the Integrations page.")
    _step_app(slug)
    client, store = _real(ctx)
    row = store.get(slug)
    switched_off = 0
    if row:
        client.api("PUT", f"/embed/updatestatus/{row['script_id']}?status=0")  # remove the runner first, then the connection
        r = client.revoke(row["auth_id"])
        if not r.ok:
            raise HTTPException(422, vs.redact(r.error, store.secrets()))
        store.delete(slug)
        switched_off = ctx.engine.s.q("UPDATE type_steps SET enabled=0 WHERE app_slug=? AND enabled=1", (slug,)).rowcount
        ctx.engine.audit(None, ctx.uid, "connection_removed", {"app": slug, "steps_switched_off": switched_off})
    return {"ok": True, "steps_switched_off": switched_off}


# ---------------- Delivery status ----------------
@app.get("/api/delivery")
def delivery_status(ctx: Ctx = Depends(admin)):
    failed = ctx.engine.s.all("SELECT id, request_id, kind, channel, delivery_error FROM outbox WHERE delivery_status='failed' ORDER BY id DESC LIMIT 20")
    people = [{"id": e["id"], "name": e["name"], "slack": bool(e.get("slack_id"))} for e in ctx.engine.d.employees.values() if e["active"]]
    return {**ctx.engine.live.summary(), "base_url": ctx.engine.live.base_url, "failed": failed, "people_slack": people}


@app.get("/api/delivery/log")
def delivery_log(status: str | None = None, channel: str | None = None, person: str | None = None, kind: str | None = None, request_id: int | None = None,
                 since: float | None = None, until: float | None = None, before: int | None = None, limit: int = 50, ctx: Ctx = Depends(admin)):
    """Admin only, and metadata only: what was sent to whom and how it ended, never what the message said."""
    f = {"status": status, "channel": channel, "person": person, "kind": kind, "request_id": request_id, "since": since, "until": until, "before": before, "limit": limit}
    return _val(nt.delivery_log, ctx.engine, f)


@app.post("/api/delivery/sweep")
def delivery_sweep(ctx: Ctx = Depends(admin)):
    return ctx.engine.live.deliver_pending()


@app.post("/api/delivery/retry/{oid}")
def delivery_retry(oid: int, ctx: Ctx = Depends(admin)):
    try:
        ctx.engine.live.retry_failed(oid, ctx.uid)
    except ValueError as ex:
        raise HTTPException(409, str(ex))
    return ctx.engine.live.deliver_pending()


# ---------------- Notifications: each person's own list, read marks and preferences ----------------
class ReadIn(BaseModel):
    ids: list[int] | None = None
    request_id: int | None = None
    all: bool = False


class PrefsIn(BaseModel):
    prefs: dict


@app.get("/api/notifications")
def notifications_list(unread: bool = False, category: str | None = None, type_: str | None = Query(None, alias="type"), status: str | None = None, channel: str | None = None,
                       since: float | None = None, until: float | None = None, q: str | None = None, before: int | None = None, limit: int = 30, ctx: Ctx = Depends(auth)):
    """Only ever the signed-in person's own messages: there is no way to name someone else."""
    f = {"unread": unread, "category": category, "type": type_, "status": status, "channel": channel, "since": since, "until": until, "q": q, "before": before, "limit": limit}
    return {**_val(nt.entries, ctx.engine, ctx.uid, f), "categories": [{"code": c, "title": t} for c, t, _ in nt.CATEGORIES]}


@app.post("/api/notifications/read")
def notifications_read(b: ReadIn, ctx: Ctx = Depends(auth)):
    changed = nt.mark_read(ctx.engine, ctx.uid, ids=b.ids, request_id=b.request_id, everything=b.all)
    return {"changed": changed, "unread": nt.unread_count(ctx.engine, ctx.uid)}


def _prefs_view(ctx):
    now = nt.get_prefs(ctx.engine.s, ctx.uid)
    return {"categories": [{"code": c, "title": t, "description": nt.DESCRIPTIONS[c], **now[c], "protected": c == nt.PROTECTED} for c, t, _ in nt.CATEGORIES],
            "mode": ctx.engine.live.mode(), "demo": ctx.is_demo}


@app.get("/api/notifications/preferences")
def notification_prefs(ctx: Ctx = Depends(auth)):
    return _prefs_view(ctx)


@app.post("/api/notifications/preferences")
def notification_prefs_save(b: PrefsIn, ctx: Ctx = Depends(auth)):
    if ctx.is_demo:
        raise HTTPException(403, "This is switched off in the demo company.")
    before = nt.get_prefs(ctx.engine.s, ctx.uid)
    after = _val(nt.set_prefs, ctx.engine.s, ctx.uid, b.prefs)
    changed = sorted(c for c in after if after[c] != before[c])
    if changed:
        ctx.engine.audit(None, ctx.uid, "notification_prefs_changed", {"categories": changed})
    return _prefs_view(ctx)


# ---------------- Understanding requests with viaSocket AI (admin switch, off by default) ----------------
class AISet(BaseModel):
    enabled: bool


@app.get("/api/ai")
def ai_status(ctx: Ctx = Depends(admin)):
    return {"enabled": ctx.engine.vs_ai.enabled(), "switch": vs.ConnectionStore(ctx.engine.s, ctx.org["id"]).ai_enabled(),
            "configured": vs.VsClient(ctx.org["id"]).configured() and not ctx.is_demo,
            "calls_today": ctx.engine.ai_usage()[0], "budget": ctx.engine.ai_usage()[1], "timeout_s": vsai.TIMEOUT_S}


@app.post("/api/ai")
def ai_set(b: AISet, ctx: Ctx = Depends(real_admin)):
    if b.enabled and not vs.VsClient(ctx.org["id"]).configured():
        raise HTTPException(409, "viaSocket is not configured on the server. See the README.")
    vs.ConnectionStore(ctx.engine.s, ctx.org["id"]).set_ai_enabled(b.enabled)
    ctx.engine.audit(None, ctx.uid, "ai_setting_changed", {"enabled": b.enabled})
    return {"ok": True, "enabled": ctx.engine.vs_ai.enabled()}


# ---------------- Slack Approve / Reject buttons ----------------
class SlackClick(BaseModel):
    token: str = ""
    slack_user: str | None = None
    decision: str | None = None


@app.post("/api/hooks/slack-decision")
def slack_decision(b: SlackClick, request: Request):
    """Called by the company's viaSocket approval automation when someone presses a button. Public, so everything is checked here (see app/slack_decisions.py); every answer is HTTP 200."""
    _limit("hook:" + _ip(request), 120, 60)
    org = sd.org_of(b.token)
    try:
        eng = registry.engine_for(org) if org else None
    except KeyError:
        eng = None
    if not eng or eng.is_demo:
        log.warning("slack decision: invalid")
        return {"status": "invalid"}
    out = sd.decide(eng, b.token, (b.slack_user or "")[:40], (b.decision or "")[:12])
    log.info("slack decision: %s", out["status"])  # the status word only: never the token or the user
    return out


def _callback_url(ctx):
    return ctx.engine.live.base_url + "/api/hooks/slack-decision"


@app.post("/api/integrations/slack/buttons")
def slack_buttons_enable(ctx: Ctx = Depends(real_admin)):
    r = vs.get_integration(ctx.engine.s, ctx.org["id"]).create_approval_flow(_callback_url(ctx))
    if not r.ok:
        raise HTTPException(409 if r.code in ("not_connected", "no_public_url") else 422, vs.redact(r.error))
    ctx.engine.audit(None, ctx.uid, "slack_buttons_enabled", {})
    return ctx.engine.live.summary()


@app.post("/api/integrations/slack/test-approval")
def slack_test_approval(ctx: Ctx = Depends(real_admin)):
    """A test message with Approve and Reject buttons to the signed-in admin. Pressing them changes nothing: it only proves the click comes back."""
    me = ctx.engine.d.emp(ctx.uid)
    if not (me or {}).get("slack_id"):
        raise HTTPException(409, "Your Slack account is not matched yet. Press Match Slack accounts first.")
    token = sd.issue(ctx.engine, ctx.org["id"], "test", None, ctx.uid)
    r = vs.get_integration(ctx.engine.s, ctx.org["id"]).slack_approval(me["slack_id"], "*Deskhand test*\nPress Approve or Reject. Nothing will change: this only checks that the buttons work.", token)
    if not r.ok:
        raise HTTPException(409 if r.code in ("no_flow", "not_connected") else 422, vs.redact(r.error))
    return sd.latest_test(ctx.engine, ctx.uid)


@app.get("/api/integrations/slack/test-approval")
def slack_test_approval_state(ctx: Ctx = Depends(real_admin)):
    return sd.latest_test(ctx.engine, ctx.uid)


# ---------------- Delivery mode, Slack matching, and each person's own mailbox ----------------
class DeliveryMode(BaseModel):
    mode: str
    rehearsal_person: str | None = None
    confirm: bool = False


SLACK_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")


@app.post("/api/delivery/mode")
def delivery_mode(b: DeliveryMode, ctx: Ctx = Depends(real_admin)):
    live = ctx.engine.live
    if b.mode == "live" and not b.confirm:
        raise HTTPException(422, "Confirm that real people should now receive Slack messages and emails.")
    if b.mode == "live" and not (live.ready("chat") or live.ready("email")):
        raise HTTPException(409, "Connect Slack or Gmail first: there is nothing to send with yet.")
    _val(live.set_mode, b.mode, ctx.uid, b.rehearsal_person)
    return live.summary()


@app.post("/api/integrations/slack/match")
def slack_match(ctx: Ctx = Depends(real_admin)):
    """Find each person's Slack account from their directory email (Get User Information, verified). A batch at a time: each lookup is a call."""
    integ = vs.get_integration(ctx.engine.s, ctx.org["id"])
    todo = [e for e in ctx.engine.d.employees.values() if e["active"] and not e.get("slack_id")]
    matched, missing, problems = [], [], []
    for e in todo[:25]:
        r = integ.slack_lookup_user(e["email"])
        if not r.ok:
            problems.append(vs.redact(r.error)[:120])
            if r.kind == vs.PERMANENT:
                break  # not connected or refused: the rest would fail the same way
            continue
        sid = (r.data or {}).get("id")
        if (r.data or {}).get("found") and SLACK_ID.match(str(sid or "")):
            accounts.update_user(ctx.org["id"], e["id"], ctx.uid, slack_id=sid)
            matched.append(e["name"])
        else:
            missing.append(e["name"])
    registry.refresh_directory(ctx.org["id"])
    left = len([e for e in ctx.engine.d.employees.values() if e["active"] and not e.get("slack_id")])
    ctx.engine.audit(None, ctx.uid, "slack_matched", {"matched": len(matched), "not_found": len(missing), "problems": len(problems)})
    return {"matched": matched, "not_found": missing, "problems": problems[:3], "unmatched_left": left, "batch_limit": 25}


def _mine(ctx):
    if ctx.is_demo:
        raise HTTPException(403, "This is switched off in the demo company.")
    tenant = vs.personal_tenant(ctx.org["id"], ctx.uid)  # a person's own connections live under their own viaSocket identity
    client = vs.VsClient(tenant)
    if not client.configured():
        raise HTTPException(409, "viaSocket is not configured on the server. See the README.")
    return client, vs.ConnectionStore(ctx.engine.s, tenant)


@app.get("/api/me/connections")
def me_connections(ctx: Ctx = Depends(auth)):
    if ctx.is_demo:
        return {"demo": True, "configured": False, "app": None}
    tenant = vs.personal_tenant(ctx.org["id"], ctx.uid)
    row = vs.ConnectionStore(ctx.engine.s, tenant).get("gmail")
    a = vs.APPS["gmail"]
    return {"demo": False, "configured": vs.VsClient(tenant).configured(), "connect_js": vs.CONNECT_JS,
            "app": {"key": "gmail", "name": a["name"], "icon": a["icon"], "connected": bool(row and row["script_id"])}, "email": ctx.user["email"]}


@app.get("/api/me/connections/token")
def me_token(ctx: Ctx = Depends(auth)):
    client, _ = _mine(ctx)
    return {"token": client.token(), "service_id": vs.APPS["gmail"]["service_id"], "action_ids": [v[1] for v in vs.APPS["gmail"]["actions"].values()]}


@app.post("/api/me/connections/connected")
def me_connected(b: ConnAct, ctx: Ctx = Depends(auth)):
    client, store = _mine(ctx)
    if not b.auth_id:
        raise HTTPException(422, "no connection id was returned")
    r = client.connections()
    if not r.ok:
        _vfail(r)
    if str(b.auth_id) not in {str(c.get("id")) for c in r.data if isinstance(c, dict)}:
        raise HTTPException(422, "that connection does not belong to you")  # a forged or stale id
    en = client.enable(vs.APPS["gmail"]["service_id"], b.auth_id)
    if not en.ok:
        _vfail(en)
    store.save("gmail", b.auth_id, en.data)
    ctx.engine.audit(None, ctx.uid, "personal_mailbox_connected", {})
    return {"ok": True}


@app.post("/api/me/connections/gmail/disconnect")
def me_disconnect(ctx: Ctx = Depends(auth)):
    client, store = _mine(ctx)
    row = store.get("gmail")
    if row:
        client.api("PUT", f"/embed/updatestatus/{row['script_id']}?status=0")
        r = client.revoke(row["auth_id"])
        if not r.ok:
            raise HTTPException(422, vs.redact(r.error, store.secrets()))
        store.delete("gmail")
        ctx.engine.audit(None, ctx.uid, "personal_mailbox_disconnected", {})
    return {"ok": True}


@app.post("/api/me/connections/test")
def me_test(ctx: Ctx = Depends(auth)):
    """An email to your own address from your own mailbox, nothing else."""
    _, store = _mine(ctx)
    if not (store.get("gmail") or {}).get("script_id"):
        raise HTTPException(409, "Connect your mailbox first.")
    r = vs.get_integration(ctx.engine.s, vs.personal_tenant(ctx.org["id"], ctx.uid)).gmail_send(ctx.user["email"], "Deskhand mailbox test", "<p>Deskhand can send from this mailbox. Nothing else was done.</p>")
    return {"outcome": r.kind, "error": vs.redact(r.error)}


# ---------------- Background worker: delivery for every company, reminders and escalations for real ones ----------------
def _worker_pass(tick):
    """One round of background work for every company: deliver messages, run queued steps in other apps, and (for real companies, every sixth round) reminders and escalations."""
    for eng in registry.all_engines():
        try:
            eng.live.deliver_pending()
            eng.stepper.run_pending()  # live companies only, a few per round
            if tick % 6 == 0 and not eng.is_demo:  # the demo company moves only when the presenter moves its clock
                eng.sweep()
        except Exception as ex:  # the worker must never die; the log gets the kind of error and the company, never message text
            log.warning("worker round failed for %s: %s", getattr(eng, "org_id", "?"), type(ex).__name__)


def _worker():
    def loop():
        tick = 0
        while True:
            time.sleep(10)
            tick += 1
            _worker_pass(tick)

    threading.Thread(target=loop, daemon=True, name="worker").start()


@contextlib.asynccontextmanager
async def _lifespan(_app):
    if os.environ.get("DESKHAND_DELIVERY_WORKER", "on") != "off":
        _worker()
    yield


app.router.lifespan_context = _lifespan
