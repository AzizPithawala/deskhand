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
from urllib.parse import urlsplit
from contextlib import contextmanager

from . import notifications, viasocket_adapter as vs

# The system notice about a failed delivery is never delivered: if Slack is down, delivering that notice through Slack would fail and create the next notice,
# forever. It stays in the outbox (and on Integrations and the dashboard).
NOT_DELIVERED_KINDS = {"delivery_failed"}
# Who gets what (docs/AGENT_PLATFORM_PLAN.md). Chat replies, policy answers and "request received" stay in the Deskhand chat; everything that needs someone's attention
# or tells them a decision goes to their Slack DM, and the important ones also by email.
SLACK_KINDS = {"approval_request", "reminder", "escalation_1", "escalation_2", "escalated", "stage_approved", "approved", "rejected", "pending_update", "task", "assigned", "fulfilled",
               "parked", "delay", "breaker", "approval_fallback", "reject_link", "decision_ack"}
EMAIL_KINDS = {"approval_request", "reminder", "escalation_1", "escalation_2", "escalated", "approved", "rejected", "fulfilled", "task", "parked"}
NEEDS_LINK_KINDS = {"approval_request", "reminder", "escalation_1", "escalation_2", "task", "parked", "approval_fallback", "reject_link"}  # addressed to someone who acts: link to the decision page
FROM_REQUESTER_KINDS = {"approval_request"}  # the request email comes from the requester's own mailbox when they connected one
FROM_DECIDER_KINDS = {"approved", "rejected", "fulfilled", "stage_approved"}  # decisions and completion come from the approver's own mailbox when they connected one
DAILY_CAP = 150
MAX_ATTEMPTS = 3
SENDING_TIMEOUT_S = 300
ONE_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())  # real UTC day: the cap must not move with the demo clock


def slack_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # no <!channel>, <@U..> or links from message text


class Delivery:
    def __init__(self, engine, integration_factory=None, base_url=None, tenant=None, personal_factory=None, fanout=None):
        self.e = engine
        self.tenant = tenant or os.environ.get("DESKHAND_TENANT_ID", "acme-demo")
        self.conns = vs.ConnectionStore(engine.s, self.tenant)
        self.factory = integration_factory or (lambda: vs.get_integration(engine.s, self.tenant))  # the company's Slack and Gmail
        self.personal_factory = personal_factory or (lambda tenant: vs.get_integration(engine.s, tenant))  # one person's own mailbox
        self.fanout = (not getattr(engine, "is_demo", False)) if fanout is None else fanout  # one message becomes a Slack DM row and an email row
        self.base_url = (base_url or os.environ.get("DESKHAND_BASE_URL") or "http://localhost:8000").rstrip("/")
        self._flight = threading.Lock()  # single-flight: only one sweep runs at a time (the 10 s worker and the manual endpoint share it)

    # ---------- mode: rehearsal sends everything to one admin, live sends to the real people ----------
    def mode(self):
        return "live" if self.e.s.get_setting("delivery_mode") == "live" else "rehearsal"

    def rehearsal_person(self):
        pid = self.e.s.get_setting("rehearsal_person")
        p = self.e.d.emp(pid) if pid else None
        return p if p and p["active"] and p.get("role") == "admin" else self.e.d.role_holder("admin")

    def recipient(self, person_id):
        """The directory person a message really goes to: in rehearsal always the rehearsing admin, in live the person it is for. Never anyone outside the directory."""
        if self.mode() != "live":
            return self.rehearsal_person()
        p = self.e.d.emp(person_id)
        return p if p and p["active"] else None

    def set_mode(self, mode, actor, person_id=None):
        if mode not in ("rehearsal", "live"):
            raise ValueError("choose rehearsal or live")
        who = self.e.d.emp(person_id) if person_id else self.rehearsal_person()
        if not who or not who["active"] or who.get("role") != "admin":
            raise ValueError("the person who receives rehearsal messages must be an admin who has joined")
        old = (self.mode(), (self.rehearsal_person() or {}).get("id"))
        dropped = 0
        if mode != old[0]:  # nothing written under one mode is sent under the other: a rehearsal message must never reach a real person, nor a live one be redirected
            dropped = self.e.s.q("UPDATE outbox SET delivery_status='skipped', delivery_note='written before the mode changed, not sent' WHERE delivery_status IN ('pending','failed')").rowcount
        self.e.s.set_setting("delivery_mode", mode)
        self.e.s.set_setting("rehearsal_person", who["id"])
        self.e.audit(None, actor, "delivery_mode_changed", {"mode": mode, "rehearsal_person": who["id"], "was": list(old), "unsent_messages_dropped": dropped})

    # ---------- readiness: configuration lives in the database, set on the Integrations page ----------
    def _conn(self, app):
        c = self.conns.get(app)
        return c if c and c["script_id"] else None

    def _personal_gmail(self, person_id):
        if not person_id:
            return None
        c = vs.ConnectionStore(self.e.s, vs.personal_tenant(self.tenant, person_id)).get("gmail")
        return c if c and c["script_id"] else None

    def problems(self):
        """Why delivery is not (fully) working right now; shown as a red banner to admins. Empty list means all good."""
        out = []
        if not vs.VsClient(self.tenant).configured():
            return ["viaSocket is not configured on the server: notifications are not being delivered"]
        if not self._conn("slack"):
            out.append("Slack not connected: notifications are not being delivered")
        if not self._conn("gmail"):
            out.append("Gmail not connected: email notifications are not being delivered")
        if not self.public_address():
            out.append("Deskhand's public address is not set (DESKHAND_BASE_URL): links in Slack messages and emails would point to this computer, and Slack buttons cannot work")
        return out

    def public_address(self):
        """False when links would point to the machine itself instead of a place colleagues can open."""
        host = (urlsplit(self.base_url).hostname or "").lower()
        return urlsplit(self.base_url).scheme in ("http", "https") and host not in ("", "localhost", "127.0.0.1", "0.0.0.0", "::1")

    def notes(self):
        """Things an admin should know that are not faults: which mode is on, and who has no Slack account matched."""
        who = self.rehearsal_person()
        if self.mode() != "live":
            return [f"Rehearsal: every Slack message and email goes only to {who['name'] if who else 'an admin'}, labelled with the person it is for. Go live on Integrations when it looks right."]
        missing = [e["name"] for e in self.e.d.employees.values() if e["active"] and not e.get("slack_id")]
        return [f"{len(missing)} people have no Slack account matched, so they get email only: {', '.join(missing[:5])}{'…' if len(missing) > 5 else ''}."] if missing else []

    def ready(self, channel):
        if getattr(self.e, "is_demo", False):  # the demo company never sends, even if it inherited real connection rows from an older database
            return False
        if not vs.VsClient(self.tenant).configured():
            return False
        if channel == "chat":
            return bool(self._conn("slack"))
        if channel == "email":
            return bool(self._conn("gmail"))
        return False

    def buttons_ready(self):
        """Approve / Reject buttons are used only in live mode, once an admin switched them on, and only where viaSocket can reach us (a public https address)."""
        flow = self.conns.get("slack_flow")
        return self.mode() == "live" and bool(flow and flow["script_id"]) and self.base_url.startswith("https://")

    def used_today(self):
        return self.e.s.one("SELECT COUNT(*) c FROM delivery_ledger WHERE day=?", (_today(),))["c"]

    def capped(self):
        return self.used_today() >= DAILY_CAP

    def status_for(self, kind, channel, to_id=None):
        """Decided when the row is written: (status, note). Only rows that can really be delivered now wait for the sweep."""
        if kind in NOT_DELIVERED_KINDS or channel not in ("chat", "email"):
            return "skipped", "system notice, outbox only"
        if channel == "chat" and kind not in SLACK_KINDS:
            return "skipped", "in the Deskhand chat only"
        if channel == "email" and kind not in EMAIL_KINDS:
            return "skipped", "email is not sent for this kind"
        if self.capped():
            return "skipped", "daily delivery cap reached"
        if not self.ready(channel):
            return "skipped", ("Slack" if channel == "chat" else "Gmail") + " not connected or not configured"
        if to_id is not None and self.mode() == "live" and not notifications.allowed(self.e.s, to_id, kind, channel):  # rehearsal ignores preferences: the admin sees everything
            return "skipped", notifications.OFF_NOTE
        if to_id is not None:
            rec = self.recipient(to_id)
            if not rec:
                return "skipped", "no active person to deliver to"
            if channel == "chat" and not rec.get("slack_id"):
                return "skipped", f"no Slack account matched for {rec['name']}"
            if channel == "email" and not rec.get("email"):
                return "skipped", f"no email address for {rec['name']}"
        return "pending", None

    # ---------- the sweep ----------
    def summary(self):
        counts = {r["delivery_status"]: r["c"] for r in self.e.s.all("SELECT delivery_status, COUNT(*) c FROM outbox GROUP BY delivery_status")}
        who = self.rehearsal_person()
        people = [e for e in self.e.d.employees.values() if e["active"]]
        return {"used_today": self.used_today(), "cap": DAILY_CAP, "capped": self.capped(), "counts": counts, "mode": self.mode(),
                "rehearsal_person": {"id": who["id"], "name": who["name"]} if who else None,
                "slack_matched": sum(1 for e in people if e.get("slack_id")), "people": len(people),
                "buttons": {"enabled": bool((self.conns.get("slack_flow") or {}).get("script_id")), "public_url": self.base_url.startswith("https://")},
                "ready": {"slack": self.ready("chat"), "email": self.ready("email")}, "problems": self.problems(), "notes": self.notes()}

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
        target = self.recipient(row["to_person"])  # who Slack or Gmail is really asked to reach: the rehearsal admin in rehearsal, the person in live
        try:
            res = self._send(row)
        except Exception as ex:  # a bug here must never break approvals
            res = vs.Result(vs.PERMANENT, error=vs.redact(f"delivery error: {type(ex).__name__}"), code="exception")
        if res.ok:
            e.s.q("UPDATE outbox SET delivery_status='delivered', delivered_at=?, delivery_error=NULL, sent_to=? WHERE id=?", (e.now(), (target or {}).get("id"), row["id"]))
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
    def _link(self, row):
        rid = row["request_id"]
        if not rid:
            return self.base_url
        return f"{self.base_url}/#{'needs' if row['kind'] in NEEDS_LINK_KINDS else 'request'}/{rid}"

    def _send(self, row):
        e = self.e
        rec = self.recipient(row["to_person"])
        if not rec:
            return vs.Result(vs.PERMANENT, error="there is no active person to deliver this to", code="no_recipient")
        rehearsal = self.mode() != "live"
        tag = f"[Rehearsal] To {e.name(row['to_person'])}: " if rehearsal else ""
        subject = re.sub(r"[\r\n]+", " ", row["subject"] or f"Request #{row['request_id']}")[:150]
        body = vs.redact(row["body"] or "")
        link = self._link(row)
        if row["channel"] == "email":
            if not rec.get("email"):
                return vs.Result(vs.PERMANENT, error=f"{rec['name']} has no email address", code="no_address")
            intro = f"Rehearsal: this is the message {e.name(row['to_person'])} would get once Deskhand is live." if rehearsal else ""
            html_body = ("<p><i>" + html.escape(intro) + "</i></p>" if intro else "") + "<p>" + html.escape(body).replace("\n", "<br>") + f'</p><p><a href="{html.escape(link)}">Open it in Deskhand</a></p>'
            mailbox = row.get("from_person")
            if self._personal_gmail(mailbox):  # their own mailbox; if it cannot send (nothing was sent), the company mailbox does
                res = self.personal_factory(vs.personal_tenant(self.tenant, mailbox)).gmail_send(rec["email"], tag + subject, html_body)
                if res.ok or res.kind != vs.PERMANENT:
                    return res
            return self.factory().gmail_send(rec["email"], tag + subject, html_body)
        if not rec.get("slack_id"):
            return vs.Result(vs.PERMANENT, error=f"no Slack account is matched for {rec['name']}", code="no_slack_id")
        text = f"{slack_escape(tag)}*{slack_escape(subject)}*\n{slack_escape(body)}\nOpen it: {link}"
        if row["kind"] == "approval_request" and not rehearsal and self.buttons_ready():  # live approvals can be decided from Slack
            from . import slack_decisions as sd  # here, not at the top: the engine imports this module
            token = sd.issue(e, self.tenant, "approval", row["request_id"], row["to_person"], row["id"])
            res = self.factory().slack_approval(rec["slack_id"], text, token)
            if res.ok or res.kind != vs.PERMANENT:
                return res
            # a certain failure to start the buttons: nothing went out, so the plain message with the link goes instead
        return self.factory().slack_dm(rec["slack_id"], text)


@contextmanager
def forced_mock(engine):
    """Chaos runs and Reset demo use this: rows written inside are marked skipped and the sweep does nothing."""
    engine._mock_depth += 1
    try:
        yield
    finally:
        engine._mock_depth -= 1
