"""`make preflight`: is delivery set up and working? Prints yes/no only: no ids, secrets or addresses."""
import os
import subprocess
import sqlite3
import sys

from . import accounts as ac, delivery as dl, viasocket_adapter as vs
from .db import Store


def _git():
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10).stdout.strip())
        return h + (" (uncommitted changes present)" if dirty else " (clean)")
    except Exception:
        return "unknown"


def report(db_path=None, tenant=None):
    db_path = db_path or os.environ.get("DESKHAND_DB", "deskhand.db")  # one company's file; report_all() walks every company
    tenant = tenant or os.environ.get("DESKHAND_TENANT_ID", "acme-demo")
    yn = lambda b: "yes" if b else "NO"
    lines = [("viaSocket values set on the server", yn(vs.VsClient(tenant).configured())),
             ("password gate configured", yn(bool(os.environ.get("DESKHAND_DEMO_PASSWORD"))))]
    if not os.path.exists(db_path):
        lines += [("database", "none yet (it is created on first start)"), ("Slack connected", "NO"), ("Gmail connected", "NO"), ("messaging mode", "rehearsal"),
                  ("delivery is working", "NO"), ("cap used today", f"0/{dl.DAILY_CAP}")]
    else:
        s = Store(db_path)
        used = s.one("SELECT COUNT(*) c FROM delivery_ledger WHERE day=?", (dl._today(),))["c"]
        cs = vs.ConnectionStore(s, tenant)
        sl, gm = cs.get("slack") or {}, cs.get("gmail") or {}
        slack_ok, gmail_ok = bool(sl.get("script_id")), bool(gm.get("script_id"))
        mode = "live" if (s.get_setting("delivery_mode") == "live") else "rehearsal"
        pend = {r["delivery_status"]: r["c"] for r in s.all("SELECT delivery_status, COUNT(*) c FROM outbox GROUP BY delivery_status")}
        roles = s.one("SELECT COUNT(*) n, SUM(holder_id IS NOT NULL) h FROM roles")
        working = vs.VsClient(tenant).configured() and slack_ok and gmail_ok and used < dl.DAILY_CAP
        lines += [("database", os.path.basename(db_path)), ("Slack connected", yn(slack_ok)), ("Gmail connected", yn(gmail_ok)), ("messaging mode", mode),
                  ("delivery is working (all of the above)", yn(working)), ("cap used today", f"{used}/{dl.DAILY_CAP}" + (" (CAP REACHED)" if used >= dl.DAILY_CAP else "")),
                  ("outbox delivery status", ", ".join(f"{k} {v}" for k, v in sorted(pend.items())) or "empty"),
                  ("business roles with a holder", f"{roles['h'] or 0} of {roles['n']}")]
        st = s.one("SELECT COUNT(*) n, COALESCE(SUM(enabled), 0) e FROM type_steps")
        runs = {r["status"]: r["c"] for r in s.all("SELECT status, COUNT(*) c FROM step_runs GROUP BY status")}
        lines += [("steps in other apps", f"{st['n']} set up, {st['e']} switched on"), ("step runs", ", ".join(f"{k} {v}" for k, v in sorted(runs.items())) or "none")]
    lines.append(("commit checked out", _git()))
    return lines


GLOBAL = ("viaSocket values set on the server", "password gate configured", "commit checked out")


def report_all(data_dir=None):
    """Every company in the data directory: sign-up is open or closed, and per company whether delivery is set up. Names only, never ids or secrets."""
    data_dir = data_dir or os.environ.get("DESKHAND_DATA_DIR", ".deskhand")
    yn = lambda b: "yes" if b else "NO"
    codes = [c for c in os.environ.get("DESKHAND_SIGNUP_CODES", "").split(",") if c.strip()]
    lines = [("viaSocket values set on the server", yn(vs.VsClient("preflight").configured())),
             ("sign-up codes set (registration open)", yn(bool(codes))),
             ("shared password gate (optional)", "on" if os.environ.get("DESKHAND_DEMO_PASSWORD") else "off")]
    plat = os.path.join(data_dir, "platform.db")
    if not os.path.exists(plat):
        return lines + [("companies", "none yet (created on first start)"), ("commit checked out", _git())]
    orgs = ac.Accounts(Store(plat, schema=ac.SCHEMA)).list_orgs()
    lines.append(("companies", str(len(orgs))))
    for org in orgs:
        lines.append((f"-- {org['name']}", "demo company: nothing is ever sent" if org["is_demo"] else ""))
        if not org["is_demo"]:
            lines += [(k, v) for k, v in report(os.path.join(data_dir, "orgs", org["id"] + ".db"), org["id"]) if k not in GLOBAL and k != "database"]
    return lines + [("commit checked out", _git())]


if __name__ == "__main__":
    vs.load_dotenv()
    for k, v in report_all():
        print(f"{k + ':':38} {v}")
    sys.exit(0)
