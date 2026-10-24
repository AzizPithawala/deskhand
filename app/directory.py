"""Employee directory and routing map. Approvers come from here, never from message text.
A company's directory is its people, its business roles and holders, the built-in request types with the admin's edits applied, and its policies."""
import json
import os
import re

DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# The only type fields a company can change. Everything else (codes, names, the leave action) comes from the template.
EDITABLE_FIELDS = ("enabled", "requires_approval", "approver_rule", "backup_rule", "extra_approvers", "auto_allow_up_to",
                   "reminder_hours", "escalate_hours", "fulfiller_rule", "details_needed", "redirect")


# What a request type an admin creates starts with. Only these routing fields are editable (setup.CUSTOM_FIELDS); the rest is fixed here.
CUSTOM_DEFAULTS = {"enabled": True, "requires_approval": True, "approver_rule": "manager", "backup_rule": None, "extra_approvers": [], "reminder_hours": 8, "escalate_hours": 24,
                   "fulfiller_rule": "role:admin"}
CUSTOM_PREFIX = "x_"


def custom_type(row):
    """A company-defined type as a directory type. It is recognised by its trigger words (never carried here: /api/state sends these dicts to every member) and holds no detail fields."""
    t = {**CUSTOM_DEFAULTS, **json.loads(row["rules_json"] or "{}")}
    archived = bool(row["archived"])
    return {**t, "code": row["code"], "name": row["name"], "description": row["description"], "notes": row["description"], "fulfilment_kind": "manual_task", "details_needed": [],
            "sensitive": False, "currency": None, "custom": True, "archived": archived, "enabled": bool(t.get("enabled", True)) and not archived}


def trigger_pattern(words):
    """One literal, whole-word, any-case matcher for a type's trigger words. No pattern language: what the admin typed is what is matched."""
    alts = [re.escape(w.strip().lower()).replace("\\ ", r"\s+").replace("'", "['\u2019]") for w in words if w.strip()]
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.I) if alts else None


def normalize_type(t):
    """Canonical form of a request type. Adds the fields the UI reads (cost_threshold, second_approver_rule) from the first cost-based extra approver."""
    t = dict(t)
    extras = [dict(x) for x in (t.get("extra_approvers") or [])]
    t["extra_approvers"] = extras
    first = next((x for x in extras if isinstance(x.get("when"), dict) and x["when"].get("cost_above") is not None), None)
    t["cost_threshold"] = first["when"]["cost_above"] if first else None
    t["second_approver_rule"] = first["rule"] if first else None
    t.setdefault("enabled", True)
    return t


def _number(v):
    try:
        return float(v) if v not in (None, "", True, False) else None
    except (TypeError, ValueError):
        return None


class Directory:
    def __init__(self, employees, types, policies=None, balances=None, roles=None, triggers=None):
        self.employees = {e["id"]: e for e in employees}
        self.types = {t["code"]: normalize_type(t) for t in types}
        self.triggers = triggers or {}  # custom type code -> its trigger words (for the matcher and for admins; not part of the type dicts everyone can read)
        self.policies = policies or {}
        self.balances = balances or {}
        self.roles = roles or {}  # business roles of a company: code -> {"name", "holder_id", "builtin"}. Empty for the demo, which uses its JSON roles.

    @classmethod
    def template(cls, data_dir=DATA):
        return json.load(open(os.path.join(data_dir, "request_types.demo.json")))

    @classmethod
    def load(cls, data_dir=DATA):
        emp = json.load(open(os.path.join(data_dir, "employees.demo.json")))
        typ = cls.template(data_dir)
        return cls(emp["employees"], typ["request_types"], typ.get("policies"), emp.get("leave_balances"))

    @staticmethod
    def employee_from_user(u, held=()):
        """A signed-in person as a directory entry. `role` is the access level the router knows (admin or employee); `roles` are the business roles they hold."""
        return {"id": u["id"], "name": u["name"], "role": "admin" if u["access_role"] in ("owner", "admin") else "employee",
                "roles": list(held), "manager_id": u.get("manager_id"), "email": u["email"], "slack_id": u.get("slack_id") or "", "phone": u.get("phone") or "",
                "active": u["status"] == "active", "department": u.get("department") or ""}

    @classmethod
    def for_company(cls, users, store, data_dir=DATA):
        """A real company's directory: template types plus this company's edits, its roles and holders, its own policies (none until it writes them)."""
        roles = {r["code"]: {"name": r["name"], "holder_id": r["holder_id"], "builtin": bool(r["builtin"])}
                 for r in store.all("SELECT * FROM roles ORDER BY builtin DESC, created_at, code")}
        edits = {r["code"]: json.loads(r["rules_json"] or "{}") for r in store.all("SELECT code, rules_json FROM type_rules")}
        types = []
        for t in cls.template(data_dir)["request_types"]:
            patch = {k: v for k, v in (edits.get(t["code"]) or {}).items() if k in EDITABLE_FIELDS}
            types.append({**t, **patch})
        triggers = {}
        for row in store.all("SELECT * FROM custom_types ORDER BY created_at, code"):  # after the built-ins; an archived type stays so old requests keep working
            types.append(custom_type(row))
            triggers[row["code"]] = json.loads(row["triggers_json"] or "[]")
        policies = {p["key"]: p["text"] for p in store.all("SELECT key, text FROM policies ORDER BY key")}
        held = {}
        for code, r in roles.items():
            if r["holder_id"]:
                held.setdefault(r["holder_id"], []).append(code)
        return cls([cls.employee_from_user(u, held.get(u["id"], ())) for u in users], types, policies, {}, roles, triggers)

    def refresh(self, users, store):
        """Reload people, roles, type rules and policies in place: the engine keeps this object."""
        fresh = Directory.for_company(users, store)
        self.employees, self.types, self.policies, self.roles, self.triggers = fresh.employees, fresh.types, fresh.policies, fresh.roles, fresh.triggers

    def emp(self, eid):
        return self.employees.get(eid)

    def type(self, code):
        return self.types.get(code)

    def custom_matchers(self):
        """This company's own types that can be recognised right now: [{code, name, regex}]. Switched off and archived types never match."""
        out = []
        for code, words in self.triggers.items():
            t = self.types.get(code)
            rx = trigger_pattern(words) if t and t.get("enabled") and not t.get("archived") else None
            if rx:
                out.append({"code": code, "name": t["name"], "description": t.get("description", ""), "regex": rx})
        return out

    def resolve_sender(self, sender):
        s = (sender or "").strip().lower()
        for e in self.employees.values():
            if s in {e["id"].lower(), e["email"].lower(), e["slack_id"].lower(), e["phone"].replace(" ", "").lower()}:
                return e
        return None

    def role_holder(self, role):
        """The active person holding a business role. `admin` is any owner or admin. The demo's JSON people carry one role string each."""
        if role != "admin" and role in self.roles:
            e = self.employees.get(self.roles[role].get("holder_id"))
            return e if e and e["active"] else None
        for e in self.employees.values():
            if e["role"] == role and e["active"]:
                return e
        return None

    def manager_of(self, e):
        return self.employees.get(e["manager_id"]) if e and e.get("manager_id") else None

    def resolve_rule(self, rule, requester):
        """A rule is `manager`, `role:<code>` or `person:<id>`. Only people of this company can come out of it."""
        if not rule:
            return None
        if rule == "manager":
            return self.manager_of(requester)
        if rule.startswith("role:"):
            return self.role_holder(rule.split(":", 1)[1])
        if rule.startswith("person:"):
            e = self.employees.get(rule.split(":", 1)[1])
            return e if e and e["active"] else None
        return None

    def other_names_in(self, text, requester):
        """Employees (not the requester) whose first name appears as a word in the text."""
        found = []
        for e in self.employees.values():
            if e["id"] == requester["id"]:
                continue
            if re.search(r"\b" + re.escape(e["name"].split()[0]) + r"\b", text, re.I):
                found.append(e)
        return found

    @staticmethod
    def condition_holds(when, details):
        """Does an extra approver's condition hold for these structured details? No condition means always."""
        if not when:
            return True
        if when.get("cost_above") is not None:
            cost, limit = _number(details.get("estimated_cost")), _number(when["cost_above"])
            return bool(cost and limit is not None and cost > limit)
        dc = when.get("detail_contains")
        if dc and dc.get("words"):
            text = str(details.get(dc.get("field")) or "")
            return bool(re.search(r"\b(" + "|".join(re.escape(w) for w in dc["words"]) + r")\b", text, re.I))
        return False

    def approver_chain(self, t, requester, details):
        """Ordered list of approver ids. Rules: routing map + company edits + directory + structured details only."""
        chain = []

        def add(e):
            if e and e["id"] != requester["id"] and e["id"] not in chain:
                chain.append(e["id"])

        needs = t.get("requires_approval", False)
        cost, allow = _number(details.get("estimated_cost")), _number(t.get("auto_allow_up_to"))
        if not needs and allow is not None and cost and cost > allow:
            needs = True
        if not needs:
            return []
        add(self.resolve_rule(t.get("approver_rule") or "manager", requester))
        for x in t.get("extra_approvers") or []:
            if self.condition_holds(x.get("when"), details):
                add(self.resolve_rule(x.get("rule"), requester))
        if not chain:  # requester was their own approver or no manager: use the backup
            add(self.resolve_rule(t.get("backup_rule"), requester))
        if not chain:
            add(self.role_holder("admin"))
        if not chain:
            add(self.role_holder("hr_lead"))
        return chain
