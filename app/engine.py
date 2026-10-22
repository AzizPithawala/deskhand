"""Deskhand core: intake, routing, lifecycle state machine, guard (dedupe, own-message ignore, circuit breaker,
idempotent retried actions, parking), escalation sweeps, outbox and audit. No network access."""
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime

from . import classifier, delivery as dl
from .adapters import ActionError, MockHR
from .db import Store, dumps
from .directory import Directory

BOT_SENDER = "deskhand-bot"
FINAL = {"REJECTED", "CLOSED", "CANCELLED"}
ALLOWED = {
    "SUBMITTED": {"NEEDS_INFO", "PENDING_APPROVAL", "APPROVED", "CANCELLED"},
    "NEEDS_INFO": {"SUBMITTED", "CANCELLED"},
    "PENDING_APPROVAL": {"APPROVED", "REJECTED", "CANCELLED"},
    "APPROVED": {"FULFILLING"},
    "FULFILLING": {"FULFILLED", "PARKED", "CANCELLED"},
    "PARKED": {"FULFILLING", "CANCELLED"},
    "FULFILLED": {"CLOSED"},
    "REJECTED": set(), "CLOSED": set(), "CANCELLED": set(),
}
BACKOFF = [1, 4, 16]  # seconds, recorded in the audit log; the prototype does not sleep


def split_email(raw):
    """Emails arrive as 'Subject: ...\nbody'. Return (subject or None, body)."""
    if raw.startswith("Subject:") and "\n" in raw:
        head, body = raw.split("\n", 1)
        return head[len("Subject:"):].strip() or None, body.strip()
    return None, raw


def _day(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%a %d %b").replace(" 0", " ")
    except (TypeError, ValueError):
        return str(iso)


LABELS = {"leave_type": "Leave type", "half_day": "Half day", "estimated_cost": "Estimated cost (INR)", "access_level": "Access level"}


def details_view(d):
    """Readable (label, value) pairs for people. No Python reprs, no empty values."""
    rows = []
    for k, v in d.items():
        if k in ("date_range", "leave_check") or v in (None, "", [], False) or (k == "balance" and d.get("leave_check")):
            continue
        label = LABELS.get(k, k.replace("_", " ").capitalize())
        if k == "dates":
            val = (" to " if d.get("date_range") and len(v) == 2 else ", ").join(_day(x) for x in v)
        elif k == "balance":
            label, val = "Leave balance", ", ".join(f"{a} {b}" for a, b in v.items())
        elif v is True:
            val = "yes"
        else:
            val = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
        rows.append((label, val))
    chk = d.get("leave_check")
    if chk:  # what the leave would do to the balance, for the approver
        rows.append(("Working days", str(chk["days"])))
        rows.append(("Balance", f"{chk['type']} {chk['balance']} → {chk['after']}"))
        rows += [("Check", w) for w in chk.get("warnings", [])]
    return rows


class InvalidTransition(Exception):
    pass


class NotAuthorized(Exception):
    pass


class Refused(Exception):
    pass


class Engine:
    def __init__(self, store=None, directory=None, hr=None, api_key=None, ai_classify=None, ai_enabled=None):
        self.s = store or Store(":memory:")  # real companies pass their own database file (app/registry.py)
        self.d = directory or Directory.load()
        self.hr = hr or MockHR(self.d)
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "") if api_key is None else api_key
        self.model = os.environ.get("DESKHAND_MODEL", "claude-haiku-5-5")
        self.ai_classify = ai_classify
        self.ai_enabled = ai_enabled  # optional switch consulted before each AI call; None means always on when an AI classifier is given
        self.live = None  # set by the web app: outbound delivery (app/delivery.py). None (engine tests) means rows are only ever written to the outbox.
        self._mock_depth = 0
        self._defaults()

    def _defaults(self):
        for k, v in {"clock_offset": 0, "ai_budget_per_day": os.environ.get("DESKHAND_AI_BUDGET_PER_DAY", 200),
                     "ai_calls_today": 0, "rate_limit_per_min": os.environ.get("DESKHAND_RATE_LIMIT_PER_MIN", 20),
                     "breaker_open": ""}.items():
            if self.s.get_setting(k) is None:
                self.s.set_setting(k, v)

    def reset(self):
        self.s.reset()
        self.hr.reset()
        self._defaults()

    # ---------- clock, audit, outbox ----------
    def now(self):
        return time.time() + float(self.s.get_setting("clock_offset", 0))

    def advance_hours(self, hours):
        self.s.set_setting("clock_offset", float(self.s.get_setting("clock_offset", 0)) + hours * 3600)
        return self.now()

    def today(self):
        return datetime.fromtimestamp(self.now()).date()

    def audit(self, request_id, actor, type_, data=None):
        self.s.q("INSERT INTO events(request_id,ts,actor,type,data_json) VALUES(?,?,?,?,?)",
                 (request_id, self.now(), actor, type_, dumps(data or {})))

    SUBJECTS = {"approved": "Request #{rid} approved", "rejected": "Request #{rid} rejected", "fulfilled": "Request #{rid} is done", "stage_approved": "Request #{rid} moves on",
                "reminder": "Reminder: request #{rid} is waiting", "escalation_1": "Escalated: request #{rid}", "escalation_2": "Escalated again: request #{rid}",
                "escalated": "Request #{rid} was escalated", "task": "Task: request #{rid}", "parked": "Needs attention: request #{rid}", "pending_update": "Request #{rid} is still pending",
                "assigned": "Request #{rid} has someone on it", "delay": "Request #{rid} hit a delay"}

    def _decider(self, rid):
        """Who last approved this request: whose mailbox a decision or completion email comes from."""
        row = self.s.one("SELECT actor FROM events WHERE request_id=? AND type='approved_stage' ORDER BY id DESC LIMIT 1", (rid,)) if rid else None
        return row["actor"] if row else None

    def _notify(self, rid, to_id, body, kind, n=0, subject=None, channel=None, by=None):
        """Write a message to the outbox. In a company one message is also written as an email row for the kinds that are emailed; each row is delivered exactly once on its own."""
        if not to_id:
            return
        req = self.req(rid) if rid else None
        ch = channel or (req["channel"] if req else "chat")
        from_id = None
        if req and kind in dl.FROM_REQUESTER_KINDS:
            from_id = req["employee_id"]
        elif req and kind in dl.FROM_DECIDER_KINDS:
            from_id = by or self._decider(rid)
        self._write_row(rid, req, to_id, body, kind, n, subject, ch, from_id)
        if self.live and self.live.fanout and not self._mock_depth and ch == "chat" and kind in dl.EMAIL_KINDS:
            self._write_row(rid, req, to_id, body, kind, n, subject, "email", from_id, suffix=":email")

    def _write_row(self, rid, req, to_id, body, kind, n, subject, ch, from_id, suffix=""):
        if not subject and rid and kind in self.SUBJECTS:
            subject = self.SUBJECTS[kind].format(rid=rid)
        if ch == "email":
            first = (self.name(to_id) or "there").split()[0]
            body = f"Hi {first},\n\n{body}\n\n— Deskhand"
            if req and to_id == req["employee_id"] and req["channel"] == "email":  # a reply to an email the requester sent
                subject = "Re: " + (req["email_subject"] or f"Request #{rid}")
        key = f"{rid}:{kind}:{to_id}:{n}{suffix}"
        dstatus, dnote = self.live.status_for(kind, ch, to_id) if self.live and not self._mock_depth else ("skipped", "no delivery for this run")
        self.s.q("INSERT OR IGNORE INTO outbox(request_id,channel,to_person,subject,body,kind,idempotency_key,created_at,delivery_status,delivery_note,from_person) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (rid, ch, to_id, subject or f"Request #{rid}" if rid else subject, body, kind, key, self.now(), dstatus, dnote, from_id))

    # ---------- request helpers ----------
    def req(self, rid):
        r = self.s.one("SELECT * FROM requests WHERE id=?", (rid,))
        if not r:
            return None
        r["details"] = json.loads(r["details_json"] or "{}")
        r["chain"] = json.loads(r["chain_json"] or "[]")
        r["email_subject"], r["message"] = split_email(r["raw_text"] or "")
        r["details_view"] = details_view(r["details"])
        return r

    def name(self, eid):
        e = self.d.emp(eid)
        return e["name"] if e else "someone"

    def current_approver(self, r):
        return r["chain"][r["chain_index"]] if r["chain"] and r["chain_index"] < len(r["chain"]) else None

    def _set_status(self, rid, new, actor, why=""):
        r = self.req(rid)
        if new not in ALLOWED.get(r["status"], set()):
            raise InvalidTransition(f"{r['status']} -> {new} is not allowed")
        extra = ""
        if new in ("CLOSED", "CANCELLED", "REJECTED"):
            extra = ", closed_at=%f" % self.now()
        self.s.q(f"UPDATE requests SET status=?{extra} WHERE id=?", (new, rid))
        self.audit(rid, actor, "status_change", {"from": r["status"], "to": new, "why": why})

    def _is_admin(self, eid):
        e = self.d.emp(eid)
        return bool(e and e["role"] == "admin")

    def can_act(self, r, actor_id):
        return actor_id in {self.current_approver(r), r.get("escalated_to")} or self._is_admin(actor_id)

    # ---------- intake ----------
    def ingest(self, channel, sender, text, source_event_id=None, is_bot=False, reply_to=None):
        eid = source_event_id or uuid.uuid4().hex
        if is_bot or sender == BOT_SENDER:
            self.audit(None, sender, "own_message_ignored", {"channel": channel})
            return {"outcome": "ignored_own_message", "request_ids": []}
        try:
            self.s.q("INSERT INTO inbound_events(channel,source_event_id,received_at,outcome) VALUES(?,?,?,?)",
                     (channel, eid, self.now(), "received"))
        except sqlite3.IntegrityError:
            prev = self.s.one("SELECT id FROM requests WHERE channel=? AND source_event_id=?", (channel, eid))
            self.audit(prev["id"] if prev else None, sender, "duplicate_suppressed", {"channel": channel, "source_event_id": eid})
            return {"outcome": "duplicate_suppressed", "request_ids": [prev["id"]] if prev else []}
        if channel in self.s.get_setting("breaker_open", "").split(","):
            self.s.q("UPDATE inbound_events SET outcome='paused_breaker' WHERE channel=? AND source_event_id=?", (channel, eid))
            return {"outcome": "paused_breaker", "request_ids": []}
        since = max(self.now() - 60, float(self.s.get_setting("breaker_reset_at", 0)))
        recent = self.s.one("SELECT COUNT(*) c FROM inbound_events WHERE channel=? AND received_at>=?", (channel, since))["c"]
        if recent > int(self.s.get_setting("rate_limit_per_min")):
            opened = [c for c in self.s.get_setting("breaker_open", "").split(",") if c] + [channel]
            self.s.set_setting("breaker_open", ",".join(opened))
            self.audit(None, "system", "breaker_opened", {"channel": channel, "events_last_minute": recent})
            admin = self.d.role_holder("admin")
            self._notify(None, admin["id"] if admin else None,
                         f"Intake on channel '{channel}' was paused: {recent} events in one minute. Reset it from the Chaos tab once checked.",
                         "breaker", n=len(opened), subject="Deskhand intake paused", channel="chat")
            self.s.q("UPDATE inbound_events SET outcome='breaker_opened' WHERE channel=? AND source_event_id=?", (channel, eid))
            return {"outcome": "breaker_opened", "request_ids": []}
        emp = self.d.resolve_sender(sender)
        if not emp:
            self.audit(None, sender, "unknown_sender_refused", {"channel": channel})
            return {"outcome": "unknown_sender_refused", "request_ids": []}
        clean, had_secret = classifier.redact(text)
        if had_secret:
            self.audit(None, emp["id"], "secret_redacted", {"channel": channel})
        if reply_to:
            old = self.req(reply_to)
            if old and old["employee_id"] == emp["id"] and old["status"] == "NEEDS_INFO":
                clean = (old["raw_text"] + " " + clean).strip()
                self._set_status(old["id"], "CANCELLED", emp["id"], "superseded by the clarification")
        cls = self._classify(clean)
        if cls["suspected_injection"]:
            self.audit(None, emp["id"], "injection_suspected", {"flags": cls.get("injection_flags", [])})
        types = [i["type_code"] for i in cls["intents"]]
        if "payroll" in types:
            self.audit(None, emp["id"], "blocked_sensitive", {"type": "payroll"})
            hr = self._hr_contact(emp)
            self._reply(emp, channel, f"I can't handle payroll or salary questions here. Please contact {hr['name'] if hr else 'HR'} privately.")
            self.s.q("UPDATE inbound_events SET outcome='blocked_sensitive' WHERE channel=? AND source_event_id=?", (channel, eid))
            return {"outcome": "blocked_sensitive", "request_ids": []}
        if cls["needs_clarification"]:
            rid = self._create(emp, channel, eid, clean, "unclear", "Needs more detail", {}, cls, "NEEDS_INFO")
            msg = cls["clarifying_question"]
            if cls["suspected_injection"]:
                msg = "I can't change rules or skip approvals. " + msg
            self._reply(emp, channel, msg, rid)
            self.s.q("UPDATE inbound_events SET outcome='needs_info' WHERE channel=? AND source_event_id=?", (channel, eid))
            return {"outcome": "needs_info", "request_ids": [rid]}
        ids = []
        notes = []
        for it in cls["intents"]:
            code = it["type_code"]
            tdef = self.d.type(code)
            if tdef and not tdef.get("enabled", True):
                self.audit(None, emp["id"], "disabled_type_refused", {"type": code})
                admin = self.d.role_holder("admin")
                notes.append(f"{tdef['name']} requests are not handled through Deskhand here. Please contact {admin['name'] if admin else 'an admin'} directly.")
                continue
            if code == "leave" and self.d.other_names_in(clean, emp):
                self.audit(None, emp["id"], "leave_for_other_refused", {})
                notes.append("I can only apply leave for you, not for someone else. Ask them to message me, or ask their manager.")
                continue
            if code == "leave" and not it["details"].get("dates"):
                rid = self._create(emp, channel, eid, clean, code, it["summary"], it["details"], cls, "NEEDS_INFO")
                self._reply(emp, channel, "Which dates do you need off?", rid)
                ids.append(rid)
                continue
            rid = self._create(emp, channel, eid, clean, code, it["summary"], it["details"], cls, "SUBMITTED")
            ids.append(rid)
            self._route(rid)
        for n in notes:
            self._reply(emp, channel, n)
        if cls["suspected_injection"] and ids:
            self._reply(emp, channel, "Note: approvals can't be skipped and I never handle passwords. Your request follows the normal process.", ids[0])
        outcome = "created" if ids else "refused"
        self.s.q("UPDATE inbound_events SET outcome=? WHERE channel=? AND source_event_id=?", (outcome, channel, eid))
        return {"outcome": outcome, "request_ids": ids}

    def _reply(self, emp, channel, body, rid=None):
        self._notify(rid, emp["id"], body, "reply", n=self.s.one("SELECT COUNT(*) c FROM outbox")["c"], subject="Deskhand", channel=channel)

    def _classify(self, text):
        calls, budget = int(self.s.get_setting("ai_calls_today")), int(self.s.get_setting("ai_budget_per_day"))
        use_ai = bool(self.api_key) or bool(self.ai_classify and (self.ai_enabled() if self.ai_enabled else True))
        if use_ai and classifier.PAYROLL.search(text):  # salary and payroll text is never sent to any AI: the rules classify it and the HR redirect follows
            self.audit(None, "system", "ai_skipped_sensitive", {})
            use_ai = False
        if use_ai and calls >= budget:
            if self.s.get_setting("ai_budget_exhausted_logged") != "1":
                self.audit(None, "system", "ai_budget_exhausted", {"budget": budget})
                self.s.set_setting("ai_budget_exhausted_logged", "1")
            use_ai = False
        if use_ai:
            try:
                fn = self.ai_classify or (lambda t, today: classifier.classify_anthropic(t, self.api_key, self.model, today))
                res = fn(text, self.today())
                self.s.set_setting("ai_calls_today", calls + 1)
                if calls + 1 >= int(budget * 0.8) and self.s.get_setting("ai_budget_warned") != "1":
                    self.audit(None, "system", "ai_budget_warning", {"used": calls + 1, "budget": budget})
                    self.s.set_setting("ai_budget_warned", "1")
                res = classifier.validate_ai_result(res) if not isinstance(res, dict) or "provider" not in res else res
                rules = classifier.classify_rules(text, self.today())
                res["suspected_injection"] = res["suspected_injection"] or rules["suspected_injection"]
                res["injection_flags"] = rules["injection_flags"]
                for it in res["intents"]:
                    if it["type_code"] == "leave" and not it["details"].get("dates"):
                        it["details"] = classifier.parse_leave(text, self.today())
                return res
            except Exception as ex:  # any AI failure falls back to the rules, never an error for the user
                self.audit(None, "system", "ai_failed_fallback", {"error": str(ex)[:120]})
        return classifier.classify_rules(text, self.today())

    def _create(self, emp, channel, eid, text, code, summary, details, cls, status):
        now = self.now()
        cur = self.s.q("INSERT INTO requests(type_code,employee_id,channel,source_event_id,raw_text,summary,details_json,confidence,provider,status,created_at,flags)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (code, emp["id"], channel, eid, text, summary, dumps(details), cls["confidence"], cls["provider"], status, now,
                        "injection" if cls["suspected_injection"] else ""))
        rid = cur.lastrowid
        self.audit(rid, emp["id"], "request_created", {"type": code, "channel": channel, "confidence": cls["confidence"], "provider": cls["provider"]})
        return rid

    # ---------- routing ----------
    def _rewrite(self, r, emp, t):
        d = r["details"]
        have = "; ".join(f"{a}: {b}" for a, b in r["details_view"])
        aliases = {"justification": ["justification", "project", "reason"], "dates": ["dates"], "item": ["item", "tool", "resource"], "issue": ["issue"]}
        missing = [k for k in t.get("details_needed", []) if not any(d.get(a) not in (None, "", []) for a in aliases.get(k, [k]))]
        lines = [f"New request #{r['id']} from {emp['name']} ({emp['department']})", f"Request: {r['summary']}", f"Type: {t['name']}"]
        if have:
            lines.append(f"Details: {have}")
        if missing:
            lines.append(f"Not provided: {', '.join(missing)}")
        if r["email_subject"]:
            lines.append(f'Email subject: "{r["email_subject"]}"')
        lines.append(f'Original message: "{r["message"]}"')
        lines.append("Options: Approve, Reject (a reason is required), or Mark pending.")
        return "\n".join(lines)

    def _route(self, rid):
        r = self.req(rid)
        emp = self.d.emp(r["employee_id"])
        t = self.d.type(r["type_code"])
        if r["type_code"] == "policy_question":
            self._set_status(rid, "APPROVED", "system", "no approval needed")
            self._set_status(rid, "FULFILLING", "system")
            self._set_status(rid, "FULFILLED", "system")
            self._notify(rid, emp["id"], self._answer_policy(emp, r["raw_text"]), "answer")
            self._set_status(rid, "CLOSED", "system")
            return
        if r["type_code"] == "leave" and getattr(self.hr, "records_balances", False):  # a company: the balance comes from its own ledger
            chk = self.hr.check(emp["id"], r["details"])
            r["details"]["balance"], r["details"]["leave_check"] = self.hr.list_leave_balances(emp["id"]), chk
            self.s.q("UPDATE requests SET details_json=? WHERE id=?", (dumps(r["details"]), rid))
            if chk["days"] == 0:  # only weekend days: there is nothing to take off
                self._set_status(rid, "NEEDS_INFO", "system", "no working days in the dates")
                self._reply(emp, r["channel"], "Those dates fall on a weekend, so there is no working day to take off. Which working days do you need off?", rid)
                return
        elif r["type_code"] == "leave" and self.d.balances:
            bal = self.hr.list_leave_balances(emp["id"])
            r["details"]["balance"] = bal
            self.s.q("UPDATE requests SET details_json=? WHERE id=?", (dumps(r["details"]), rid))
        chain = self.d.approver_chain(t, emp, r["details"])
        if not chain:
            self._set_status(rid, "APPROVED", "system", "policy allows without approval")
            self._start_fulfilment(rid)
            return
        now = self.now()
        self.s.q("UPDATE requests SET chain_json=?, chain_index=0, assigned_at=? WHERE id=?", (dumps(chain), now, rid))
        self._set_status(rid, "PENDING_APPROVAL", "system", f"approvers: {', '.join(self.name(c) for c in chain)}")
        r = self.req(rid)
        self._notify(rid, chain[0], self._rewrite(r, emp, t), "approval_request", subject=f"Approval needed: {t['name']} #{rid}")
        extra = f" It needs {len(chain)} approvals, in order." if len(chain) > 1 else ""
        self._notify(rid, emp["id"], f"I've sent your request #{rid} to {self.name(chain[0])} for approval.{extra} I'll update you here.\nWhat I understood: {r['summary']}.", "submitted")

    def _hr_contact(self, emp):
        """Who to send payroll and policy questions to: the payroll type's own setting, else the HR lead."""
        pay = self.d.type("payroll") or {}
        return self.d.resolve_rule(pay.get("redirect"), emp) or self.d.role_holder("hr_lead")

    def _answer_policy(self, emp, text):
        t = text.lower()
        hr = self._hr_contact(emp)
        if re.search(r"how many|balance|left", t):
            if not (self.d.balances or getattr(self.hr, "records_balances", False)):  # no records and no prepared figures: say so rather than invent them
                return f"I can't see leave balances yet. Please ask {hr['name'] if hr else 'HR'}."
            b = self.hr.list_leave_balances(emp["id"])
            return "Your leave balance: " + ", ".join(f"{k} {v}" for k, v in b.items()) + "."
        for k, v in self.d.policies.items():
            if k.replace("_", " ") in t or k.split("_")[0] in t:
                return f"{k.replace('_', ' ').title()}: {v}"
        return f"I don't have that policy to hand. Please ask {hr['name'] if hr else 'HR'}."

    # ---------- decisions ----------
    def approve(self, rid, actor_id):
        r = self.req(rid)
        if r["status"] != "PENDING_APPROVAL":
            self.audit(rid, actor_id, "duplicate_action_ignored", {"action": "approve", "status": r["status"]})
            raise InvalidTransition(f"request is {r['status']}, not pending approval")
        if not self.can_act(r, actor_id):
            self.audit(rid, actor_id, "unauthorized_action_refused", {"action": "approve"})
            raise NotAuthorized("only the assigned approver, an escalation contact or an admin can approve")
        self.audit(rid, actor_id, "approved_stage", {"stage": r["chain_index"] + 1, "of": len(r["chain"])})
        if r["chain_index"] + 1 < len(r["chain"]):
            nxt = r["chain"][r["chain_index"] + 1]
            self.s.q("UPDATE requests SET chain_index=chain_index+1, assigned_at=?, last_reminder_at=NULL, escalated_to=NULL, escalation_level=0 WHERE id=?",
                     (self.now(), rid))
            r2 = self.req(rid)
            emp = self.d.emp(r["employee_id"])
            self._notify(rid, nxt, self._rewrite(r2, emp, self.d.type(r["type_code"])), "approval_request", n=r["chain_index"] + 1,
                         subject=f"Approval needed: {self.d.type(r['type_code'])['name']} #{rid}")
            self._notify(rid, r["employee_id"], f"{self.name(actor_id)} approved your request #{rid}. It now needs {self.name(nxt)}.", "stage_approved", n=r["chain_index"], by=actor_id)
            return self.req(rid)
        self.s.q("UPDATE requests SET decided_at=? WHERE id=?", (self.now(), rid))
        self._set_status(rid, "APPROVED", actor_id, "all approvals given")
        if self.d.type(r["type_code"])["fulfilment_kind"] == "auto_action":
            self._notify(rid, r["employee_id"], f"Your request #{rid} was approved by {self.name(actor_id)}.", "approved", by=actor_id)
        self._start_fulfilment(rid, approved_by=actor_id)
        return self.req(rid)

    def reject(self, rid, actor_id, reason):
        r = self.req(rid)
        if not (reason or "").strip():
            raise Refused("a reason is required to reject")
        if r["status"] != "PENDING_APPROVAL":
            raise InvalidTransition(f"request is {r['status']}, not pending approval")
        if not self.can_act(r, actor_id):
            self.audit(rid, actor_id, "unauthorized_action_refused", {"action": "reject"})
            raise NotAuthorized("only the assigned approver, an escalation contact or an admin can reject")
        clean, _ = classifier.redact(reason.strip())
        self.s.q("UPDATE requests SET rejection_reason=?, decided_at=? WHERE id=?", (clean, self.now(), rid))
        self._set_status(rid, "REJECTED", actor_id, clean)
        self._notify(rid, r["employee_id"], f"Your request #{rid} was rejected by {self.name(actor_id)}. Reason: {clean}", "rejected", by=actor_id)
        return self.req(rid)

    def mark_pending(self, rid, actor_id, note=""):
        r = self.req(rid)
        if r["status"] != "PENDING_APPROVAL":
            raise InvalidTransition(f"request is {r['status']}, not pending approval")
        if not self.can_act(r, actor_id):
            raise NotAuthorized("not allowed")
        self.audit(rid, actor_id, "marked_pending", {"note": note})
        n = self.s.one("SELECT COUNT(*) c FROM events WHERE request_id=? AND type='marked_pending'", (rid,))["c"]
        self._notify(rid, r["employee_id"], f"Your request #{rid} is still pending with {self.name(actor_id)}." + (f" Note: {note}" if note else ""), "pending_update", n=n)
        return self.req(rid)

    def cancel(self, rid, actor_id):
        r = self.req(rid)
        if actor_id != r["employee_id"] and not self._is_admin(actor_id):
            raise NotAuthorized("only the requester or an admin can cancel")
        self._set_status(rid, "CANCELLED", actor_id, "cancelled")
        return self.req(rid)

    def done(self, rid, actor_id):
        r = self.req(rid)
        if r["status"] != "FULFILLING":
            raise InvalidTransition(f"request is {r['status']}, not being fulfilled")
        if actor_id != r["fulfiller_id"] and not self._is_admin(actor_id):
            self.audit(rid, actor_id, "unauthorized_action_refused", {"action": "done"})
            raise NotAuthorized("only the assigned fulfiller or an admin can mark this done")
        self._set_status(rid, "FULFILLED", actor_id, "marked done")
        self._notify(rid, r["employee_id"], f"Your request #{rid} is ready: {self.name(actor_id)} has completed it.", "fulfilled", by=actor_id)
        self._set_status(rid, "CLOSED", "system")
        return self.req(rid)

    # ---------- fulfilment and the guard ----------
    def _start_fulfilment(self, rid, approved_by=None):
        r = self.req(rid)
        t = self.d.type(r["type_code"])
        self._set_status(rid, "FULFILLING", "system")
        if t["fulfilment_kind"] == "auto_action":
            self._run_leave(rid)
            return
        ful = self.d.resolve_rule(t.get("fulfiller_rule"), self.d.emp(r["employee_id"])) or self.d.role_holder("admin")
        self.s.q("UPDATE requests SET fulfiller_id=? WHERE id=?", (ful["id"], rid))
        emp = self.d.emp(r["employee_id"])
        note = " Do not send credentials in chat or email; grant access in the tool, then click Done." if r["type_code"] in ("software_seat", "access") else ""
        self._notify(rid, ful["id"], f"Task for request #{rid}: {t['name']} for {emp['name']}.{note}\nClick Done when finished.", "task", subject=f"Task: {t['name']} #{rid}")
        lead = f"Your request #{rid} was approved by {self.name(approved_by)}. " if approved_by else ""
        self._notify(rid, emp["id"], f"{lead}{ful['name']} will take care of it. I'll tell you when it's done.", "approved" if lead else "assigned", by=approved_by)

    def _run_leave(self, rid):
        r = self.req(rid)
        d = r["details"]
        ref = f"deskhand-{rid}"

        def fn():
            existing = self.hr.find_leave_by_ref(ref)  # read before write: a lost response must not apply the leave twice
            if existing:
                return existing
            return self.hr.create_leave_request(r["employee_id"], d.get("dates"), d.get("leave_type"), d.get("reason"), ref, d.get("half_day", False), date_range=d.get("date_range", False))

        res = self.run_action(rid, "apply_leave", fn)
        if res is not None:
            self._set_status(rid, "FULFILLED", "system", f"leave {self.hr.applied}")
            left = ""
            if getattr(self.hr, "records_balances", False):
                left = f" {d.get('leave_type', 'casual').capitalize()} leave left: {self.hr.list_leave_balances(r['employee_id']).get(d.get('leave_type', 'casual'), 0)}."
            shown = (" to " if d.get("date_range") and len(d.get("dates", [])) == 2 else ", ").join(_day(x) for x in d.get("dates", []))
            self._notify(rid, r["employee_id"], f"Done: your leave ({shown}) has been {self.hr.applied}.{left}", "fulfilled")
            self._set_status(rid, "CLOSED", "system")

    def run_action(self, rid, step, fn, max_attempts=3):
        key = f"{rid}:{step}"
        row = self.s.one("SELECT * FROM action_runs WHERE idempotency_key=?", (key,))
        if row and row["status"] == "succeeded":
            self.audit(rid, "system", "action_idempotent_hit", {"step": step})
            return json.loads(row["result_json"])
        if not row:
            self.s.q("INSERT INTO action_runs(request_id,step,idempotency_key,attempts,status,updated_at) VALUES(?,?,?,?,?,?)",
                     (rid, step, key, 0, "pending", self.now()))
        attempts = 0
        while attempts < max_attempts:
            attempts += 1
            self.s.q("UPDATE action_runs SET attempts=attempts+1, updated_at=? WHERE idempotency_key=?", (self.now(), key))
            try:
                res = fn()
                self.s.q("UPDATE action_runs SET status='succeeded', result_json=?, last_error=NULL WHERE idempotency_key=?", (dumps(res), key))
                self.audit(rid, "system", "action_succeeded", {"step": step, "attempt": attempts})
                return res
            except ActionError as ex:
                self.s.q("UPDATE action_runs SET last_error=? WHERE idempotency_key=?", (str(ex), key))
                if ex.retryable and attempts < max_attempts:
                    self.audit(rid, "system", "action_retry", {"step": step, "attempt": attempts, "error": str(ex), "next_backoff_s": BACKOFF[min(attempts - 1, 2)]})
                    continue
                self.s.q("UPDATE action_runs SET status='failed' WHERE idempotency_key=?", (key,))
                self.audit(rid, "system", "action_failed", {"step": step, "attempt": attempts, "error": str(ex), "retryable": ex.retryable})
                self._set_status(rid, "PARKED", "system", f"{step} failed: {ex}")
                admin = self.d.role_holder("admin")
                self._notify(rid, admin["id"] if admin else None, f"Request #{rid} is parked: {step} failed after {attempts} attempt(s): {ex}. Use Replay once the system is back.",
                             "parked", subject=f"Needs attention: request #{rid}")
                self._notify(rid, self.req(rid)["employee_id"], f"Your request #{rid} hit a delay in {self.hr.label}. It's with the admin and nothing is lost.", "delay")
                return None
        return None

    def replay(self, rid, actor_id):
        r = self.req(rid)
        if r["status"] != "PARKED":
            raise InvalidTransition(f"request is {r['status']}, not parked")
        if not self._is_admin(actor_id):
            raise NotAuthorized("only an admin can replay")
        self.s.q("UPDATE action_runs SET status='pending', attempts=0 WHERE request_id=?", (rid,))
        self._set_status(rid, "FULFILLING", actor_id, "replay")
        self.audit(rid, actor_id, "replayed", {})
        self._run_leave(rid)
        return self.req(rid)

    def reset_breaker(self, actor_id, channel=None):
        if not self._is_admin(actor_id):
            raise NotAuthorized("only an admin can reset the breaker")
        self.s.set_setting("breaker_open", "")
        self.s.set_setting("breaker_reset_at", self.now())
        self.audit(None, actor_id, "breaker_reset", {})

    # ---------- scheduler ----------
    def sweep(self):
        out = {"reminders": 0, "escalations": 0}
        now = self.now()
        for r in self.s.all("SELECT * FROM requests WHERE status='PENDING_APPROVAL'"):
            r = self.req(r["id"])
            t = self.d.type(r["type_code"])
            age_h = (now - (r["assigned_at"] or r["created_at"])) / 3600
            cur = self.current_approver(r)
            if r["last_reminder_at"] is None and age_h >= t["reminder_hours"]:
                self.s.q("UPDATE requests SET last_reminder_at=? WHERE id=?", (now, r["id"]))
                self._notify(r["id"], r["escalated_to"] or cur, f"Reminder: request #{r['id']} ({t['name']}) has been waiting {int(age_h)}h for your decision.", "reminder")
                self.audit(r["id"], "system", "reminder_sent", {"to": r["escalated_to"] or cur})
                out["reminders"] += 1
            if r["escalation_level"] == 0 and age_h >= t["escalate_hours"]:
                backup = self.d.resolve_rule(t.get("backup_rule"), self.d.emp(r["employee_id"]))
                if not backup or backup["id"] == cur:
                    backup = self.d.role_holder("admin")
                self.s.q("UPDATE requests SET escalation_level=1, escalated_to=?, escalated_at=? WHERE id=?", (backup["id"], now, r["id"]))
                self._notify(r["id"], backup["id"], f"Escalation: request #{r['id']} ({t['name']}) has waited {int(age_h)}h with {self.name(cur)}. You can decide it now.", "escalation_1")
                self._notify(r["id"], r["employee_id"], f"Your request #{r['id']} is still pending with {self.name(cur)}; I've escalated it to {backup['name']}.", "escalated")
                self.audit(r["id"], "system", "escalated", {"to": backup["id"], "level": 1})
                out["escalations"] += 1
            elif r["escalation_level"] == 1 and (now - (r["escalated_at"] or now)) / 3600 >= t["escalate_hours"]:
                admin = self.d.role_holder("admin")
                if admin and admin["id"] != r["escalated_to"]:
                    self.s.q("UPDATE requests SET escalation_level=2, escalated_to=?, escalated_at=? WHERE id=?", (admin["id"], now, r["id"]))
                    self._notify(r["id"], admin["id"], f"Second escalation: request #{r['id']} ({t['name']}) is still undecided. Please decide it.", "escalation_2")
                    self.audit(r["id"], "system", "escalated", {"to": admin["id"], "level": 2})
                    out["escalations"] += 1
        return out

    # ---------- views ----------
    def status_text(self, r):
        if r["status"] == "PENDING_APPROVAL":
            who = self.name(r["escalated_to"] or self.current_approver(r))
            ts = r["escalated_at"] if r["escalated_to"] and r["escalated_at"] else (r["assigned_at"] or r["created_at"])
            since = datetime.utcfromtimestamp(ts).strftime("%d %b %H:%M")
            return f"Pending with {who} since {since} UTC"
        if r["status"] == "CLOSED":
            return "Completed"
        return r["status"].replace("_", " ").title()

    def list_requests(self, status=None, employee=None):
        sql, args = "SELECT id FROM requests WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if employee:
            sql += " AND employee_id=?"
            args.append(employee)
        rows = [self.req(x["id"]) for x in self.s.all(sql + " ORDER BY id DESC", args)]
        for r in rows:
            r["status_text"] = self.status_text(r)
            r["employee_name"] = self.name(r["employee_id"])
            r["approver_name"] = self.name(self.current_approver(r)) if self.current_approver(r) else None
        return rows

    def inbox(self, person_id):
        items = []
        for r in self.list_requests():
            if r["status"] == "PENDING_APPROVAL" and self.can_act(r, person_id):
                items.append({**r, "inbox_kind": "approval"})
            elif r["status"] == "FULFILLING" and (r["fulfiller_id"] == person_id or self._is_admin(person_id)) and r["type_code"] != "leave":
                items.append({**r, "inbox_kind": "task"})
            elif r["status"] == "PARKED" and self._is_admin(person_id):
                items.append({**r, "inbox_kind": "parked"})
        return items

    def timeline(self, rid):
        return [dict(e, data=json.loads(e["data_json"])) for e in self.s.all("SELECT * FROM events WHERE request_id=? ORDER BY id", (rid,))]

    def dashboard(self):
        by_status = {r["status"]: r["c"] for r in self.s.all("SELECT status, COUNT(*) c FROM requests GROUP BY status")}
        dec = self.s.one("SELECT AVG(decided_at-created_at) a FROM requests WHERE decided_at IS NOT NULL")["a"]
        guard = {r["type"]: r["c"] for r in self.s.all("SELECT type, COUNT(*) c FROM events GROUP BY type")}
        keys = ["duplicate_suppressed", "own_message_ignored", "breaker_opened", "action_retry", "action_idempotent_hit", "action_failed",
                "injection_suspected", "unknown_sender_refused", "blocked_sensitive", "secret_redacted", "leave_for_other_refused",
                "unauthorized_action_refused", "disabled_type_refused", "escalated", "reminder_sent", "ai_budget_exhausted", "delivery_retry", "delivery_failed"]
        prov = {r["provider"]: r["c"] for r in self.s.all("SELECT provider, COUNT(*) c FROM requests GROUP BY provider")}
        return {"by_status": by_status, "total": sum(by_status.values()), "avg_decision_minutes": round(dec / 60, 1) if dec else None,
                "guard": {k: guard.get(k, 0) for k in keys}, "classified_by": prov,
                "delivery": self.live.summary() if self.live else {"used_today": 0, "cap": 150, "capped": False, "counts": {}, "ready": {"slack": False, "email": False}, "problems": []},
                "ai_calls_today": int(self.s.get_setting("ai_calls_today")), "ai_budget": int(self.s.get_setting("ai_budget_per_day")),
                "breaker_open": [c for c in self.s.get_setting("breaker_open", "").split(",") if c], "hr_effects": len(self.hr.leaves)}

    def digest(self, person_id):
        me = self.d.emp(person_id)
        mine = self.list_requests(employee=person_id)
        decided = self.s.all("SELECT request_id, type FROM events WHERE actor=? AND type IN ('approved_stage')", (person_id,))
        rej = self.s.all("SELECT id FROM requests WHERE rejection_reason IS NOT NULL AND id IN (SELECT request_id FROM events WHERE actor=? AND type='status_change' AND data_json LIKE '%REJECTED%')", (person_id,))
        pending = [r for r in self.list_requests("PENDING_APPROVAL") if self.can_act(r, person_id)]
        oldest = max(((self.now() - (r["assigned_at"] or r["created_at"])) / 3600 for r in pending), default=0)
        return {"person": me["name"], "as_approver": {"approved": len(decided), "rejected": len(rej), "pending": len(pending), "oldest_pending_hours": round(oldest, 1)},
                "as_employee": [{"id": r["id"], "type": r["type_code"], "status": r["status_text"]} for r in mine if r["status"] not in FINAL]}


import re  # noqa: E402  (used in _answer_policy)
