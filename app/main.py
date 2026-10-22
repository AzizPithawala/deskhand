"""Deskhand web app. Identity comes from the signed-in session (never from the request body); each company has its own engine and
database file; admin, demo-only and visibility rules are enforced here, in code."""
import contextlib
import hmac
import os
import re
import threading
import time
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from . import accounts as ac, chaos, delivery as dl, setup as st, viasocket_adapter as vs, viasocket_ai as vsai
from .engine import InvalidTransition, NotAuthorized, Refused
from .registry import DEMO_ORG_ID, open_platform

vs.load_dotenv()
DATA_DIR = os.environ.get("DESKHAND_DATA_DIR", ".deskhand")
SIGNUP_CODES = [c.strip() for c in os.environ.get("DESKHAND_SIGNUP_CODES", "").split(",") if c.strip()]
accounts, registry = open_platform(DATA_DIR, SIGNUP_CODES)
COOKIE = "dh_session"

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
    sess = accounts.get_session(token)
    if not sess:
        raise HTTPException(401, "Sign in to continue.")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), sess["csrf"]):
        raise HTTPException(403, "This request did not come from the Deskhand page. Reload and try again.")
    try:
        eng = registry.engine_for(sess["org"]["id"])
    except KeyError:
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
        people = [{k: p.get(k) for k in ("id", "name", "role", "department", "manager_id", "active")} for p in people]
    return {"employees": people, "types": list(ctx.engine.d.types.values()), "now": ctx.engine.now()}


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
    return {"request": r, "timeline": ctx.engine.timeline(rid)}


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


@app.get("/api/inbox")
def inbox(ctx: Ctx = Depends(auth)):
    return ctx.engine.inbox(ctx.uid)


@app.get("/api/outbox")
def outbox(limit: int = 100, ctx: Ctx = Depends(auth)):
    return ctx.engine.s.all("SELECT * FROM outbox WHERE to_person=? ORDER BY id DESC LIMIT ?", (ctx.uid, min(max(limit, 1), 500)))


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
    banners = [] if ctx.is_demo else list(d["problems"]) + (
        [f"Delivery paused: the daily cap of {d['cap']} messages is reached ({d['used_today']}). Messages stay in the outbox until tomorrow (UTC)."] if d["capped"] else [])
    return {"now": ctx.engine.now(), "banners": banners, "demo": ctx.is_demo, "delivery": {"used_today": d["used_today"], "cap": d["cap"], "capped": d["capped"]}}


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
    return accounts.list_users(ctx.org["id"])


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
    return {"people": [{"id": e["id"], "name": e["name"]} for e in d.employees.values() if e["active"]], "role_options": [{"code": r["code"], "name": r["name"]} for r in st.list_roles(d)]}


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
    return {"types": st.list_types(ctx.engine.d, ctx.engine.s), **_pickable(ctx), "demo": ctx.is_demo}


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
    return {"demo": False, **st.checklist(ctx.engine.d, ctx.engine.s, accounts.list_users(ctx.org["id"]), connected, n)}


@app.post("/api/setup/checklist/dismiss")
def setup_checklist_dismiss(ctx: Ctx = Depends(real_admin)):
    ctx.engine.s.set_setting("checklist_dismissed", 1)
    return {"ok": True}


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
    elif b.action == "slack_send":
        r = integ.slack_send("Deskhand connection test: this channel will receive request notifications.")
    elif b.action == "gmail_send":
        to = ctx.engine.live.notify_email()
        if not to:
            raise HTTPException(409, "Set the notification email first.")
        r = integ.gmail_send(to, "Deskhand connection test", "<p>This is a test email from Deskhand. Nothing else was done.</p>")
    else:
        raise HTTPException(404, "unknown test")
    ctx.engine.audit(None, ctx.uid, "integration_test", {"action": b.action, "outcome": r.kind, "code": r.code})
    return {"outcome": r.kind, "code": r.code, "error": vs.redact(r.error), "data": vs.scrub(r.data)}


# ---------------- Delivery status ----------------
def _mask(addr):
    return (addr[0] + "***@" + addr.split("@", 1)[1]) if addr else ""


@app.get("/api/delivery")
def delivery_status(ctx: Ctx = Depends(admin)):
    failed = ctx.engine.s.all("SELECT id, request_id, kind, channel, delivery_error FROM outbox WHERE delivery_status='failed' ORDER BY id DESC LIMIT 20")
    return {**ctx.engine.live.summary(), "email_to": _mask(ctx.engine.live.notify_email()), "base_url": ctx.engine.live.base_url, "failed": failed}


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


# ---------------- Understanding requests with viaSocket AI (admin switch, off by default) ----------------
class AISet(BaseModel):
    enabled: bool


@app.get("/api/ai")
def ai_status(ctx: Ctx = Depends(admin)):
    return {"enabled": ctx.engine.vs_ai.enabled(), "switch": vs.ConnectionStore(ctx.engine.s, ctx.org["id"]).ai_enabled(),
            "configured": vs.VsClient(ctx.org["id"]).configured() and not ctx.is_demo,
            "calls_today": int(ctx.engine.s.get_setting("ai_calls_today", 0)), "budget": int(ctx.engine.s.get_setting("ai_budget_per_day", 0)), "timeout_s": vsai.TIMEOUT_S}


@app.post("/api/ai")
def ai_set(b: AISet, ctx: Ctx = Depends(real_admin)):
    if b.enabled and not vs.VsClient(ctx.org["id"]).configured():
        raise HTTPException(409, "viaSocket is not configured on the server. See the README.")
    vs.ConnectionStore(ctx.engine.s, ctx.org["id"]).set_ai_enabled(b.enabled)
    ctx.engine.audit(None, ctx.uid, "ai_setting_changed", {"enabled": b.enabled})
    return {"ok": True, "enabled": ctx.engine.vs_ai.enabled()}


# ---------------- Background worker: delivery for every company, reminders and escalations for real ones ----------------
def _worker():
    def loop():
        tick = 0
        while True:
            time.sleep(10)
            tick += 1
            for eng in registry.all_engines():
                try:
                    eng.live.deliver_pending()
                    if tick % 6 == 0 and not eng.is_demo:  # the demo company moves only when the presenter moves its clock
                        eng.sweep()
                except Exception:  # the worker must never die or log message text
                    pass

    threading.Thread(target=loop, daemon=True, name="worker").start()


@contextlib.asynccontextmanager
async def _lifespan(_app):
    if os.environ.get("DESKHAND_DELIVERY_WORKER", "on") != "off":
        _worker()
    yield


app.router.lifespan_context = _lifespan
