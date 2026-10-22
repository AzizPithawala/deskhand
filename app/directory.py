"""Employee directory and routing map. Approvers come from here, never from message text."""
import json
import os
import re

DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


class Directory:
    def __init__(self, employees, types, policies=None, balances=None):
        self.employees = {e["id"]: e for e in employees}
        self.types = {t["code"]: t for t in types}
        self.policies = policies or {}
        self.balances = balances or {}

    @classmethod
    def load(cls, data_dir=DATA):
        emp = json.load(open(os.path.join(data_dir, "employees.demo.json")))
        typ = json.load(open(os.path.join(data_dir, "request_types.demo.json")))
        return cls(emp["employees"], typ["request_types"], typ.get("policies"), emp.get("leave_balances"))

    @staticmethod
    def employee_from_user(u):
        """A signed-in person as a directory entry. Access role maps to the business role the router knows until Phase 2 adds real roles."""
        return {"id": u["id"], "name": u["name"], "role": "admin" if u["access_role"] in ("owner", "admin") else "employee",
                "manager_id": u.get("manager_id"), "email": u["email"], "slack_id": u.get("slack_id") or "", "phone": u.get("phone") or "",
                "active": u["status"] == "active", "department": u.get("department") or ""}

    @classmethod
    def from_users(cls, users, data_dir=DATA):
        typ = json.load(open(os.path.join(data_dir, "request_types.demo.json")))
        return cls([cls.employee_from_user(u) for u in users], typ["request_types"], typ.get("policies"), {})

    def replace_employees(self, users):
        """Refresh the people in place (the engine keeps this object) after an admin changes the company's people."""
        self.employees = {u["id"]: self.employee_from_user(u) for u in users}

    def emp(self, eid):
        return self.employees.get(eid)

    def type(self, code):
        return self.types.get(code)

    def resolve_sender(self, sender):
        s = (sender or "").strip().lower()
        for e in self.employees.values():
            if s in {e["id"].lower(), e["email"].lower(), e["slack_id"].lower(), e["phone"].replace(" ", "").lower()}:
                return e
        return None

    def role_holder(self, role):
        for e in self.employees.values():
            if e["role"] == role and e["active"]:
                return e
        return None

    def manager_of(self, e):
        return self.employees.get(e["manager_id"]) if e and e.get("manager_id") else None

    def resolve_rule(self, rule, requester):
        if not rule:
            return None
        if rule == "manager":
            return self.manager_of(requester)
        if rule.startswith("role:"):
            return self.role_holder(rule.split(":", 1)[1])
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

    def approver_chain(self, t, requester, details):
        """Ordered list of approver ids. Rules: routing map + directory + structured details only."""
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
        if not chain:  # requester was their own approver or no manager: use the backup
            add(self.resolve_rule(t.get("backup_rule"), requester))
        if not chain:
            add(self.role_holder("admin"))
        if not chain:
            add(self.role_holder("hr_lead"))
        return chain
