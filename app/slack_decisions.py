"""Slack Approve / Reject buttons. An approval DM can carry buttons; a click reaches Deskhand as a callback from a viaSocket automation. The callback is public, so it
authorises nothing by itself: it needs a ONE-TIME token that Deskhand issued for that very message (only its hash is stored, it works once, it expires), and the Slack user
id Slack reported for the click must belong to a person in the company directory who can act on the request right now. Every outcome is answered with HTTP 200 and a status word,
and a repeat does nothing (viaSocket redelivers). A Slack Reject only points the person to the web page, because a reason is mandatory. The web page always works without any of this."""
import hashlib
import re
import secrets
import time


TTL_S = 7 * 86400
SLACK_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")


def _h(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def org_of(token):
    """Tokens look like <company id>.<random>; the company id says whose database holds the record (it is not a secret)."""
    org, dot, rest = str(token or "").partition(".")
    return org if dot and rest and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", org) else None


def issue(engine, org_id, kind, request_id, person_id, outbox_id=None):
    """A new one-time token for one message. An earlier unused token for the same outbox row (a retry) stops working."""
    now = time.time()
    if outbox_id is not None:
        engine.s.q("UPDATE slack_decisions SET used_at=?, outcome='superseded' WHERE outbox_id=? AND used_at IS NULL", (now, outbox_id))
    raw = f"{org_id}.{secrets.token_urlsafe(32)}"
    engine.s.q("INSERT INTO slack_decisions(token_hash,kind,request_id,person_id,outbox_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
               (_h(raw), kind, request_id, person_id, outbox_id, now, now + TTL_S))
    return raw


def person_by_slack(engine, slack_user):
    """The active person whose Slack id this is. ONLY the Slack id counts: a directory id or email in the body must never be taken for a click."""
    sid = str(slack_user or "").strip()
    if not SLACK_ID.match(sid):
        return None
    return next((e for e in engine.d.employees.values() if e["active"] and (e.get("slack_id") or "").lower() == sid.lower()), None)


def _finish(engine, token_hash, outcome, who=None):
    engine.s.q("UPDATE slack_decisions SET outcome=?, clicked_by=COALESCE(?, clicked_by) WHERE token_hash=?", (outcome, who, token_hash))


def _claim(engine, token_hash, who=None):
    """Use the token up, once, whoever else is trying at the same moment."""
    return engine.s.q("UPDATE slack_decisions SET used_at=?, clicked_by=? WHERE token_hash=? AND used_at IS NULL", (time.time(), who, token_hash)).rowcount == 1


def latest_test(engine, person_id):
    """What became of the latest test approval an admin sent to themselves."""
    row = engine.s.one("SELECT used_at, outcome, created_at FROM slack_decisions WHERE kind='test' AND person_id=? ORDER BY created_at DESC LIMIT 1", (person_id,))
    if not row:
        return {"state": "none"}
    if row["used_at"] is None:
        return {"state": "waiting", "since": row["created_at"]}
    return {"state": "received" if row["outcome"] == "test_ok" else row["outcome"], "at": row["used_at"]}


def decide(engine, token, slack_user, decision):
    """Handle one click. Returns {"status": word}; it never raises and never reveals who is allowed to do what."""
    from .engine import InvalidTransition, NotAuthorized  # here, not at the top: the engine imports delivery, which imports this module
    row = engine.s.one("SELECT * FROM slack_decisions WHERE token_hash=?", (_h(token),))
    if not row:
        return {"status": "invalid"}
    th, now = row["token_hash"], time.time()
    if row["used_at"] is not None:
        return {"status": "already_used"}
    if now > row["expires_at"]:
        return {"status": "expired"}
    decision = (decision or "").strip().lower()
    if decision == "error":  # the automation could not show the buttons: the person still gets a plain message with the link
        if not _claim(engine, th):
            return {"status": "already_used"}
        _finish(engine, th, "send_failed")
        req = engine.req(row["request_id"]) if row["request_id"] else None
        if req and req["status"] == "PENDING_APPROVAL":
            engine._notify(row["request_id"], row["person_id"], f"Request #{row['request_id']} is waiting for your decision: {req['summary']}.", "approval_fallback", n=th[:6])
        engine.audit(row["request_id"], "system", "slack_buttons_failed", {})
        return {"status": "fallback_sent"}
    if decision not in ("approve", "reject"):
        return {"status": "bad_decision"}
    person = person_by_slack(engine, slack_user)
    if not person:
        engine.audit(row["request_id"], "system", "slack_click_refused", {"why": "unknown_person"})
        return {"status": "unknown_person"}
    if row["kind"] == "test":
        if person["id"] != row["person_id"]:
            engine.audit(None, "system", "slack_click_refused", {"why": "not_the_tester"})
            return {"status": "not_allowed"}
        if not _claim(engine, th, person["id"]):
            return {"status": "already_used"}
        _finish(engine, th, "test_ok", person["id"])
        engine.audit(None, person["id"], "slack_test_click", {"decision": decision})
        return {"status": "test_ok", "decision": decision}
    r = engine.req(row["request_id"])
    if not r:
        return {"status": "invalid"}
    if r["status"] != "PENDING_APPROVAL":
        if _claim(engine, th, person["id"]):
            _finish(engine, th, "already_decided", person["id"])
        return {"status": "already_decided"}
    if not engine.can_act(r, person["id"]):
        engine.audit(row["request_id"], person["id"], "slack_click_refused", {"why": "not_allowed"})  # the token stays usable: the right person may still click
        return {"status": "not_allowed"}
    if not _claim(engine, th, person["id"]):
        return {"status": "already_used"}
    rid = row["request_id"]
    if decision == "reject":
        _finish(engine, th, "needs_reason", person["id"])
        engine._notify(rid, person["id"], f"To reject request #{rid}, please give the reason in Deskhand, because the person who asked is told why.", "reject_link", n=th[:6])
        return {"status": "needs_reason"}
    try:
        engine.approve(rid, person["id"], via="slack")
    except (InvalidTransition, NotAuthorized):  # decided by someone else a moment ago
        _finish(engine, th, "already_decided", person["id"])
        return {"status": "already_decided"}
    _finish(engine, th, "approved", person["id"])
    engine._notify(rid, person["id"], f"You approved request #{rid} from Slack. The person who asked is being told.", "decision_ack", n=th[:6])
    return {"status": "approved"}
