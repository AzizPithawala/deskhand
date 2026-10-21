"""`make preflight`: is delivery set up and working? Prints yes/no only: no ids, secrets or addresses."""
import os
import subprocess
import sqlite3
import sys

from . import delivery as dl, viasocket_adapter as vs
from .db import Store


def _git():
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10).stdout.strip())
        return h + (" (uncommitted changes present)" if dirty else " (clean)")
    except Exception:
        return "unknown"


def report(db_path=None, tenant=None):
    db_path = db_path or os.environ.get("DESKHAND_DB", "deskhand.db")
    tenant = tenant or os.environ.get("DESKHAND_TENANT_ID", "acme-demo")
    yn = lambda b: "yes" if b else "NO"
    lines = [("viaSocket values set on the server", yn(vs.VsClient(tenant).configured())),
             ("password gate configured", yn(bool(os.environ.get("DESKHAND_DEMO_PASSWORD"))))]
    if not os.path.exists(db_path):
        lines += [("database", "none yet (it is created on first start)"), ("Slack connected", "NO"), ("Slack channel chosen", "NO"), ("Gmail connected", "NO"),
                  ("Notification email set (one valid address)", "NO"), ("delivery is working", "NO"), ("cap used today", f"0/{dl.DAILY_CAP}")]
    else:
        s = Store(db_path)
        used = s.one("SELECT COUNT(*) c FROM delivery_ledger WHERE day=?", (dl._today(),))["c"]
        cs = vs.ConnectionStore(s, tenant)
        sl, gm = cs.get("slack") or {}, cs.get("gmail") or {}
        slack_ok, gmail_ok = bool(sl.get("script_id")), bool(gm.get("script_id"))
        chan = bool(sl.get("settings", {}).get("channel_id"))
        email = bool(dl.ONE_EMAIL.match((gm.get("settings", {}).get("notify_email") or "").strip()))
        pend = {r["delivery_status"]: r["c"] for r in s.all("SELECT delivery_status, COUNT(*) c FROM outbox GROUP BY delivery_status")}
        working = vs.VsClient(tenant).configured() and slack_ok and chan and gmail_ok and email and used < dl.DAILY_CAP
        lines += [("database", os.path.basename(db_path)), ("Slack connected", yn(slack_ok)), ("Slack channel chosen", yn(chan)), ("Gmail connected", yn(gmail_ok)),
                  ("Notification email set (one valid address)", yn(email)), ("delivery is working (all of the above)", yn(working)),
                  ("cap used today", f"{used}/{dl.DAILY_CAP}" + (" (CAP REACHED)" if used >= dl.DAILY_CAP else "")),
                  ("outbox delivery status", ", ".join(f"{k} {v}" for k, v in sorted(pend.items())) or "empty")]
    lines.append(("commit checked out", _git()))
    return lines


if __name__ == "__main__":
    vs.load_dotenv()
    for k, v in report():
        print(f"{k + ':':38} {v}")
    sys.exit(0)
