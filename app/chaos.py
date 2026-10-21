"""Live failure scenarios for the demo. Each returns {'scenario', 'steps', 'checks', 'passed'}."""
import uuid

from .engine import BOT_SENDER


def _result(name, steps, checks):
    return {"scenario": name, "steps": steps, "checks": checks, "passed": all(checks.values())}


def _ensure_intake(e):
    """Demo helper: an earlier flood may have paused intake. Reset it as the admin so the next scenario can run."""
    if e.s.get_setting("breaker_open", ""):
        e.reset_breaker(e.d.role_holder("admin")["id"])


def duplicate_x4(e):
    _ensure_intake(e)
    eid = "dup-" + uuid.uuid4().hex[:8]
    outs = [e.ingest("chat", "e01", "Need a Claude seat for my work on the research project.", source_event_id=eid)["outcome"] for _ in range(4)]
    n = e.s.one("SELECT COUNT(*) c FROM requests WHERE source_event_id=?", (eid,))["c"]
    sup = e.s.one("SELECT COUNT(*) c FROM events WHERE type='duplicate_suppressed' AND data_json LIKE ?", (f"%{eid}%",))["c"]
    return _result("Same message delivered 4 times", [f"delivery {i + 1}: {o}" for i, o in enumerate(outs)],
                   {"exactly one request created": n == 1, "three duplicates suppressed": sup == 3})


def own_message(e):
    o = e.ingest("chat", BOT_SENDER, "Your request #1 was approved.", is_bot=True)["outcome"]
    n = e.s.one("SELECT COUNT(*) c FROM events WHERE type='own_message_ignored'")["c"]
    return _result("Agent receives its own reply", [f"own message: {o}"], {"ignored": o == "ignored_own_message", "audited": n >= 1})


def burst(e):
    outs = []
    for i in range(int(e.s.get_setting("rate_limit_per_min")) + 3):
        outs.append(e.ingest("chat", "e02", f"I need leave tomorrow, test {i}", source_event_id=f"burst-{uuid.uuid4().hex[:6]}-{i}")["outcome"])
    tripped = "breaker_opened" in outs
    paused = "paused_breaker" in outs
    admin_notified = e.s.one("SELECT COUNT(*) c FROM outbox WHERE kind='breaker'")["c"] >= 1
    return _result("Burst of messages (loop or flood)", [f"{len(outs)} events sent, last outcomes: {outs[-3:]}"],
                   {"breaker tripped": tripped, "intake paused afterwards": paused, "admin notified": admin_notified})


def _leave_request(e, emp="e01"):
    _ensure_intake(e)
    r = e.ingest("chat", emp, "I need leave tomorrow, family function", source_event_id="leave-" + uuid.uuid4().hex[:8])
    rid = r["request_ids"][0]
    approver = e.current_approver(e.req(rid))
    return rid, approver


def hr_fail_twice(e):
    e.hr.heal()
    before = len(e.hr.leaves)
    rid, ap = _leave_request(e)
    e.hr.fail_next = 2
    e.approve(rid, ap)
    r = e.req(rid)
    retries = e.s.one("SELECT COUNT(*) c FROM events WHERE request_id=? AND type='action_retry'", (rid,))["c"]
    return _result("HR system fails twice, then recovers", [f"retries: {retries}", f"final status: {r['status']}"],
                   {"request completed": r["status"] == "CLOSED", "leave applied exactly once": len(e.hr.leaves) - before == 1, "two retries logged": retries == 2})


def hr_timeout_after_success(e):
    e.hr.heal()
    before = len(e.hr.leaves)
    rid, ap = _leave_request(e)
    e.hr.timeout_after_effect = 1
    e.approve(rid, ap)
    r = e.req(rid)
    return _result("HR applies the leave but the reply is lost", [f"final status: {r['status']}", "retry found the existing leave (read before write)"],
                   {"request completed": r["status"] == "CLOSED", "leave applied exactly once": len(e.hr.leaves) - before == 1})


def hr_down_then_replay(e):
    e.hr.heal()
    before = len(e.hr.leaves)
    rid, ap = _leave_request(e)
    e.hr.down = True
    e.approve(rid, ap)
    parked = e.req(rid)["status"] == "PARKED"
    notified = e.s.one("SELECT COUNT(*) c FROM outbox WHERE request_id=? AND kind='parked'", (rid,))["c"] >= 1
    e.hr.heal()
    admin = e.d.role_holder("admin")["id"]
    e.replay(rid, admin)
    status = e.req(rid)["status"]
    return _result("HR down: request parked, then replayed", [f"after failures: {'PARKED' if parked else 'not parked'}", f"after replay: {status}"],
                   {"parked, not lost": parked, "admin notified": notified, "completed after replay": status == "CLOSED", "leave applied exactly once": len(e.hr.leaves) - before == 1})


def ai_budget(e):
    _ensure_intake(e)
    e.s.set_setting("ai_budget_per_day", 0)
    e.s.set_setting("ai_budget_exhausted_logged", "0")
    r = e.ingest("chat", "e03", "Need a pack of notebooks and some pens.", source_event_id="budget-" + uuid.uuid4().hex[:6])
    rid = r["request_ids"][0]
    prov = e.req(rid)["provider"]
    return _result("AI budget exhausted", [f"classified by: {prov}", "note: with no API key the rules are always used, so this shows the fallback path only"],
                   {"no error, request still handled": bool(r["request_ids"]), "falls back to rules": prov == "rules"})


SCENARIOS = {"duplicate_x4": duplicate_x4, "own_message": own_message, "burst": burst, "hr_fail_twice": hr_fail_twice,
             "hr_timeout_after_success": hr_timeout_after_success, "hr_down_then_replay": hr_down_then_replay, "ai_budget": ai_budget}
