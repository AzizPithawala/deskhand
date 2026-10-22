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


# ---------------- the person's own list ----------------
DESCRIPTIONS = {
    "decide": "Requests waiting for you to approve or reject.",
    "nudge": "Reminders when something waits too long, and escalations of your own requests.",
    "mine": "Approvals, rejections, progress and completion of requests you made.",
    "task": "Work assigned to you after a request was approved.",
    "system": "Delivery problems and requests that need an admin.",
}
CHANNELS = ("slack", "email")
MAX_LIMIT = 100
_IN = ",".join("?" * len(NOTIFICATION_KINDS))
# A person's notification is the row addressed to them; its email copy (key ending ":email") is shown as a chip on it, not as a second entry. The key, not the channel, tells them apart,
# so a request that came in by email (whose rows are all channel "email") still shows up.


class FilterError(ValueError):
    pass


def _like(text):
    """A search term as a LIKE pattern: the user's % and _ are just characters."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _filters(f):
    """Validate the filters and turn them into SQL pieces. Everything user-supplied is a bound parameter."""
    where, args = [], []
    if f.get("unread"):
        where.append("o.read_at IS NULL")
    if f.get("category"):
        if f["category"] not in TITLES:
            raise FilterError("Unknown category.")
        kinds = next(k for c, _, k in CATEGORIES if c == f["category"])
        where.append(f"o.kind IN ({','.join('?' * len(kinds))})")
        args += kinds
    if f.get("type"):
        where.append("r.type_code = ?")
        args.append(str(f["type"])[:40])
    if f.get("status"):
        where.append("r.status = ?")
        args.append(str(f["status"])[:40])
    if f.get("channel"):
        if f["channel"] not in CHANNELS:
            raise FilterError("Channel must be slack or email.")
        where.append("(o.channel = 'chat' AND o.delivery_status = 'delivered')" if f["channel"] == "slack" else
                     "EXISTS (SELECT 1 FROM outbox m WHERE m.idempotency_key = o.idempotency_key || ':email' AND m.delivery_status = 'delivered')")
    if f.get("since") is not None:
        where.append("o.created_at >= ?")
        args.append(float(f["since"]))
    if f.get("until") is not None:
        where.append("o.created_at < ?")
        args.append(float(f["until"]))
    if f.get("q"):
        q = str(f["q"]).strip()[:100]
        if q:
            where.append("(o.subject LIKE ? ESCAPE '\\' OR o.body LIKE ? ESCAPE '\\')")
            args += [_like(q), _like(q)]
    if f.get("before") is not None:
        where.append("o.id < ?")
        args.append(int(f["before"]))
    return where, args


def copy_state(row):
    """How one copy of a message fared, in words a person understands. A copy that was delivered to the rehearsing admin instead of the person says so."""
    st = row["delivery_status"]
    if st == "delivered" and row.get("sent_to") and row["sent_to"] != row["to_person"]:
        return "rehearsal"
    return st


def entries(engine, person_id, f=None):
    """The person's notifications, newest first, with the filters applied, plus how many are unread in all."""
    f = f or {}
    where, args = _filters(f)
    asked = f.get("limit")
    limit = min(max(30 if asked is None else int(asked), 1), MAX_LIMIT)
    sql = (f"SELECT o.*, r.type_code AS r_type, r.status AS r_status, r.summary AS r_summary, "
           "(SELECT m.delivery_status FROM outbox m WHERE m.idempotency_key = o.idempotency_key || ':email') AS email_status, "
           "(SELECT m.delivery_note FROM outbox m WHERE m.idempotency_key = o.idempotency_key || ':email') AS email_note, "
           "(SELECT m.sent_to FROM outbox m WHERE m.idempotency_key = o.idempotency_key || ':email') AS email_sent_to "
           f"FROM outbox o LEFT JOIN requests r ON r.id = o.request_id WHERE o.to_person=? AND o.idempotency_key NOT LIKE '%:email' AND o.kind IN ({_IN})"
           + "".join(" AND " + w for w in where) + " ORDER BY o.id DESC LIMIT ?")
    rows = engine.s.all(sql, (person_id, *NOTIFICATION_KINDS, *args, limit + 1))
    more = len(rows) > limit
    out = []
    for o in rows[:limit]:
        email = None
        if o["email_status"]:
            email = {"state": copy_state({"delivery_status": o["email_status"], "sent_to": o["email_sent_to"], "to_person": o["to_person"]}), "note": o["email_note"] or ""}
        t = engine.d.type(o["r_type"]) if o["r_type"] else None
        out.append({"id": o["id"], "kind": o["kind"], "category": KIND_CATEGORY[o["kind"]], "category_title": TITLES[KIND_CATEGORY[o["kind"]]],
                    "subject": o["subject"] or "", "text": o["body"] or "", "created_at": o["created_at"], "read": o["read_at"] is not None,
                    "request_id": o["request_id"], "type_code": o["r_type"], "type_name": t["name"] if t else None, "request_status": o["r_status"], "request_summary": o["r_summary"],
                    "needs_action": KIND_CATEGORY[o["kind"]] in ("decide", "task") and o["r_status"] in ("PENDING_APPROVAL", "FULFILLING"),
                    "slack": {"state": copy_state(o), "note": o["delivery_note"] or ""}, "email": email})
    return {"items": out, "unread": unread_count(engine, person_id), "next_before": out[-1]["id"] if more and out else None}


def unread_count(engine, person_id):
    return engine.s.one(f"SELECT COUNT(*) c FROM outbox WHERE to_person=? AND idempotency_key NOT LIKE '%:email' AND read_at IS NULL AND kind IN ({_IN})", (person_id, *NOTIFICATION_KINDS))["c"]


def mark_read(engine, person_id, ids=None, request_id=None, everything=False):
    """Mark the person's own notifications read: by id, everything about one request, or all. Safe to repeat. Returns how many changed."""
    base = f"UPDATE outbox SET read_at=? WHERE to_person=? AND idempotency_key NOT LIKE '%:email' AND read_at IS NULL AND kind IN ({_IN})"
    args = [engine.now(), person_id, *NOTIFICATION_KINDS]
    if everything:
        cur = engine.s.q(base, args)
    elif request_id is not None:
        cur = engine.s.q(base + " AND request_id=?", (*args, int(request_id)))
    elif ids:
        ids = [int(i) for i in ids][:200]
        cur = engine.s.q(base + f" AND id IN ({','.join('?' * len(ids))})", (*args, *ids))
    else:
        return 0
    return cur.rowcount
