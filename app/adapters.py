"""External systems behind adapters. This prototype ships a Keka-shaped mock HR system with failure injection.
The real viaSocket adapter will implement the same methods (see docs/VIASOCKET_INTEGRATION.md)."""


class ActionError(Exception):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class MockHR:
    label = "the HR system"
    applied = "applied in the HR system"
    records_balances = False  # its balances are the demo's prepared figures (Directory.balances), not records

    def __init__(self, directory):
        self.d = directory
        self.reset()

    def reset(self):
        self.leaves = []  # real effects, one per successful create
        self.calls = 0
        self.fail_next = 0          # next N calls fail with a retryable error, no effect
        self.timeout_after_effect = 0  # next N calls apply the effect, then raise a timeout
        self.down = False           # every call fails (retryable) until healed

    def heal(self):
        self.fail_next = 0
        self.timeout_after_effect = 0
        self.down = False

    def find_employee(self, email):
        e = self.d.resolve_sender(email)
        return {"id": e["id"], "name": e["name"], "manager_id": e["manager_id"]} if e else None

    def list_leave_balances(self, emp_id):
        return dict(self.d.balances.get(emp_id, {"casual": 6, "sick": 5}))

    def find_leave_by_ref(self, ref):
        for lv in self.leaves:
            if lv["ref"] == ref:
                return dict(lv)
        return None

    def create_leave_request(self, emp_id, dates, leave_type, reason, ref, half_day=False, date_range=False):
        self.calls += 1
        if self.down:
            raise ActionError("HR system unavailable (503)", retryable=True)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ActionError("HR system timed out (504)", retryable=True)
        lv = {"leave_id": f"L{len(self.leaves) + 1}", "emp_id": emp_id, "dates": dates, "type": leave_type,
              "reason": reason, "ref": ref, "half_day": half_day}
        self.leaves.append(lv)
        if self.timeout_after_effect > 0:
            self.timeout_after_effect -= 1
            raise ActionError("HR response lost after the leave was applied (timeout)", retryable=True)
        return dict(lv)
