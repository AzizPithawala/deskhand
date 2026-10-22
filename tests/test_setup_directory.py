"""Company setup as data: the golden check that the data-driven router matches the old hard-coded one, roles, rules and policies. No network."""
import copy
import itertools
import json
import os
import re

import pytest

from app import accounts as ac, registry as rg, setup
from app.db import Store
from app.directory import Directory, EDITABLE_FIELDS

HERE = os.path.dirname(__file__)
LEGACY = json.load(open(os.path.join(HERE, "fixtures", "request_types.legacy.json")))["request_types"]  # the template as it was before extra_approvers


class OldRouter(Directory):
    """The approver chain exactly as it was written before Phase 2 (access and accessories were special cases in code). Kept only as the reference."""

    def __init__(self, employees):
        super().__init__(employees, LEGACY)
        self.types = {t["code"]: t for t in LEGACY}  # the legacy fields, not the normalised ones

    def approver_chain(self, t, requester, details):
        chain = []

        def add(e):
            if e and e["id"] != requester["id"] and e["id"] not in chain:
                chain.append(e["id"])

        needs = t.get("requires_approval", False)
        cost = details.get("estimated_cost")
        if not needs and t["code"] == "accessories" and cost and cost > t.get("auto_allow_up_to", 0):
            needs = True
        if not needs:
            return []
        add(self.resolve_rule(t.get("approver_rule") or "manager", requester))
        thr = t.get("cost_threshold")
        if t.get("second_approver_rule") and thr is not None and cost and cost > thr and t["code"] != "access":
            add(self.resolve_rule(t["second_approver_rule"], requester))
        if t["code"] == "access":
            res = (details.get("resource") or "").lower()
            if "finance" in res:
                add(self.role_holder("finance_lead"))
            if re.search(r"\b(admin|root|aws|production|prod)\b", res):
                add(self.role_holder("it_admin"))
        if not chain:
            add(self.resolve_rule(t.get("backup_rule"), requester))
        if not chain:
            add(self.role_holder("admin"))
        if not chain:
            add(self.role_holder("hr_lead"))
        return chain


COSTS = [None, 0, 1, 1999, 2000, 2001, 4999, 5000, 5001, 19999, 20000, 20001, 100000]
RESOURCES = [None, "", "Drive", "shared finance folder", "Finance board", "finances drive", "finance-team wiki", "the financial report", "AWS console", "production database",
             "admin panel", "product roadmap", "prod", "root", "Root access to servers", "finance admin console"]


def people_variants():
    base = json.load(open(os.path.join(os.path.dirname(HERE), "data", "employees.demo.json")))["employees"]
    yield "all active", base
    for who in ("e06", "e09", "e07", "e08", "e10"):  # a role holder or the admin is away
        v = copy.deepcopy(base)
        next(e for e in v if e["id"] == who)["active"] = False
        yield f"{who} inactive", v
    v = copy.deepcopy(base)
    for e in v:
        e["manager_id"] = None
    yield "nobody has a manager", v


def test_new_router_gives_the_same_chain_as_the_old_one_for_every_combination():
    checked = 0
    for label, people in people_variants():
        new, old = Directory(copy.deepcopy(people), LEGACY_AS_TEMPLATE()), OldRouter(copy.deepcopy(people))
        for requester in new.employees.values():
            for code in old.types:
                for cost, res in itertools.product(COSTS, RESOURCES):
                    details = {"estimated_cost": cost, "resource": res}
                    a = new.approver_chain(new.type(code), new.emp(requester["id"]), details)
                    b = old.approver_chain(old.type(code), old.emp(requester["id"]), details)
                    assert a == b, (label, requester["id"], code, cost, res, a, b)
                    checked += 1
    assert checked > 50000


def LEGACY_AS_TEMPLATE():
    return Directory.template()["request_types"]  # the live template, in its new form


def test_refinance_no_longer_counts_as_finance():
    d = Directory.load()
    t, req = d.type("access"), d.emp("e01")
    assert d.approver_chain(t, req, {"resource": "refinance tool"}) == ["e04"]  # a word match, not a substring: the one deliberate tightening
    assert d.approver_chain(t, req, {"resource": "Finance folder"}) == ["e04", "e09"]


def test_template_is_in_the_new_form_and_the_ui_fields_are_derived():
    d = Directory.load()
    for code in ("software_seat", "equipment"):
        t = d.type(code)
        assert t["extra_approvers"][0]["rule"] == "manager" and t["cost_threshold"] == t["extra_approvers"][0]["when"]["cost_above"] and t["second_approver_rule"] == "manager"
    assert d.type("access")["extra_approvers"] and d.type("access")["cost_threshold"] is None
    assert d.type("accessories")["auto_allow_up_to"] == 2000 and d.type("leave")["enabled"] is True
    assert set(EDITABLE_FIELDS) >= {"approver_rule", "extra_approvers", "reminder_hours", "enabled"} and "code" not in EDITABLE_FIELDS and "fulfilment_kind" not in EDITABLE_FIELDS


# ---------- a real company: roles, holders, edits, policies ----------
def user(i, name, access="member", manager=None, status="active"):
    return {"id": i, "name": name, "email": f"{i}@co.test", "access_role": access, "status": status, "manager_id": manager, "department": "Ops", "slack_id": None, "phone": None}


@pytest.fixture
def co():
    s = Store()
    setup.seed_roles(s)
    users = [user("o", "Olive", "owner"), user("m", "Mia", manager="o"), user("a", "Ann", manager="m"), user("i", "Ivan", manager="o")]
    return s, users, Directory.for_company(users, s)


def test_a_new_company_has_four_roles_with_no_holders_and_no_policies(co):
    s, users, d = co
    assert sorted(d.roles) == ["finance_lead", "hr_lead", "it_admin", "office_admin"] and all(r["builtin"] and r["holder_id"] is None for r in d.roles.values())
    assert d.policies == {} and d.role_holder("it_admin") is None and d.role_holder("admin")["id"] == "o"
    seat = d.type("software_seat")
    assert d.approver_chain(seat, d.emp("a"), {}) == ["o"]  # no IT admin yet: backup rule, then an admin


def test_assigning_a_holder_changes_routing_and_a_deactivated_holder_falls_back(co):
    s, users, d = co
    s.q("UPDATE roles SET holder_id='i' WHERE code='it_admin'")
    d.refresh(users, s)
    assert d.role_holder("it_admin")["id"] == "i" and d.emp("i")["roles"] == ["it_admin"] and d.emp("a")["roles"] == []
    seat = d.type("software_seat")
    assert d.approver_chain(seat, d.emp("a"), {}) == ["i"]
    assert d.approver_chain(seat, d.emp("a"), {"estimated_cost": 6000}) == ["i", "m"]  # over the limit: the requester's manager joins
    users[3] = user("i", "Ivan", manager="o", status="disabled")
    d.refresh(users, s)
    assert d.role_holder("it_admin") is None and d.approver_chain(seat, d.emp("a"), {}) == ["o"]


def test_edits_override_only_editable_fields_and_a_person_can_be_the_approver(co):
    s, users, d = co
    patch = {"approver_rule": "person:i", "reminder_hours": 2, "escalate_hours": 6, "name": "Hacked", "fulfilment_kind": "auto_action", "code": "evil", "extra_approvers": []}
    s.q("INSERT INTO type_rules(code, rules_json) VALUES('equipment', ?)", (json.dumps(patch),))
    d.refresh(users, s)
    t = d.type("equipment")
    assert t["approver_rule"] == "person:i" and t["reminder_hours"] == 2 and t["extra_approvers"] == []
    assert t["name"] == "Equipment" and t["fulfilment_kind"] == "manual_task" and t["code"] == "equipment"  # not editable: ignored
    assert d.approver_chain(t, d.emp("a"), {"estimated_cost": 999999}) == ["i"] and d.type("leave")["approver_rule"] == "manager"
    s.q("DELETE FROM type_rules")
    d.refresh(users, s)
    assert d.type("equipment")["approver_rule"] == "role:office_admin"  # reverting restores the template


def test_always_condition_and_unknown_condition(co):
    s, users, d = co
    t = dict(d.type("it_help"), requires_approval=True, extra_approvers=[{"rule": "person:i", "when": None}, {"rule": "person:o", "when": {"bogus": 1}}])
    assert d.approver_chain(t, d.emp("a"), {}) == ["m", "i"]  # the manager first; no condition means always; an unknown condition never holds


def test_policies_come_from_the_company_table(co):
    s, users, d = co
    s.q("INSERT INTO policies(key,title,text) VALUES('work_from_home','Work from home','Three days a week.')")
    d.refresh(users, s)
    assert d.policies == {"work_from_home": "Three days a week."}
    assert Directory.load().policies["work_from_home"].startswith("Up to 2 days")  # the demo keeps its own texts


def test_a_demo_reset_keeps_company_setup():
    s = Store()
    setup.seed_roles(s)
    s.q("INSERT INTO policies(key,title,text) VALUES('k','T','x')")
    s.q("UPDATE roles SET holder_id='u' WHERE code='hr_lead'")
    s.reset()
    assert s.one("SELECT holder_id FROM roles WHERE code='hr_lead'")["holder_id"] == "u" and s.one("SELECT text FROM policies WHERE key='k'")


def test_registry_builds_company_directories_from_their_own_setup(tmp_path):
    acc, reg = rg.open_platform(str(tmp_path), ["c1", "c2"])
    o1, u1 = acc.register_company("One", "Owner One", "a@one.test", "a-long-password", "c1")
    o2, u2 = acc.register_company("Two", "Owner Two", "b@two.test", "a-long-password", "c2")
    e1, e2 = reg.engine_for(o1["id"]), reg.engine_for(o2["id"])
    assert sorted(e1.d.roles) == sorted(e2.d.roles) and e1.d.policies == {}
    e1.s.q("UPDATE roles SET holder_id=? WHERE code='it_admin'", (u1["id"],))
    reg.refresh_directory(o1["id"])
    assert e1.d.role_holder("it_admin")["id"] == u1["id"] and e2.d.role_holder("it_admin") is None  # one company's setup never reaches the other
    demo = reg.engine_for(rg.DEMO_ORG_ID)
    assert demo.d.roles == {} and demo.d.role_holder("it_admin")["id"] == "e06"
