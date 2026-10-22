"""Company setup: business roles, request-type rules and policies are data an admin edits. Validation lives here so every edit is checked the same way.
Nothing in this module reads message text. Rules name a role, the requester's manager or a person of this company; the router resolves them (rule 1)."""
import functools
import json
import re
import time

from .directory import EDITABLE_FIELDS, Directory, normalize_type

DEFAULT_ROLES = [("it_admin", "IT admin"), ("office_admin", "Office admin"), ("hr_lead", "HR lead"), ("finance_lead", "Finance lead")]
RESERVED_ROLES = {"admin", "manager", "person", "system", "role"}
MAX_CUSTOM_ROLES, MAX_POLICIES, MAX_EXTRA = 20, 30, 2
HOURS_MAX, AMOUNT_MAX = 720, 10_000_000
RULE = re.compile(r"^(manager|role:[a-z][a-z0-9_]{1,30}|person:[A-Za-z0-9_-]{1,64})$")
WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,29}$")
CONDITION_FIELDS = {"leave": ["leave_type", "reason"], "software_seat": ["tool", "project", "justification"], "equipment": ["item", "reason"],
                    "accessories": ["item", "reason"], "it_help": ["issue"], "access": ["resource", "tool"]}  # only details the readers actually extract


class SetupError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


@functools.lru_cache(maxsize=1)
def _template():
    return {t["code"]: normalize_type(t) for t in Directory.template()["request_types"]}


def seed_roles(store):
    """The four built-in business roles exist in every company from the start, with no holder."""
    for code, name in DEFAULT_ROLES:
        store.q("INSERT OR IGNORE INTO roles(code,name,holder_id,builtin,created_at) VALUES(?,?,NULL,1,?)", (code, name, time.time()))


def _clean(text, low, high, what):
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    s = re.sub(r"[ \t]+", " ", s).strip()
    if not low <= len(s) <= high:
        raise SetupError(f"{what} must be {low} to {high} characters.")
    return s


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30]


# ---------------- roles ----------------
def _role_rows(d):
    """Roles of this company. The demo company has none in a table: its four roles are read from its JSON people, read-only."""
    if d.roles:
        return [{"code": c, "name": r["name"], "builtin": r["builtin"], "holder_id": r["holder_id"]} for c, r in d.roles.items()]
    return [{"code": c, "name": n, "builtin": True, "holder_id": next((e["id"] for e in d.employees.values() if e["role"] == c and e["active"]), None)} for c, n in DEFAULT_ROLES]


def _rules_of(t):
    out = [t.get(k) for k in ("approver_rule", "backup_rule", "fulfiller_rule", "redirect")]
    out += [x.get("rule") for x in t.get("extra_approvers") or []]
    return [r for r in out if isinstance(r, str) and RULE.match(r)]  # `system:` actions are fixed in code, not rules an admin sets


def list_roles(d):
    used = {}
    for t in d.types.values():
        for r in _rules_of(t):
            if r.startswith("role:"):
                used.setdefault(r[5:], set()).add(t["name"])
    out = []
    for r in _role_rows(d):
        h = d.employees.get(r["holder_id"]) if r["holder_id"] else None
        out.append({**r, "holder_name": h["name"] if h else None, "holder_state": "none" if not r["holder_id"] else ("ok" if h and h["active"] else "inactive"),
                    "used_by": sorted(used.get(r["code"], []))})
    return out


def add_role(store, d, name):
    name = _clean(name, 2, 40, "A role name")
    code = _slug(name)
    if len(code) < 2 or code in RESERVED_ROLES or code in d.roles:
        raise SetupError("There is already a role with that name, or the name is reserved.")
    if sum(1 for r in d.roles.values() if not r["builtin"]) >= MAX_CUSTOM_ROLES:
        raise SetupError(f"A company can have up to {MAX_CUSTOM_ROLES} custom roles.")
    store.q("INSERT INTO roles(code,name,holder_id,builtin,created_at) VALUES(?,?,NULL,0,?)", (code, name, time.time()))
    return code


def set_holder(store, d, code, holder_id):
    if code not in d.roles:
        raise SetupError("No such role.", 404)
    if holder_id:
        e = d.employees.get(holder_id)
        if not e or not e["active"]:
            raise SetupError("Choose someone from this company who has joined and is active.")
    old = d.roles[code]["holder_id"]
    store.q("UPDATE roles SET holder_id=? WHERE code=?", (holder_id or None, code))
    return {"holder_id": [old, holder_id or None]}


def delete_role(store, d, code):
    r = d.roles.get(code)
    if not r:
        raise SetupError("No such role.", 404)
    if r["builtin"]:
        raise SetupError("Built-in roles cannot be removed. You can leave them without a holder.")
    users = sorted(t["name"] for t in d.types.values() if f"role:{code}" in _rules_of(t))
    if users:
        raise SetupError(f"{r['name']} is still used by: {', '.join(users)}. Change those rules first.")
    store.q("DELETE FROM roles WHERE code=?", (code,))


# ---------------- request types ----------------
def fields_for(t):
    code = t["code"]
    if code == "payroll":
        return ["redirect"]
    if code == "policy_question":
        return ["enabled"]
    f = ["enabled", "requires_approval", "approver_rule", "backup_rule", "extra_approvers", "reminder_hours", "escalate_hours", "details_needed"]
    if "auto_allow_up_to" in t:
        f.append("auto_allow_up_to")
    if t.get("fulfilment_kind") == "manual_task":
        f.append("fulfiller_rule")
    return f


def describe_rule(d, rule):
    """A rule as words, plus whether it can resolve to a person right now."""
    if not rule:
        return {"rule": rule, "label": "No one", "ok": False}
    if rule == "manager":
        return {"rule": rule, "label": "The requester's manager", "ok": True}
    kind, _, val = rule.partition(":")
    if kind == "person":
        e = d.employees.get(val)
        return {"rule": rule, "label": e["name"] if e else "Unknown person", "ok": bool(e and e["active"]), "note": None if e and e["active"] else "not active"}
    if val == "admin":
        h = d.role_holder("admin")
        return {"rule": rule, "label": "An admin" + (f" ({h['name']})" if h else ""), "ok": bool(h), "note": None if h else "no admin"}
    names = {r["code"]: r["name"] for r in _role_rows(d)}
    h = d.role_holder(val)
    return {"rule": rule, "label": f"{names.get(val, val)}" + (f" ({h['name']})" if h else ""), "ok": bool(h), "note": None if h else "no holder"}


def type_view(d, store, code):
    t, base = d.types[code], _template()[code]
    row = store.one("SELECT reviewed_at FROM type_rules WHERE code=?", (code,)) or {}
    fields = fields_for(t)
    changed = [k for k in fields if t.get(k) != base.get(k)]
    return {"code": code, "name": t["name"], "notes": t.get("notes", ""), "currency": t.get("currency"), "blocked": bool(t.get("blocked")),
            "fulfilment_kind": t.get("fulfilment_kind"), "fields": fields, "changed": changed, "reviewed_at": row.get("reviewed_at"),
            "condition_fields": CONDITION_FIELDS.get(code, []), "details_known": base.get("details_needed", []),
            **{k: t.get(k) for k in ("enabled", "requires_approval", "approver_rule", "backup_rule", "extra_approvers", "auto_allow_up_to", "reminder_hours",
                                     "escalate_hours", "fulfiller_rule", "details_needed", "redirect")},
            "labels": {r: describe_rule(d, r) for r in sorted(set(_rules_of(t)))}}


def list_types(d, store):
    return [type_view(d, store, c) for c in d.types]


def _rule(d, v, what, none_ok=False):
    if v is None:
        if none_ok:
            return None
        raise SetupError(f"{what} needs a choice.")
    if not isinstance(v, str) or not RULE.match(v):
        raise SetupError(f"{what}: that is not a valid choice.")
    if v.startswith("role:"):
        code = v[5:]
        if code != "admin" and code not in d.roles:
            raise SetupError(f"{what}: there is no role called {code}.")
    elif v.startswith("person:"):
        e = d.employees.get(v[7:])
        if not e or not e["active"]:
            raise SetupError(f"{what}: choose someone from this company who has joined and is active.")
    return v


def _number(v, what, low=0, high=AMOUNT_MAX):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= v <= high:
        raise SetupError(f"{what} must be a number from {low} to {high}.")
    return int(v) if float(v).is_integer() else float(v)


def _extras(d, code, v):
    if not isinstance(v, list) or len(v) > MAX_EXTRA:
        raise SetupError(f"A request type can have up to {MAX_EXTRA} extra approvers.")
    out = []
    for i, x in enumerate(v, 1):
        if not isinstance(x, dict):
            raise SetupError(f"Extra approver {i}: not understood.")
        rule, when = _rule(d, x.get("rule"), f"Extra approver {i}"), x.get("when")
        if when in (None, {}):
            out.append({"rule": rule, "when": None})
        elif isinstance(when, dict) and set(when) == {"cost_above"}:
            out.append({"rule": rule, "when": {"cost_above": _number(when["cost_above"], f"Extra approver {i}: the cost limit")}})
        elif isinstance(when, dict) and set(when) == {"detail_contains"} and isinstance(when["detail_contains"], dict):
            dc = when["detail_contains"]
            if dc.get("field") not in CONDITION_FIELDS.get(code, []):
                raise SetupError(f"Extra approver {i}: choose one of {', '.join(CONDITION_FIELDS.get(code, [])) or 'no fields'} for this type.")
            words = dc.get("words")
            if not isinstance(words, list) or not 1 <= len(words) <= 10 or not all(isinstance(w, str) and WORD.match(w.strip()) for w in words):
                raise SetupError(f"Extra approver {i}: give 1 to 10 words (letters, numbers, spaces, dashes).")
            out.append({"rule": rule, "when": {"detail_contains": {"field": dc["field"], "words": [w.strip().lower() for w in words]}}})
        else:
            raise SetupError(f"Extra approver {i}: the condition must be a cost limit or words in a detail.")
    return out


def validate_patch(d, code, patch):
    """Return a clean patch or raise SetupError. Only fields this type allows; each value checked; nothing taken from message text."""
    t = d.types.get(code)
    if not t:
        raise SetupError("No such request type.", 404)
    allowed = fields_for(t)
    out = {}
    for k, v in patch.items():
        if k not in EDITABLE_FIELDS or k not in allowed:
            raise SetupError(f"{k.replace('_', ' ')} cannot be changed for {t['name']}.")
        if k in ("enabled", "requires_approval"):
            if not isinstance(v, bool):
                raise SetupError(f"{k.replace('_', ' ')} must be on or off.")
            out[k] = v
        elif k == "approver_rule":
            out[k] = _rule(d, v, "The approver")
        elif k == "backup_rule":
            out[k] = _rule(d, v, "The backup", none_ok=True)
        elif k == "fulfiller_rule":
            out[k] = _rule(d, v, "The person who does the task")
        elif k == "redirect":
            out[k] = _rule(d, v, "The person to contact")
        elif k == "extra_approvers":
            out[k] = _extras(d, code, v)
        elif k == "auto_allow_up_to":
            out[k] = _number(v, "The automatic allowance")
        elif k in ("reminder_hours", "escalate_hours"):
            out[k] = _number(v, k.replace("_", " "), 1, HOURS_MAX)
        elif k == "details_needed":
            known = _template()[code].get("details_needed", [])
            if not isinstance(v, list) or any(x not in known for x in v):
                raise SetupError(f"Choose from: {', '.join(known)}.")
            out[k] = [x for x in known if x in v]
    if "reminder_hours" in out or "escalate_hours" in out:
        rem, esc = out.get("reminder_hours", t.get("reminder_hours")), out.get("escalate_hours", t.get("escalate_hours"))
        if esc <= rem:
            raise SetupError("Escalation must come later than the reminder.")
    return out


def _overrides(store, code):
    row = store.one("SELECT rules_json FROM type_rules WHERE code=?", (code,))
    return json.loads(row["rules_json"]) if row and row["rules_json"] else {}


def _save(store, code, overrides, actor, reviewed):
    now = time.time()
    store.q("INSERT INTO type_rules(code,rules_json,reviewed_at,updated_at,updated_by) VALUES(?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET rules_json=excluded.rules_json,"
            " reviewed_at=COALESCE(excluded.reviewed_at, type_rules.reviewed_at), updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (code, json.dumps(overrides, sort_keys=True), now if reviewed else None, now, actor))


def patch_type(store, d, code, patch, actor):
    """Apply a validated edit. Returns {field: [old, new]} for the audit record."""
    clean = validate_patch(d, code, patch)
    base, t = _template()[code], d.types[code]
    over, changes = _overrides(store, code), {}
    for k, v in clean.items():
        if t.get(k) != v:
            changes[k] = [t.get(k), v]
        if base.get(k) == v:
            over.pop(k, None)  # back to the template default: nothing to remember
        else:
            over[k] = v
    _save(store, code, over, actor, reviewed=True)  # editing a type counts as having looked at it
    return changes


def reset_type(store, d, code, actor):
    if code not in _template():
        raise SetupError("No such request type.", 404)
    changes = {k: [d.types[code].get(k), _template()[code].get(k)] for k in fields_for(d.types[code]) if d.types[code].get(k) != _template()[code].get(k)}
    _save(store, code, {}, actor, reviewed=False)
    return changes


def mark_reviewed(store, code, actor):
    if code not in _template():
        raise SetupError("No such request type.", 404)
    _save(store, code, _overrides(store, code), actor, reviewed=True)


# ---------------- policies ----------------
def list_policies(d, store):
    titles = {r["key"]: r["title"] for r in store.all("SELECT key, title FROM policies")}
    return [{"key": k, "title": titles.get(k) or k.replace("_", " ").capitalize(), "text": v} for k, v in sorted(d.policies.items())]


def set_policy(store, d, title, text):
    title, text = _clean(title, 2, 60, "The policy name"), _clean(text, 1, 1000, "The policy text")
    key = _slug(title)
    if len(key) < 2:
        raise SetupError("Give the policy a name with letters or numbers.")
    if key not in d.policies and len(d.policies) >= MAX_POLICIES:
        raise SetupError(f"A company can have up to {MAX_POLICIES} policies.")
    store.q("INSERT INTO policies(key,title,text,updated_at) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title, text=excluded.text, updated_at=excluded.updated_at",
            (key, title, text, time.time()))
    return key


def delete_policy(store, d, key):
    if key not in d.policies:
        raise SetupError("No such policy.", 404)
    store.q("DELETE FROM policies WHERE key=?", (key,))


# ---------------- routing test ----------------
def simulate(d, person_id, code, estimated_cost=None, resource=None):
    """What would happen to this person's request of this type? Uses the same resolve_rule and approver_chain the engine uses, so the answer cannot drift from real routing."""
    e, t = d.emp(person_id), d.type(code)
    if not e or not e["active"]:
        raise SetupError("Choose someone from this company who has joined and is active.")
    if not t:
        raise SetupError("No such request type.", 404)
    details = {}
    if estimated_cost is not None:
        details["estimated_cost"] = _number(estimated_cost, "The cost")
    if resource:
        details["resource"] = _clean(resource, 1, 120, "The resource")
    nm = lambda x: {"id": x["id"], "name": x["name"]} if x else None
    out = {"type": t["name"], "person": e["name"], "chain": [], "warnings": [], "fulfiller": None, "escalates_to": None,
           "reminder_hours": t.get("reminder_hours"), "escalate_hours": t.get("escalate_hours")}
    if not t.get("enabled", True):
        return {**out, "outcome": "disabled", "text": f"{t['name']} is switched off. {e['name']} is told it is not handled here."}
    if t.get("blocked"):
        who = d.resolve_rule(t.get("redirect"), e)
        return {**out, "outcome": "blocked", "text": f"{t['name']} is never processed. {e['name']} is told to contact {who['name'] if who else 'HR'} privately."}
    if code == "policy_question":
        return {**out, "outcome": "answered", "text": "Deskhand answers this itself from the company's policies. No approval."}
    chain = d.approver_chain(t, e, details)
    why = {}  # label each approver by the rule that produced them
    prim = d.resolve_rule(t.get("approver_rule") or "manager", e)
    if prim:
        why.setdefault(prim["id"], "approver")
    for x in t.get("extra_approvers") or []:
        who = d.resolve_rule(x.get("rule"), e)
        if who and d.condition_holds(x.get("when"), details):
            why.setdefault(who["id"], "extra approver")
    out["chain"] = [{**nm(d.emp(i)), "reason": why.get(i, "fallback: backup rule or an admin")} for i in chain]
    for r in sorted(set(_rules_of(t))):
        v = describe_rule(d, r)
        if not v["ok"] and r != "manager":
            out["warnings"].append(f"{v['label']}: {v.get('note') or 'cannot be found'}. Requests that need it use the backup, then an admin.")
    if not e.get("manager_id") and "manager" in _rules_of(t) + ([] if t.get("approver_rule") else ["manager"]):
        out["warnings"].append(f"{e['name']} has no manager set, so a rule that means 'the manager' finds no one.")
    if chain:
        back = d.resolve_rule(t.get("backup_rule"), e)
        if not back or back["id"] == chain[0]:
            back = d.role_holder("admin")
        out["escalates_to"], out["outcome"] = nm(back), "approval"
    else:
        out["outcome"] = "no_approval"
    if t.get("fulfilment_kind") == "auto_action":
        out["fulfilment"] = "Deskhand applies it in the HR system" if d.balances else "Deskhand records it in the company's leave record"
    else:
        ful = d.resolve_rule(t.get("fulfiller_rule"), e) or d.role_holder("admin")
        out["fulfiller"], out["fulfilment"] = nm(ful), f"{ful['name'] if ful else 'no one'} does the task and clicks Done"
    names = " then ".join(c["name"] for c in out["chain"]) or "no one"
    out["text"] = (f"Needs approval from {names}." if chain else "No approval needed: it goes straight to fulfilment.") + f" {out['fulfilment']}."
    return out


# ---------------- checklist ----------------
def checklist(d, store, people, connected, requests, mode="rehearsal", rehearsal_name=""):
    """Setup steps, computed from the data. `people`: the company's users; `connected`: {app: bool}; `requests`: how many requests exist."""
    joined = [p for p in people if p["status"] in ("active", "invited")]
    others = [p for p in joined if p["access_role"] != "owner"]
    managed = [p for p in others if p.get("manager_id")]
    roles = [r for r in list_roles(d) if r["builtin"]]
    assigned = [r for r in roles if r["holder_state"] == "ok"]
    reviewed = {r["code"] for r in store.all("SELECT code FROM type_rules WHERE reviewed_at IS NOT NULL")}
    codes = list(d.types)
    steps = [
        {"key": "people", "title": "Invite your people", "done": len(joined) >= 2, "detail": f"{len(joined)} {'person' if len(joined) == 1 else 'people'} so far", "link": "#people"},
        {"key": "managers", "title": "Set who reports to whom", "done": bool(others) and len(managed) == len(others), "detail": f"{len(managed)} of {len(others)} have a manager", "link": "#people"},
        {"key": "roles", "title": "Choose who holds each business role", "done": len(assigned) == len(roles), "detail": f"{len(assigned)} of {len(roles)} roles have a holder", "link": "#setup/roles"},
        {"key": "types", "title": "Review the request types", "done": all(c in reviewed for c in codes), "detail": f"{len([c for c in codes if c in reviewed])} of {len(codes)} reviewed", "link": "#setup/types"},
        {"key": "apps", "title": "Connect Slack and Gmail", "done": bool(connected.get("slack") and connected.get("gmail")),
         "detail": ", ".join(f"{a.capitalize()} {'connected' if connected.get(a) else 'not connected'}" for a in ("slack", "gmail")), "link": "#integrations"},
        {"key": "test", "title": "Send a test request", "done": requests > 0, "detail": f"{requests} request{'s' if requests != 1 else ''} so far", "link": "#ask"},
        {"key": "live", "title": "Switch to live messaging", "done": mode == "live",
         "detail": "Live: Slack messages and emails reach the real people" if mode == "live" else f"Rehearsal: everything goes only to {rehearsal_name or 'an admin'}", "link": "#integrations"},
    ]
    return {"steps": steps, "done": sum(1 for s in steps if s["done"]), "total": len(steps), "dismissed": store.get_setting("checklist_dismissed") == "1"}
