"""One engine and one database file per company. A company's requests, outbox, audit and connections live only in orgs/<id>.db, so
nothing in one company's engine can reach another company's rows. The demo company is a company like any other, flagged is_demo."""
import os
import re
import shutil
import sqlite3
import threading

from . import viasocket_adapter as vs, viasocket_ai as vsai
from .accounts import Accounts
from .db import Store
from .delivery import Delivery
from .directory import Directory
from .engine import Engine

DEMO_ORG_ID = "acme-demo"
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class OrgRegistry:
    def __init__(self, accounts: Accounts, data_dir):
        self.accounts, self.data_dir = accounts, data_dir
        self._engines, self._lock = {}, threading.RLock()
        os.makedirs(os.path.join(data_dir, "orgs"), exist_ok=True)

    def path(self, org_id):
        if not SAFE_ID.match(org_id or ""):
            raise ValueError("bad company id")
        return os.path.join(self.data_dir, "orgs", org_id + ".db")

    def _users(self, org_id):
        return [u for u in self.accounts.s.all("SELECT * FROM users WHERE org_id=? ORDER BY created_at, id", (org_id,))]

    def _build(self, org):
        directory = Directory.load() if org["is_demo"] else Directory.from_users(self._users(org["id"]))
        engine = Engine(store=Store(self.path(org["id"])), directory=directory, api_key="")
        engine.org_id, engine.is_demo = org["id"], bool(org["is_demo"])
        engine.live = Delivery(engine, tenant=org["id"])  # live-only delivery, per company
        ai = vsai.ViaSocketClassifier(engine.s, org["id"])
        engine.ai_classify, engine.ai_enabled = ai, ai.enabled
        engine.vs_ai = ai
        return engine

    def engine_for(self, org_id):
        with self._lock:
            if org_id not in self._engines:
                org = self.accounts.get_org(org_id)
                if not org or org["status"] != "active":
                    raise KeyError(org_id)
                self._engines[org_id] = self._build(org)
            return self._engines[org_id]

    def refresh_directory(self, org_id):
        """Call after people change. The demo company keeps its fixed JSON directory."""
        with self._lock:
            eng = self._engines.get(org_id)
            if eng and not eng.is_demo:
                eng.d.replace_employees(self._users(org_id))

    def all_engines(self):
        for org in self.accounts.list_orgs():
            try:
                yield self.engine_for(org["id"])
            except Exception:
                continue

    # ---------- demo company and the old single-company database ----------
    def seed_demo(self):
        """Create the demo company and its ten fictional people once. They have no password: visitors enter through the demo button."""
        if self.accounts.get_org(DEMO_ORG_ID):
            return
        self.accounts.create_org("Acme Demo", is_demo=True, org_id=DEMO_ORG_ID)
        for e in Directory.load().employees.values():
            self.accounts.create_user(DEMO_ORG_ID, e["email"], e["name"], "admin" if e["role"] == "admin" else "member", status="demo",
                                      user_id=e["id"], department=e.get("department"), manager_id=e.get("manager_id"),
                                      slack_id=e.get("slack_id"), phone=e.get("phone"))

    def migrate_legacy(self, *candidates):
        """The single-company database of earlier versions becomes the demo company's file (copied with SQLite's backup API, so a WAL is safe)."""
        target = self.path(DEMO_ORG_ID)
        if os.path.exists(target):
            return False
        for c in candidates:
            if c and os.path.isfile(c) and os.path.abspath(c) != os.path.abspath(target):
                src, dst = sqlite3.connect(c), sqlite3.connect(target)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
                return True
        return False


def open_platform(data_dir, signup_codes=()):
    """Open the platform database and its registry, seed the demo company and move an old database into it if there is one."""
    from . import accounts as ac
    acc = ac.Accounts(Store(os.path.join(data_dir, "platform.db"), schema=ac.SCHEMA))
    acc.sync_codes(list(signup_codes))
    reg = OrgRegistry(acc, data_dir)
    reg.seed_demo()
    reg.migrate_legacy(os.environ.get("DESKHAND_DB"), os.path.join(data_dir, "deskhand.db"), "deskhand.db")
    return acc, reg
