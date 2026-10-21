"""Outbound live delivery of outbox rows to Slack and Gmail through viaSocket, kept strictly apart from approvals.

Approving, rejecting or any status change only writes the outbox row. A sweep delivers the row later, so Slack or Gmail being
down can never block or undo a decision. Safeguards: off by default, a hard daily cap, an allow-list of kinds, a single email
address that is never anything but the override, exactly-once per outbox idempotency_key, and no retry when a retry could send twice."""
import html
import os
import re
import time
from contextlib import contextmanager

from . import viasocket_adapter as vs

LIVE_KINDS = {"approval_request", "approved", "rejected", "fulfilled", "escalation_1", "escalation_2", "parked"}
LINK_KINDS = {"approval_request", "escalation_1", "escalation_2", "parked"}  # addressed to someone who must act in the Deskhand UI
DAILY_CAP = 40
MAX_ATTEMPTS = 3
SENDING_TIMEOUT_S = 300
ONE_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())  # real UTC day: the cap must not move with the demo clock


def slack_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # no <!channel>, <@U..> or links from message text


class Delivery:
    def __init__(self, engine, integration_factory=None, base_url=None):
        self.e = engine
        self.factory = integration_factory or (lambda: vs.get_integration(engine.s))
        self.base_url = (base_url or os.environ.get("DESKHAND_BASE_URL") or "http://localhost:8000").rstrip("/")

    # ---------- switches ----------
    def toggle(self):
        v = self.e.s.get_setting("live_delivery")
        if v is None:
            v = os.environ.get("DESKHAND_LIVE_DELIVERY", "off")
        return str(v).lower() == "on"

    def set_toggle(self, on, actor_id, why=""):
        self.e.s.set_setting("live_delivery", "on" if on else "off")
        self.e.audit(None, actor_id, "live_delivery_" + ("on" if on else "off"), {"why": why})
        if not on:
            self.e.s.q("UPDATE outbox SET delivery_status='skipped', delivery_note='live delivery switched off' WHERE delivery_status='pending'")

    def email_override(self):
        v = (os.environ.get("DESKHAND_LIVE_EMAIL_OVERRIDE") or "").strip()
        return v if ONE_EMAIL.match(v) else ""

    def active(self):
        """Live only when switched on, not forced to mock, and the integration really is viaSocket."""
        return self.toggle() and not self.e._mock_depth and self.factory().mode == "viasocket"

    def used_today(self):
        return self.e.s.one("SELECT COUNT(*) c FROM delivery_ledger WHERE day=?", (_today(),))["c"]

    def capped(self):
        return self.used_today() >= DAILY_CAP

    def status_for(self, kind, channel):
        """Decided when the row is written: only rows created while live is active and of an allowed kind wait for delivery."""
        if kind in LIVE_KINDS and channel in ("chat", "email") and self.active() and not self.capped():
            return "pending"
        return "skipped"

    # ---------- the sweep ----------
    def summary(self):
        counts = {r["delivery_status"]: r["c"] for r in self.e.s.all("SELECT delivery_status, COUNT(*) c FROM outbox GROUP BY delivery_status")}
        return {"on": self.toggle() and self.factory().mode == "viasocket", "toggle": self.toggle(), "used_today": self.used_today(), "cap": DAILY_CAP,
                "capped": self.capped(), "counts": counts, "email_override_set": bool(self.email_override())}

    def deliver_pending(self, limit=20):
        out = {"delivered": 0, "failed": 0, "retry": 0, "skipped": 0}
        if not self.active():
            return out
        now = self.e.now()
        for r in self.e.s.all("SELECT id FROM outbox WHERE delivery_status='sending' AND delivery_next_at<?", (time.time() - SENDING_TIMEOUT_S,)):
            self._fail(r["id"], "outcome unknown: the app stopped before an answer; check the channel before retrying", unknown=True)
        rows = self.e.s.all("SELECT * FROM outbox WHERE delivery_status='pending' AND (delivery_next_at IS NULL OR delivery_next_at<=?) ORDER BY id LIMIT ?", (now, limit))
        for row in rows:
            if self.capped():
                self._hit_cap()
                out["skipped"] += 1
                break
            res = self._deliver_one(row)
            out[res] = out.get(res, 0) + 1
        return out

    def _hit_cap(self):
        self.e.audit(None, "system", "live_delivery_cap_reached", {"cap": DAILY_CAP})
        self.e.s.q("UPDATE outbox SET delivery_status='skipped', delivery_note='daily live-delivery cap reached' WHERE delivery_status='pending'")
        self.e.s.set_setting("live_delivery", "off")  # stop live delivery; an admin turns it back on tomorrow

    def _deliver_one(self, row):
        e, ik = self.e, row["idempotency_key"]
        if row["kind"] not in LIVE_KINDS:  # second line of defence: the allow-list is enforced again at send time
            e.s.q("UPDATE outbox SET delivery_status='skipped', delivery_note='kind is not delivered live' WHERE id=?", (row["id"],))
            return "skipped"
        # exactly once: an earlier success for this key means never send again
        done = e.s.one("SELECT status FROM action_runs WHERE idempotency_key=?", ("deliver:" + ik,))
        if done and done["status"] == "succeeded":
            e.s.q("UPDATE outbox SET delivery_status='delivered' WHERE id=?", (row["id"],))
            e.audit(row["request_id"], "system", "delivery_idempotent_hit", {"outbox": row["id"]})
            return "delivered"
        # claim the row so a second sweep or a crash can never send it twice
        cur = e.s.q("UPDATE outbox SET delivery_status='sending', delivery_attempts=delivery_attempts+1, delivery_next_at=? WHERE id=? AND delivery_status='pending'",
                    (time.time(), row["id"]))
        if cur.rowcount != 1:
            return "skipped"
        attempt = row["delivery_attempts"] + 1
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
        label = f"[Deskhand demo to {name}]"
        subject = re.sub(r"[\r\n]+", " ", row["subject"] or f"Request #{row['request_id']}")[:150]
        body = vs.redact(row["body"] or "")
        link = f"{self.base_url}/#inbox/{row['to_person']}" if row["kind"] in LINK_KINDS else ""
        integ = self.factory()
        if row["channel"] == "email":
            to = self.email_override()
            if not to:
                return vs.Result(vs.PERMANENT, error="no single live email address is configured (DESKHAND_LIVE_EMAIL_OVERRIDE)", code="no_override")
            html_body = "<p>" + html.escape(body).replace("\n", "<br>") + "</p>" + (f'<p><a href="{html.escape(link)}">Open your Deskhand inbox</a></p>' if link else "")
            assert to == self.email_override()  # never any other address
            return integ.gmail_send(to, f"{label} {subject}", html_body)
        text = f"{label} {slack_escape(subject)}\n{slack_escape(body)}" + (f"\nOpen your Deskhand inbox: {link}" if link else "")
        return integ.slack_send(text)


@contextmanager
def forced_mock(engine):
    """Chaos runs, tests and Reset demo use this: rows written inside are never delivered live and the sweep does nothing."""
    engine._mock_depth += 1
    try:
        yield
    finally:
        engine._mock_depth -= 1
