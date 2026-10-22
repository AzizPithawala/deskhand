"""Accounts: companies, the people who can sign in, sessions, invites and signup codes. One small platform database; every
company's own data (requests, outbox, audit, connections) is a separate SQLite file, so one company cannot read another's rows.

Security notes: passwords are hashed with scrypt (stdlib) and never logged or returned; session tokens, invite tokens and signup
codes are stored only as SHA-256 hashes; login failures are throttled; a deactivated person loses every session at once."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from .db import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0, timezone TEXT, status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, password_hash TEXT,
  status TEXT NOT NULL, access_role TEXT NOT NULL, department TEXT, manager_id TEXT, slack_id TEXT, phone TEXT,
  created_at REAL NOT NULL, last_login_at REAL);
CREATE INDEX IF NOT EXISTS users_org ON users(org_id);
CREATE TABLE IF NOT EXISTS sessions(
  id_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, org_id TEXT NOT NULL, csrf TEXT NOT NULL,
  created_at REAL NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL, ip TEXT, ua TEXT);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS invites(
  token_hash TEXT PRIMARY KEY, org_id TEXT NOT NULL, user_id TEXT NOT NULL, kind TEXT NOT NULL, created_by TEXT,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL);
CREATE TABLE IF NOT EXISTS signup_codes(
  code_hash TEXT PRIMARY KEY, max_uses INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS login_attempts_key ON login_attempts(key, at);
CREATE TABLE IF NOT EXISTS platform_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT, org_id TEXT, type TEXT NOT NULL, data_json TEXT DEFAULT '{}');
CREATE TRIGGER IF NOT EXISTS platform_audit_no_update BEFORE UPDATE ON platform_audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS platform_audit_no_delete BEFORE DELETE ON platform_audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
"""

ACCESS_ROLES = ("owner", "admin", "member")
SESSION_IDLE_S = 12 * 3600
SESSION_ABSOLUTE_S = 14 * 86400
INVITE_TTL_S = 7 * 86400
LOCKOUT_MAX, LOCKOUT_WINDOW_S = 5, 15 * 60
MIN_PASSWORD = 10
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
COMMON = {"password12", "password123", "1234567890", "qwertyuiop", "letmein123", "welcome123", "iloveyou12", "admin12345", "changeme123"}


class AccountError(Exception):
    """A refusal with a plain message that is safe to show to the person."""
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def _h(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def hash_password(pw):
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw, stored):
    try:
        _, n, r, p, salt, want = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(want))
    except Exception:
        return False


_DUMMY = hash_password("not-a-real-password")  # verified against when the email is unknown, so timing does not reveal who exists


def password_problem(pw, email=""):
    pw = pw or ""
    if len(pw) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(pw) > 200:
        return "That password is too long."
    low = pw.lower()
    if low in COMMON or (email and (low == email.lower() or low == email.split("@")[0].lower())):
        return "Choose a password that is harder to guess."
    return None


def clean_name(v, what="name", limit=100):
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    if not v or len(v) > limit:
        raise AccountError(f"Enter a {what} of 1 to {limit} characters.")
    return v


def clean_email(v):
    v = str(v or "").strip().lower()
    if not EMAIL_RE.match(v) or len(v) > 200:
        raise AccountError("Enter one valid email address.")
    return v


class Accounts:
    def __init__(self, store=None, now=time.time):
        self.s = store or Store(":memory:", schema=SCHEMA)
        self.now = now

    # ---------- audit ----------
    def audit(self, actor, type_, org_id=None, data=None):
        self.s.q("INSERT INTO platform_audit(at,actor,org_id,type,data_json) VALUES(?,?,?,?,?)",
                 (self.now(), actor, org_id, type_, json.dumps(data or {})))

    # ---------- signup codes ----------
    def sync_codes(self, codes):
        """Make the stored codes match the configured list. A code no longer configured stops working."""
        keep = set()
        for c in codes:
            c = c.strip()
            if not c:
                continue
            keep.add(_h(c))
            self.s.q("INSERT OR IGNORE INTO signup_codes(code_hash,max_uses,uses,created_at) VALUES(?,?,?,?)", (_h(c), 1, 0, self.now()))
        for r in self.s.all("SELECT code_hash FROM signup_codes"):
            if r["code_hash"] not in keep:
                self.s.q("UPDATE signup_codes SET max_uses=uses WHERE code_hash=?", (r["code_hash"],))

    def _consume_code(self, code):
        cur = self.s.q("UPDATE signup_codes SET uses=uses+1 WHERE code_hash=? AND uses<max_uses", (_h((code or "").strip()),))
        return cur.rowcount == 1

    def _refund_code(self, code):
        self.s.q("UPDATE signup_codes SET uses=uses-1 WHERE code_hash=? AND uses>0", (_h((code or "").strip()),))

    # ---------- companies ----------
    def get_org(self, org_id):
        return self.s.one("SELECT * FROM orgs WHERE id=?", (org_id,))

    def list_orgs(self):
        return self.s.all("SELECT * FROM orgs WHERE status='active' ORDER BY created_at")

    def create_org(self, name, timezone=None, is_demo=False, org_id=None):
        org_id = org_id or "org_" + secrets.token_hex(5)
        self.s.q("INSERT INTO orgs(id,name,is_demo,timezone,status,created_at) VALUES(?,?,?,?,?,?)",
                 (org_id, name, 1 if is_demo else 0, timezone, "active", self.now()))
        return self.get_org(org_id)

    def register_company(self, company, admin_name, email, password, code, timezone=None):
        """Create a company and its owner. Needs a signup code. Returns (org, user)."""
        company, admin_name, email = clean_name(company, "company name"), clean_name(admin_name), clean_email(email)
        problem = password_problem(password, email)
        if problem:
            raise AccountError(problem)
        if self.get_by_email(email):
            raise AccountError("That email address is already registered.", 409)
        if not self._consume_code(code):
            raise AccountError("That invite code is not valid.", 403)
        try:
            org = self.create_org(company, str(timezone or "")[:60] or None)
            user = self.create_user(org["id"], email, admin_name, "owner", status="active", password=password)
        except Exception:
            self._refund_code(code)
            raise
        self.audit(user["id"], "company_registered", org["id"], {"company": company})
        return org, user

    # ---------- people ----------
    PUBLIC = ("id", "org_id", "email", "name", "status", "access_role", "department", "manager_id", "slack_id", "phone", "created_at", "last_login_at")

    def public(self, u):
        return {k: u[k] for k in self.PUBLIC} if u else None

    def get_user(self, user_id):
        return self.s.one("SELECT * FROM users WHERE id=?", (user_id,))

    def get_by_email(self, email):
        return self.s.one("SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),))

    def list_users(self, org_id):
        return [self.public(u) for u in self.s.all("SELECT * FROM users WHERE org_id=? ORDER BY created_at, id", (org_id,))]

    def create_user(self, org_id, email, name, access_role="member", status="invited", password=None, user_id=None,
                    department=None, manager_id=None, slack_id=None, phone=None):
        if access_role not in ACCESS_ROLES:
            raise AccountError("Unknown access role.")
        email, name = clean_email(email), clean_name(name)
        if self.get_by_email(email):
            raise AccountError("That email address is already registered.", 409)
        uid = user_id or "u_" + secrets.token_hex(5)
        self.s.q("INSERT INTO users(id,org_id,email,name,password_hash,status,access_role,department,manager_id,slack_id,phone,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (uid, org_id, email, name, hash_password(password) if password else None, status, access_role,
                  department, manager_id, slack_id, phone, self.now()))
        return self.get_user(uid)

    def _in_org(self, org_id, user_id):
        u = self.get_user(user_id) if user_id else None
        if not u or u["org_id"] != org_id:
            raise AccountError("That person is not in this company.", 404)
        return u

    def admins(self, org_id):
        return [u for u in self.s.all("SELECT * FROM users WHERE org_id=? AND status='active' AND access_role IN ('owner','admin')", (org_id,))]

    def update_user(self, org_id, user_id, actor_id=None, **fields):
        """Admin edits. Only the allowed fields; the owner cannot be demoted or disabled; a company always keeps an admin."""
        u = self._in_org(org_id, user_id)
        sets = {}
        if "name" in fields:
            sets["name"] = clean_name(fields["name"])
        for k in ("department", "slack_id", "phone"):
            if k in fields:
                v = re.sub(r"\s+", " ", str(fields[k] or "")).strip()[:100]
                sets[k] = v or None
        if "manager_id" in fields:
            mid = fields["manager_id"] or None
            if mid:
                if mid == user_id:
                    raise AccountError("A person cannot be their own manager.")
                self._in_org(org_id, mid)
                seen, cur = {user_id}, mid  # refuse a loop in the reporting line
                while cur:
                    if cur in seen:
                        raise AccountError("That would make a loop in the reporting line.")
                    seen.add(cur)
                    cur = (self.get_user(cur) or {}).get("manager_id")
            sets["manager_id"] = mid
        if "access_role" in fields:
            role = fields["access_role"]
            if role not in ACCESS_ROLES or role == "owner" and u["access_role"] != "owner":
                raise AccountError("Choose admin or member. There is exactly one owner.")
            if u["access_role"] == "owner" and role != "owner":
                raise AccountError("The owner cannot be changed.")
            sets["access_role"] = role
        if "status" in fields:
            st = fields["status"]
            if st not in ("active", "disabled"):
                raise AccountError("Status can only be active or disabled.")
            if u["access_role"] == "owner" and st == "disabled":
                raise AccountError("The owner cannot be deactivated.")
            if u["status"] in ("invited", "demo"):
                raise AccountError("This person has not joined yet.")
            sets["status"] = st
        if not sets:
            return self.public(u)
        self.s.q(f"UPDATE users SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), user_id))
        if sets.get("status") == "disabled" or ("access_role" in sets and sets["access_role"] != u["access_role"]):
            self.end_user_sessions(user_id)  # a changed or removed person signs in again
        self.audit(actor_id, "user_updated", org_id, {"user": user_id, "fields": sorted(sets)})
        return self.public(self.get_user(user_id))

    def set_password(self, user_id, password):
        u = self.get_user(user_id)
        problem = password_problem(password, u["email"])
        if problem:
            raise AccountError(problem)
        self.s.q("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id))
        self.end_user_sessions(user_id)

    # ---------- signing in ----------
    def _throttle_key(self, email, ip):
        # keyed on the email alone: the address a client reports is spoofable, so keying on it would let an attacker dodge the lockout
        return _h((email or "").strip().lower())

    def authenticate(self, email, password, ip=None):
        key = self._throttle_key(email, ip)
        since = self.now() - LOCKOUT_WINDOW_S
        fails = self.s.one("SELECT COUNT(*) c FROM login_attempts WHERE key=? AND at>=?", (key, since))["c"]
        if fails >= LOCKOUT_MAX:
            raise AccountError("Too many sign-in attempts for this account. Wait 15 minutes and try again.", 429)
        u = self.get_by_email(email)
        ok = verify_password(password or "", u["password_hash"] if u and u["password_hash"] else _DUMMY)
        if not (u and ok and u["status"] == "active"):
            self.s.q("INSERT INTO login_attempts(key,at) VALUES(?,?)", (key, self.now()))
            self.audit(None, "login_failed", u["org_id"] if u else None, {})
            raise AccountError("The email or password is not right.", 401)
        org = self.get_org(u["org_id"])
        if not org or org["status"] != "active":
            raise AccountError("The email or password is not right.", 401)
        self.s.q("DELETE FROM login_attempts WHERE key=?", (key,))
        self.s.q("UPDATE users SET last_login_at=? WHERE id=?", (self.now(), u["id"]))
        self.audit(u["id"], "login_succeeded", u["org_id"], {})
        return self.get_user(u["id"])

    # ---------- sessions ----------
    def create_session(self, user, ip=None, ua=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        now = self.now()
        self.s.q("INSERT INTO sessions(id_hash,user_id,org_id,csrf,created_at,last_seen,expires_at,ip,ua) VALUES(?,?,?,?,?,?,?,?,?)",
                 (_h(token), user["id"], user["org_id"], csrf, now, now, now + SESSION_ABSOLUTE_S, (ip or "")[:64], (ua or "")[:200]))
        return token, csrf

    def get_session(self, token):
        """The live session and person behind a cookie, or None. Expired, idle or deactivated sessions are removed."""
        if not token:
            return None
        row = self.s.one("SELECT * FROM sessions WHERE id_hash=?", (_h(token),))
        if not row:
            return None
        now = self.now()
        user = self.get_user(row["user_id"])
        org = self.get_org(row["org_id"])
        if (now > row["expires_at"] or now - row["last_seen"] > SESSION_IDLE_S or not user or user["status"] not in ("active", "demo")
                or not org or org["status"] != "active" or user["org_id"] != row["org_id"]):
            self.s.q("DELETE FROM sessions WHERE id_hash=?", (row["id_hash"],))
            return None
        if now - row["last_seen"] > 60:
            self.s.q("UPDATE sessions SET last_seen=? WHERE id_hash=?", (now, row["id_hash"]))
        return {"user": user, "org": org, "csrf": row["csrf"], "token_hash": row["id_hash"]}

    def switch_demo_user(self, token, user_id):
        """Demo company only: move a demo session to another fictional person."""
        sess = self.get_session(token)
        target = self.get_user(user_id)
        if not sess or not sess["org"]["is_demo"] or not target or target["org_id"] != sess["org"]["id"] or target["status"] != "demo":
            raise AccountError("That person is not in the demo company.", 404)
        self.s.q("UPDATE sessions SET user_id=? WHERE id_hash=?", (user_id, sess["token_hash"]))

    def end_session(self, token):
        self.s.q("DELETE FROM sessions WHERE id_hash=?", (_h(token or ""),))

    def end_user_sessions(self, user_id):
        self.s.q("DELETE FROM sessions WHERE user_id=?", (user_id,))

    # ---------- invites and password-reset links ----------
    def create_invite(self, org_id, email, name, access_role, created_by, department=None, manager_id=None):
        """Create the person (status invited) and a single-use join token. The admin copies the link."""
        if access_role not in ("admin", "member"):
            raise AccountError("Invite someone as an admin or a member.")
        if manager_id:
            self._in_org(org_id, manager_id)
        user = self.create_user(org_id, email, name, access_role, status="invited", department=department, manager_id=manager_id)
        token = self._issue(org_id, user["id"], "invite", created_by)
        self.audit(created_by, "invite_created", org_id, {"user": user["id"]})
        return self.public(user), token

    def create_reset_link(self, org_id, user_id, created_by):
        u = self._in_org(org_id, user_id)
        if u["status"] not in ("active", "disabled", "invited"):
            raise AccountError("This person cannot sign in with a password.")
        token = self._issue(org_id, user_id, "reset" if u["status"] != "invited" else "invite", created_by)
        self.audit(created_by, "reset_link_created", org_id, {"user": user_id})
        return token

    def _issue(self, org_id, user_id, kind, created_by):
        token = secrets.token_urlsafe(32)
        now = self.now()
        self.s.q("INSERT INTO invites(token_hash,org_id,user_id,kind,created_by,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                 (_h(token), org_id, user_id, kind, created_by, now, now + INVITE_TTL_S))
        return token

    def peek_invite(self, token):
        row = self.s.one("SELECT * FROM invites WHERE token_hash=?", (_h(token or ""),))
        if not row or row["used_at"] or self.now() > row["expires_at"]:
            return None
        u = self.get_user(row["user_id"])
        org = self.get_org(row["org_id"])
        return {"kind": row["kind"], "name": u["name"], "email": u["email"], "company": org["name"]} if u and org else None

    def accept_invite(self, token, password, ip=None):
        """Spend the link once: set the password and sign the person in. Returns the user."""
        row = self.s.one("SELECT * FROM invites WHERE token_hash=?", (_h(token or ""),))
        if not row or row["used_at"] or self.now() > row["expires_at"]:
            raise AccountError("This link has expired or was already used. Ask your admin for a new one.", 410)
        u = self.get_user(row["user_id"])
        problem = password_problem(password, u["email"]) if u else "This link is not valid."
        if problem:
            raise AccountError(problem)
        cur = self.s.q("UPDATE invites SET used_at=? WHERE token_hash=? AND used_at IS NULL", (self.now(), row["token_hash"]))
        if cur.rowcount != 1:
            raise AccountError("This link has expired or was already used. Ask your admin for a new one.", 410)
        self.s.q("UPDATE users SET password_hash=?, status=CASE WHEN status='invited' THEN 'active' ELSE status END WHERE id=?",
                 (hash_password(password), u["id"]))
        self.end_user_sessions(u["id"])
        self.s.q("UPDATE users SET last_login_at=? WHERE id=?", (self.now(), u["id"]))
        self.audit(u["id"], "invite_accepted" if row["kind"] == "invite" else "password_reset", u["org_id"], {})
        return self.get_user(u["id"])
