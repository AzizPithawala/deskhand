"""Outbound delivery of outbox rows to Slack and Gmail through viaSocket. The app is LIVE-ONLY: there is no switch and no mock fallback.
Delivery starts by itself once an admin has connected Slack and chosen a channel (chat rows) and connected Gmail and entered the notification email
(email rows). It is kept strictly apart from approvals: approving or rejecting only writes the outbox row, and a sweep delivers it later, so a down
Slack or Gmail can never block or undo a decision. Safeguards: a daily backstop cap, exactly-once per outbox idempotency_key (atomic claim, single-flight
sweep), no automatic resend when the outcome is unknown, escaped message text, and email only ever to the one stored notification address."""
import html
import os
import re
import threading
import time
from contextlib import contextmanager

from . import viasocket_adapter as vs

# Every kind is delivered, except the system notice about a failed delivery: if Slack is down, delivering that notice through Slack would fail
# and create the next notice, forever. It stays in the outbox (and on the Connections tab and dashboard).
NOT_DELIVERED_KINDS = {"delivery_failed"}
LINK_KINDS = {"approval_request", "reminder", "escalation_1", "escalation_2", "task", "parked", "breaker"}  # addressed to someone who acts in the Deskhand UI
DAILY_CAP = 150
MAX_ATTEMPTS = 3
SENDING_TIMEOUT_S = 300
ONE_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())  # real UTC day: the cap must not move with the demo clock


def slack_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # no <!channel>, <@U..> or links from message text


class Delivery:
    def __init__(self, engine, integration_factory=None, base_url=None, tenant=None):
        self.e = engine
        self.tenant = tenant or os.environ.get("DESKHAND_TENANT_ID", "acme-demo")
        self.conns = vs.ConnectionStore(engine.s, self.tenant)
        self.factory = integration_factory or (lambda: vs.get_integration(engine.s, self.tenant))
        self.base_url = (base_url or os.environ.get("DESKHAND_BASE_URL") or "http://localhost:8000").rstrip("/")
        self._flight = threading.Lock()  # single-flight: only one sweep runs at a time (the 10 s worker and the manual endpoint share it)

    # ---------- readiness: configuration lives in the database, set on the Connections tab ----------
    def _conn(self, app):
        c = self.conns.get(app)
        return c if c and c["script_id"] else None

    def notify_email(self):
        """The one stored notification address, or '' when unset or not a single valid address. Email never goes anywhere else."""
        c = self._conn("gmail")
        v = ((c or {}).get("settings", {}).get("notify_email") or "").strip()
        return v if ONE_EMAIL.match(v) else ""

    def problems(self):
        """Why delivery is not (fully) working right now; shown as a red banner on every tab. Empty list means all good."""
        out = []
        if not vs.VsClient(self.tenant).configured():
            return ["viaSocket is not configured on the server: notifications are not being delivered"]
        slack = self._conn("slack")
        if not slack:
            out.append("Slack not connected: notifications are not being delivered")
        elif not slack["settings"].get("channel_id"):
            out.append("Slack channel not chosen: notifications are not being delivered")
        if not self._conn("gmail"):
            out.append("Gmail not connected: email notifications are not being delivered")
        elif not self.notify_email():
            out.append("Notification email not set: email notifications are not being delivered")
        return out

    def ready(self, channel):
        if getattr(self.e, "is_demo", False):  # the demo company never sends, even if it inherited real connection rows from an older database
            return False
        if not vs.VsClient(self.tenant).configured():
            return False
        if channel == "chat":
            c = self._conn("slack")
            return bool(c and c["settings"].get("channel_id"))
        if channel == "email":
            return bool(self._conn("gmail") and self.notify_email())
        return False

    def used_today(self):
        return self.e.s.one("SELECT COUNT(*) c FROM delivery_ledger WHERE day=?", (_today(),))["c"]

    def capped(self):
        return self.used_today() >= DAILY_CAP

    def status_for(self, kind, channel):
        """Decided when the row is written: (status, note). Only rows that can really be delivered now wait for the sweep."""
        if kind in NOT_DELIVERED_KINDS or channel not in ("chat", "email"):
            return "skipped", "system notice, outbox only"
        if self.capped():
            return "skipped", "daily delivery cap reached"
        if not self.ready(channel):
            return "skipped", ("Slack" if channel == "chat" else "Gmail") + " not connected or not configured"
        return "pending", None

    # ---------- the sweep ----------
    def summary(self):
        counts = {r["delivery_status"]: r["c"] for r in self.e.s.all("SELECT delivery_status, COUNT(*) c FROM outbox GROUP BY delivery_status")}
        return {"used_today": self.used_today(), "cap": DAILY_CAP, "capped": self.capped(), "counts": counts,
                "ready": {"slack": self.ready("chat"), "email": self.ready("email")}, "problems": self.problems()}

    def deliver_pending(self, limit=20):
        out = {"delivered": 0, "failed": 0, "retry": 0, "skipped": 0}
        if self.e._mock_depth:  # chaos and Reset demo never deliver
            return out
        if not self._flight.acquire(blocking=False):  # a sweep is already running: skip, do not queue behind it
            return {**out, "busy": True}
        try:
            return self._sweep(out, limit)
        finally:
            self._flight.release()

    def _sweep(self, out, limit):
        now = self.e.now()
        for r in self.e.s.all("SELECT id FROM outbox WHERE delivery_status='sending' AND delivery_next_at<?", (time.time() - SENDING_TIMEOUT_S,)):
            self._fail(r["id"], "outcome unknown: the app stopped before an answer; check the channel before retrying", unknown=True)
        rows = self.e.s.all("SELECT * FROM outbox WHERE delivery_status='pending' AND (delivery_next_at IS NULL OR delivery_next_at<=?) ORDER BY id LIMIT ?", (now, limit))
        for row in rows:
            if self.capped():
                self._hit_cap()
                out["skipped"] += 1
                break
            if not self.ready(row["channel"]):  # disconnected since the row was written: not delivered, the banner says so
                self.e.s.q("UPDATE outbox SET delivery_status='skipped', delivery_note='not connected or not configured' WHERE id=? AND delivery_status='pending'", (row["id"],))
                out["skipped"] += 1
                continue
            res = self._deliver_one(row)
            out[res] = out.get(res, 0) + 1
        return out

    def _hit_cap(self):
        self.e.audit(None, "system", "live_delivery_cap_reached", {"cap": DAILY_CAP})
        self.e.s.q("UPDATE outbox SET delivery_status='skipped', delivery_note='daily delivery cap reached' WHERE delivery_status='pending'")

    def _deliver_one(self, row):
        e, ik = self.e, row["idempotency_key"]
        if row["kind"] in NOT_DELIVERED_KINDS:  # second line of defence at send time
            e.s.q("UPDATE outbox SET delivery_status='skipped', delivery_note='system notice, outbox only' WHERE id=? AND delivery_status='pending'", (row["id"],))
            return "skipped"
        # 1. ATOMIC CLAIM. One UPDATE with the condition in its WHERE; only the caller that changed exactly one row may send.
        #    Any other sweep (the 10 s worker, the manual endpoint, a second process on the same database) changes 0 rows and walks away.
        cur = e.s.q("UPDATE outbox SET delivery_status='sending', delivery_attempts=delivery_attempts+1, delivery_next_at=? WHERE id=? AND delivery_status='pending'",
                    (time.time(), row["id"]))
        if cur.rowcount != 1:
            return "skipped"
        # 2. exactly once: an earlier success recorded for this key means never send again
        done = e.s.one("SELECT status FROM action_runs WHERE idempotency_key=?", ("deliver:" + ik,))
        if done and done["status"] == "succeeded":
            e.s.q("UPDATE outbox SET delivery_status='delivered' WHERE id=?", (row["id"],))
            e.audit(row["request_id"], "system", "delivery_idempotent_hit", {"outbox": row["id"]})
            return "delivered"
        attempt = e.s.one("SELECT delivery_attempts a FROM outbox WHERE id=?", (row["id"],))["a"]  # re-read: our snapshot of the row may be stale
        if not e.s.one("SELECT 1 x FROM action_runs WHERE idempotency_key=?", ("deliver:" + ik,)):
            e.s.q("INSERT INTO action_runs(request_id,step,idempotency_key,attempts,status,updated_at) VALUES(?,?,?,?,?,?)", (row["request_id"], "deliver", "deliver:" + ik, 0, "pending", e.now()))
        e.s.q("UPDATE action_runs SET attempts=?, updated_at=? WHERE idempotency_key=?", (attempt, e.now(), "deliver:" + ik))
        e.s.q("INSERT INTO delivery_ledger(day,ikey,ts) VALUES(?,?,?)", (_today(), f"{ik}#{attempt}", time.time()))  # counts every real send attempt
        try:
            res = self._send(row)
        except Exception as ex:  # a bug here must never break approvals
            res = vs.Result(vs.PERMANENT, error=vs.redact(f"delivery error: {type(ex).__name__}"), code="exception")
        if res.ok:
            e.s.q("UPDATE outbox SET delivery_status='delivered', delivered_at=?, delivery_error=NULL WHERE id=?", (e.now(), row["id"]))
            e.s.q("UPDATE action_runs SET status='succeeded' WHERE idempotency_key=?", ("deliver:" + ik,))
            e.audit(row["request_id"], "system", "delivery_succeeded", {"outbox": row["id"], "kind": row["kind"], "channel": row["channel"], "attempt": attempt})
            return "delivered"
        if res.kind == vs.RETRYABLE and res.safe_retry and attempt < MAX_ATTEMPTS:
            back = [1, 4, 16][min(attempt - 1, 2)]
            e.s.q("UPDATE outbox SET delivery_status='pending', delivery_error=?, delivery_next_at=? WHERE id=?", (res.error[:200], e.now() + back, row["id"]))
            e.audit(row["request_id"], "system", "delivery_retry", {"outbox": row["id"], "attempt": attempt, "error": res.error[:120], "next_backoff_s": back})
            return "retry"
        unknown = res.kind == vs.RETRYABLE and not res.safe_retry
        self._fail(row["id"], res.error + (" (outcome unknown: it may have been delivered; check before retrying)" if unknown else ""), unknown=unknown, attempt=attempt)
        return "failed"

    def _fail(self, outbox_id, error, unknown=False, attempt=0):
        e = self.e
        row = e.s.one("SELECT request_id, kind FROM outbox WHERE id=?", (outbox_id,))
        e.s.q("UPDATE outbox SET delivery_status='failed', delivery_error=? WHERE id=?", (vs.redact(error)[:300], outbox_id))
        e.audit(row["request_id"], "system", "delivery_failed", {"outbox": outbox_id, "kind": row["kind"], "attempt": attempt, "unknown_outcome": unknown, "error": vs.redact(error)[:160]})
        admin = e.d.role_holder("admin")
        # the admin notice itself is outbox-only (not a live kind), so a failing channel cannot loop
        e._notify(row["request_id"], admin["id"] if admin else None, f"Delivery of a '{row['kind']}' message failed and was parked: {vs.redact(error)[:160]}. Retry it from the Connections tab.",
                  "delivery_failed", n=outbox_id, subject="Needs attention: delivery failed", channel="chat")

    def retry_failed(self, outbox_id, actor_id):
        if self.capped():
            raise ValueError("the daily live-delivery cap is reached")
        row = self.e.s.one("SELECT * FROM outbox WHERE id=? AND delivery_status='failed'", (outbox_id,))
        if not row:
            raise ValueError("that delivery is not failed")
        self.e.s.q("UPDATE outbox SET delivery_status='pending', delivery_error=NULL, delivery_next_at=NULL WHERE id=?", (outbox_id,))
        self.e.audit(row["request_id"], actor_id, "delivery_replayed", {"outbox": outbox_id})

    # ---------- composing and sending ----------
    def _send(self, row):
        e = self.e
        name = e.name(row["to_person"])
        subject = re.sub(r"[\r\n]+", " ", row["subject"] or f"Request #{row['request_id']}")[:150]
        body = vs.redact(row["body"] or "")
        link = f"{self.base_url}/#inbox/{row['to_person']}" if row["kind"] in LINK_KINDS else ""
        integ = self.factory()
        if row["channel"] == "email":
            to = self.notify_email()
            if not to:
                return vs.Result(vs.PERMANENT, error="no single notification email is set on the Connections tab", code="no_notify_email")
            html_body = "<p>" + html.escape(body).replace("\n", "<br>") + "</p>" + (f'<p><a href="{html.escape(link)}">Open your Deskhand inbox</a></p>' if link else "")
            assert to == self.notify_email()  # never any other address
            return integ.gmail_send(to, f"To {name}: {subject}", html_body)
        text = f"[Deskhand to {name}] {slack_escape(subject)}\n{slack_escape(body)}" + (f"\nOpen your Deskhand inbox: {link}" if link else "")
        return integ.slack_send(text)


@contextmanager
def forced_mock(engine):
    """Chaos runs and Reset demo use this: rows written inside are marked skipped and the sweep does nothing."""
    engine._mock_depth += 1
    try:
        yield
    finally:
        engine._mock_depth -= 1
