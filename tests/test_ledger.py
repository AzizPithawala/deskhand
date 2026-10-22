"""Deskhand's own leave records: counting rules, balances, idempotent deduction, append-only, warnings, and the leave flow in a company. No network."""
import json
import sqlite3
from datetime import date

import pytest

from app import registry as rg
from app.adapters import ActionError
from app.db import Store
from app.ledger import Ledger, LedgerHR, leave_dates, num, working_days

FRI, SAT, SUN, MON, TUE = "2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27"


@pytest.fixture
def led():
    return Ledger(Store(), today=lambda: date(2026, 10, 22))


def test_working_days_count_monday_to_friday_only():
    assert [d.isoformat() for d, w in working_days([FRI, MON])] == [FRI, MON]  # two separate days
    assert [d.isoformat() for d, w in working_days([FRI, TUE], date_range=True)] == [FRI, MON, TUE]  # a range skips the weekend
    assert working_days([SAT, SUN]) == [] and working_days([SAT, MON], date_range=True)[0][0].isoformat() == MON
    assert working_days([FRI], half_day=True) == [(date(2026, 10, 23), 0.5)] and working_days([FRI, MON], half_day=True) == [(date(2026, 10, 23), 0.5)]
    assert working_days(["nonsense", None]) == [] and working_days(None) == []
    assert working_days(["2026-01-01", "2030-01-01"], date_range=True) == []  # a multi-year range is refused, not expanded
    with pytest.raises(ValueError):
        leave_dates(["2026-01-01", "2026-12-31"], date_range=True)


def test_everyone_gets_the_yearly_allowance_once_and_balances_follow_the_ledger(led):
    assert led.balances("u1") == {"casual": 12, "sick": 8} and led.balances("u1") == {"casual": 12, "sick": 8}  # asking twice does not grant twice
    assert len(led.s.all("SELECT * FROM leave_ledger WHERE kind='grant'")) == 2
    led.apply("u1", "casual", working_days([FRI, MON]), "deskhand-1", 1, "family")
    assert led.balance("u1", "casual") == 10 and led.balance("u1", "sick") == 8 and led.balance("u2", "casual") == 12  # people are separate
    assert led.adjust("u1", "casual", 3, "joined mid-year, extra days agreed", "admin1") == 13
    assert led.adjust("u1", "casual", -0.5, "correction", "admin1") == 12.5
    rows = led.entries("u1")
    assert [r["kind"] for r in rows if r["leave_type"] == "casual"] == ["adjust", "adjust", "use", "grant"] and rows[0]["actor"] == "admin1" and "correction" in rows[0]["note"]


def test_a_leave_is_deducted_once_whatever_happens_to_the_call(led):
    r1 = led.apply("u1", "casual", working_days([FRI, MON]), "deskhand-7", 7, "x")
    r2 = led.apply("u1", "casual", working_days([FRI, MON]), "deskhand-7", 7, "x")  # a retry after a lost answer
    assert r1 == r2 and r1["days"] == 2 and r1["ref"] == "deskhand-7" and led.balance("u1", "casual") == 10
    assert led.find("deskhand-7")["dates"] == [FRI, MON] and led.find("deskhand-8") is None
    hr = LedgerHR(led)
    assert hr.find_leave_by_ref("deskhand-7")["leave_id"] == r1["leave_id"] and len(hr.leaves) == 1
    again = hr.create_leave_request("u1", [FRI, MON], "casual", "x", "deskhand-7")  # even a second create with the same reference changes nothing
    assert again["leave_id"] == r1["leave_id"] and led.balance("u1", "casual") == 10


def test_a_leave_across_new_year_is_split_per_year_all_or_nothing(led):
    r = led.apply("u1", "casual", working_days(["2026-12-30", "2027-01-04"], date_range=True), "deskhand-9", 9, "holiday")
    assert r["days"] == 4 and led.balance("u1", "casual", 2026) == 10 and led.balance("u1", "casual", 2027) == 10  # 30, 31 Dec in 2026; 1 and 4 Jan in 2027
    assert len(led.s.all("SELECT * FROM leave_ledger WHERE group_ref='deskhand-9'")) == 2


def test_the_ledger_cannot_be_edited_or_deleted(led):
    led.balance("u1", "casual")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        led.s.q("UPDATE leave_ledger SET days=100")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        led.s.q("DELETE FROM leave_ledger")
    assert led.balance("u1", "casual") == 12


def test_a_request_is_checked_before_anyone_decides_and_a_low_balance_only_warns(led):
    ok = led.check("u1", {"dates": [FRI, MON], "leave_type": "casual"})
    assert ok == {"days": 2, "type": "casual", "year": 2026, "balance": 12, "after": 10, "warnings": []}
    led.adjust("u1", "sick", -7, "used elsewhere", "a")
    low = led.check("u1", {"dates": [FRI, TUE], "date_range": True, "leave_type": "sick"})
    assert low["days"] == 3 and low["after"] == -2 and "3 working days needed, 1 left in sick leave" in low["warnings"][0] and "balance would be -2" in low["warnings"][0]
    assert "No working days" in led.check("u1", {"dates": [SAT, SUN]})["warnings"][0] and led.check("u1", {"dates": [SAT]})["days"] == 0
    led.apply("u1", "casual", working_days([MON]), "deskhand-2", 2, "x")
    assert "Overlaps leave already recorded for 2026-10-26" in led.check("u1", {"dates": [MON, TUE]})["warnings"][0]
    half = led.check("u1", {"dates": [FRI], "half_day": True, "leave_type": "casual"})
    assert half["days"] == 0.5 and half["after"] == 10.5
    assert led.check("u1", {"dates": [FRI], "leave_type": "bogus"})["type"] == "casual"  # an unknown type from a reader falls back, never fails


def test_nothing_to_deduct_is_a_permanent_refusal_not_a_retry(led):
    hr = LedgerHR(led)
    with pytest.raises(ActionError) as e:
        hr.create_leave_request("u1", [SAT, SUN], "casual", "x", "deskhand-3")
    assert e.value.retryable is False and led.balance("u1", "casual") == 12
    assert num(12.0) == 12 and num(1.5) == 1.5 and num(-0.25) == -0.25


def test_allowances_are_validated_and_apply_to_people_not_yet_granted(led):
    assert led.balance("u1", "casual") == 12
    assert led.set_yearly("casual", 15) == 12
    assert led.balance("u1", "casual") == 12 and led.balance("u2", "casual") == 15  # u1 already has this year's grant; an admin adjusts them
    for bad in (-1, 367, 1.25, "ten", True, None):
        with pytest.raises(ValueError):
            led.set_yearly("casual", bad)
    with pytest.raises(ValueError):
        led.set_yearly("bogus", 5)
    for bad in (0, 400, 0.3, "x", True):
        with pytest.raises(ValueError):
            led.adjust("u1", "casual", bad, "n", "a")


# ---------------- the whole flow in a company ----------------
@pytest.fixture
def company(tmp_path):
    acc, reg = rg.open_platform(str(tmp_path), ["c1"])
    org, owner = acc.register_company("Ledger Co", "Olive Owner", "o@l.test", "a-long-password", "c1")
    boss, _ = acc.create_invite(org["id"], "boss@l.test", "Bo Boss", "member", owner["id"])
    acc.accept_invite(acc.create_reset_link(org["id"], boss["id"], owner["id"]), "a-long-password")
    ann, _ = acc.create_invite(org["id"], "ann@l.test", "Ann Employee", "member", owner["id"], manager_id=boss["id"])
    acc.accept_invite(acc.create_reset_link(org["id"], ann["id"], owner["id"]), "a-long-password")
    reg.refresh_directory(org["id"])
    return reg.engine_for(org["id"]), owner, boss, ann


def test_a_company_leave_flows_from_ask_to_the_ledger_and_the_requester_is_told(company):
    e, owner, boss, ann = company
    rid = e.ingest("chat", ann["id"], "I need Friday and Monday off, family function.")["request_ids"][0]
    r = e.req(rid)
    assert r["chain"] == [boss["id"]] and r["details"]["leave_check"]["days"] == 2 and r["details"]["leave_check"]["after"] == 10 and r["details"]["balance"] == {"casual": 12, "sick": 8}
    rows = dict(r["details_view"])
    assert rows["Working days"] == "2" and rows["Balance"] == "casual 12 → 10" and "Leave balance" not in rows and "Date range" not in rows
    assert e.ledger.balance(ann["id"], "casual") == 12  # nothing is deducted until it is approved
    e.approve(rid, boss["id"])
    assert e.req(rid)["status"] == "CLOSED" and e.ledger.balance(ann["id"], "casual") == 10
    done = [m for m in e.s.all("SELECT * FROM outbox WHERE request_id=? AND kind='fulfilled'", (rid,))][0]
    assert "recorded in your leave balance" in done["body"] and "Casual leave left: 10" in done["body"] and "HR system" not in done["body"]
    assert e.dashboard()["hr_effects"] == 1
    e.replay  # noqa: B018 - the engine API is unchanged
    again = e.ledger.apply(ann["id"], "casual", working_days([FRI, MON]), f"deskhand-{rid}", rid, "x")
    assert again["days"] == 2 and e.ledger.balance(ann["id"], "casual") == 10  # replay-safe


def test_a_range_and_a_half_day_are_counted_from_the_message(company):
    e, owner, boss, ann = company
    a = e.ingest("chat", ann["id"], "Need leave from 2 Nov to 6 Nov for my sister's wedding.")["request_ids"][0]
    assert e.req(a)["details"]["date_range"] is True and e.req(a)["details"]["leave_check"]["days"] == 5
    b = e.ingest("chat", ann["id"], "Can I take a half day tomorrow afternoon? Doctor appointment.", "x2")["request_ids"][0]
    assert e.req(b)["details"]["leave_check"]["days"] == 0.5 or e.req(b)["status"] == "NEEDS_INFO"  # tomorrow may be a weekend: then it asks instead


def test_dates_on_a_weekend_ask_again_instead_of_going_to_the_approver(company):
    e, owner, boss, ann = company
    rid = e.ingest("chat", ann["id"], "I need Saturday and Sunday off, family function.")["request_ids"][0]
    r = e.req(rid)
    assert r["status"] == "NEEDS_INFO" and r["chain"] == []
    msgs = [m["body"] for m in e.s.all("SELECT body FROM outbox WHERE request_id=?", (rid,))]
    assert any("fall on a weekend" in m for m in msgs) and not any(m["kind"] == "approval_request" for m in e.s.all("SELECT kind FROM outbox WHERE request_id=?", (rid,)))


def test_low_balance_warns_the_approver_who_still_decides(company):
    e, owner, boss, ann = company
    e.ledger.adjust(ann["id"], "casual", -11, "used earlier", owner["id"])
    rid = e.ingest("chat", ann["id"], "I need Friday and Monday off, family function.")["request_ids"][0]
    r = e.req(rid)
    assert r["status"] == "PENDING_APPROVAL" and "2 working days needed, 1 left" in dict(r["details_view"])["Check"]
    note = e.s.one("SELECT body FROM outbox WHERE request_id=? AND kind='approval_request'", (rid,))["body"]
    assert "Check: 2 working days needed, 1 left in casual leave" in note  # the approver is told in the message itself
    e.approve(rid, boss["id"])
    assert e.ledger.balance(ann["id"], "casual") == -1  # a human decided: the balance shows it, negative


def test_the_demo_company_keeps_its_prepared_mock_hr(tmp_path):
    acc, reg = rg.open_platform(str(tmp_path), [])
    d = reg.engine_for(rg.DEMO_ORG_ID)
    assert d.hr.records_balances is False and not hasattr(d, "ledger") and d.hr.label == "the HR system"
    rid = d.ingest("chat", "e01", "I need Friday and Monday off, family function.")["request_ids"][0]
    assert "leave_check" not in d.req(rid)["details"] and d.req(rid)["details"]["balance"]  # the demo's own figures


def test_two_companies_have_separate_ledgers(tmp_path):
    acc, reg = rg.open_platform(str(tmp_path), ["c1", "c2"])
    o1, u1 = acc.register_company("One", "A", "a@one.test", "a-long-password", "c1")
    o2, u2 = acc.register_company("Two", "B", "b@two.test", "a-long-password", "c2")
    e1, e2 = reg.engine_for(o1["id"]), reg.engine_for(o2["id"])
    e1.ledger.adjust(u1["id"], "casual", 5, "extra", u1["id"])
    e1.ledger.set_yearly("casual", 20)
    assert e1.ledger.balance(u1["id"], "casual") == 17 and e2.ledger.balance(u2["id"], "casual") == 12 and e2.ledger.types()[0]["yearly_days"] == 12
    assert e2.s.all("SELECT * FROM leave_ledger WHERE person_id=?", (u1["id"],)) == []


def test_the_approver_message_has_no_empty_brackets_when_a_person_has_no_department(company):
    e, owner, boss, ann = company
    rid = e.ingest("chat", ann["id"], "I need Friday and Monday off, family function.")["request_ids"][0]
    body = e.s.one("SELECT body FROM outbox WHERE request_id=? AND kind='approval_request' AND channel='chat'", (rid,))["body"]
    assert "from Ann Employee\n" in body and "()" not in body
