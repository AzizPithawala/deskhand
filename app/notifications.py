"""Notifications: what counts as one, which category it belongs to, and how a person chooses to be reached. Everything a person is told is a row in the company's outbox; this module is the
view over it. A person's preferences can reduce how they are reached (Slack, email) but never hide what needs their decision: the in-app copy always exists and "needs your decision"
keeps at least one of Slack or email. Only the person themself reads or changes their own list."""
import time

# (code, title, the message kinds in it). Every kind the engine writes is here or in EXCLUDED; tests/test_notifications.py reads the source to prove it.
CATEGORIES = [
    ("decide", "Needs your decision", ["approval_request", "approval_fallback", "reject_link"]),
    ("nudge", "Reminders and escalations", ["reminder", "escalation_1", "escalation_2", "escalated"]),
    ("mine", "About your requests", ["stage_approved", "approved", "rejected", "fulfilled", "pending_update", "assigned", "delay"]),
    ("task", "Tasks for you", ["task"]),
    ("system", "System and admin", ["parked", "breaker", "delivery_failed"]),
]
EXCLUDED = {"reply", "answer", "submitted", "decision_ack"}  # the Ask conversation and confirmations of your own click: not notifications
TITLES = {c: t for c, t, _ in CATEGORIES}
KIND_CATEGORY = {k: c for c, _, kinds in CATEGORIES for k in kinds}
NOTIFICATION_KINDS = sorted(KIND_CATEGORY)
PROTECTED = "decide"  # at least one of Slack or email always stays on for this category


class PrefError(ValueError):
    pass


def category_of(kind):
    return KIND_CATEGORY.get(kind)


def get_prefs(store, person_id):
    """{category: {"slack": bool, "email": bool}} with everything on unless the person turned something off."""
    out = {c: {"slack": True, "email": True} for c, _, _ in CATEGORIES}
    for r in store.all("SELECT category, slack, email FROM notification_prefs WHERE person_id=?", (person_id,)):
        if r["category"] in out:
            out[r["category"]] = {"slack": bool(r["slack"]), "email": bool(r["email"])}
    return out


def set_prefs(store, person_id, prefs):
    """Save a person's choices (a partial dict is fine). Returns the full result. Refuses anything unknown and any choice that would hide a decision."""
    if not isinstance(prefs, dict):
        raise PrefError("The settings were not understood.")
    now = get_prefs(store, person_id)
    for cat, v in prefs.items():
        if cat not in now:
            raise PrefError(f"There is no category called {cat}.")
        if not isinstance(v, dict) or set(v) - {"slack", "email"} or not all(isinstance(x, bool) for x in v.values()):
            raise PrefError(f"{TITLES[cat]}: choose Slack and email as on or off.")
        now[cat] = {**now[cat], **v}
    if not (now[PROTECTED]["slack"] or now[PROTECTED]["email"]):
        raise PrefError(f"{TITLES[PROTECTED]} keeps at least one of Slack or email on, so an approval cannot go unseen.")
    for cat, v in now.items():
        store.q("INSERT INTO notification_prefs(person_id,category,slack,email,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(person_id,category) DO UPDATE SET slack=excluded.slack, email=excluded.email, updated_at=excluded.updated_at",
                (person_id, cat, int(v["slack"]), int(v["email"]), time.time()))
    return now


def allowed(store, person_id, kind, channel):
    """May this kind reach this person by this channel ('chat' is Slack)? Kinds that are not notifications are not governed by preferences."""
    cat = KIND_CATEGORY.get(kind)
    if not cat or channel not in ("chat", "email"):
        return True
    return get_prefs(store, person_id)[cat]["slack" if channel == "chat" else "email"]
