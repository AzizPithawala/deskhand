"""`make preflight`: the state that matters before a first live run. Prints no ids, secrets or addresses."""
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
    lines = [("integration mode", os.environ.get("DESKHAND_INTEGRATION", "mock").lower()),
             ("viaSocket values set", "yes" if vs.VsClient(tenant).configured() else "NO")]
    if not os.path.exists(db_path):
        lines += [("database", "none yet (it is created on first start)"), ("live delivery", "off"), ("cap used today", f"0/{dl.DAILY_CAP}")]
        slack = gmail = "not connected"
        channel = "no"
    else:
        s = Store(db_path)
        toggle = s.get_setting("live_delivery")
        on = str(toggle if toggle is not None else os.environ.get("DESKHAND_LIVE_DELIVERY", "off")).lower() == "on"
        used = s.one("SELECT COUNT(*) c FROM delivery_ledger WHERE day=?", (dl._today(),))["c"]
        cs = vs.ConnectionStore(s, tenant)
        st = {k: (cs.get(k) or {}) for k in ("slack", "gmail")}
        slack = "connected" if st["slack"].get("script_id") else "not connected"
        gmail = "connected" if st["gmail"].get("script_id") else "not connected"
        channel = "yes" if st["slack"].get("settings", {}).get("channel_id") else "no"
        pend = {r["delivery_status"]: r["c"] for r in s.all("SELECT delivery_status, COUNT(*) c FROM outbox GROUP BY delivery_status")}
        lines += [("database", os.path.basename(db_path)), ("live delivery", "ON" if on else "off"), ("cap used today", f"{used}/{dl.DAILY_CAP}" + (" (CAP REACHED)" if used >= dl.DAILY_CAP else "")),
                  ("outbox delivery status", ", ".join(f"{k} {v}" for k, v in sorted(pend.items())) or "empty")]
    override = (os.environ.get("DESKHAND_LIVE_EMAIL_OVERRIDE") or "").strip()
    lines += [("email override is one valid address", "yes" if dl.ONE_EMAIL.match(override) else ("NO (not a single valid address)" if override else "NO (not set: no live email will be sent)")),
              ("Slack", slack), ("Slack channel chosen", channel), ("Gmail", gmail), ("commit checked out", _git())]
    return lines


if __name__ == "__main__":
    vs.load_dotenv()
    for k, v in report():
        print(f"{k + ':':38} {v}")
    sys.exit(0)
