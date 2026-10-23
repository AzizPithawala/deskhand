"""Dates in leave requests: a weekday that is today means today, dates are always in date order, and the approver is told when the leave includes today or is already past.
Every test names its own "today", so none depends on the weekday it runs on."""
from datetime import date, datetime, timedelta

import pytest

from app import classifier
from app.engine import Engine, details_view
from tests import apiutil

WEEK = [date(2026, 10, d) for d in range(19, 26)]  # Monday 19 Oct to Sunday 25 Oct 2026


def dates(text, today):
    return classifier.parse_leave(text, today)["dates"]


@pytest.mark.parametrize("today,expected", [
    (WEEK[0], ["2026-10-19", "2026-10-23"]),   # Monday: Monday is today
    (WEEK[1], ["2026-10-23", "2026-10-26"]),
    (WEEK[2], ["2026-10-23", "2026-10-26"]),
    (WEEK[3], ["2026-10-23", "2026-10-26"]),
    (WEEK[4], ["2026-10-23", "2026-10-26"]),   # Friday: Friday is today (this used to be next week)
    (WEEK[5], ["2026-10-26", "2026-10-30"]),   # Saturday: the coming Monday and the coming Friday
    (WEEK[6], ["2026-10-26", "2026-10-30"]),
])
def test_friday_and_monday_in_either_order_is_the_same_days_in_date_order_on_every_weekday(today, expected):
    assert dates("I need Friday and Monday off, family function.", today) == expected
    assert dates("I need Monday and Friday off, family function.", today) == expected


def test_a_weekday_that_is_today_means_today_and_next_means_after_today():
    friday, wednesday = date(2026, 10, 23), date(2026, 10, 21)
    assert dates("I need Friday off", friday) == ["2026-10-23"]
    assert dates("I need next Friday off", friday) == ["2026-10-30"]
    assert dates("I need Friday off", wednesday) == dates("I need next Friday off", wednesday) == ["2026-10-23"]
    assert dates("I need Friday off", date(2026, 10, 24)) == ["2026-10-30"]  # said on Saturday: the coming Friday, never the one just gone


def test_the_same_weekday_twice_is_one_day_and_today_or_tomorrow_sort_in_with_weekdays():
    friday = date(2026, 10, 23)
    assert dates("Friday, Friday please", friday) == ["2026-10-23"]
    assert dates("I need Monday and today off", friday) == ["2026-10-23", "2026-10-26"]
    assert dates("I need Monday and tomorrow off", friday) == ["2026-10-24", "2026-10-26"]


def test_n_days_starting_a_weekday_starts_today_when_it_is_that_weekday_and_stays_a_range():
    r = classifier.parse_leave("I need 3 days starting Monday", date(2026, 10, 19))
    assert r["dates"] == ["2026-10-19", "2026-10-21"] and r["date_range"] is True
    r = classifier.parse_leave("I need 3 days starting Monday", date(2026, 10, 20))
    assert r["dates"] == ["2026-10-26", "2026-10-28"] and r["date_range"] is True


def pin(monkeypatch, day):
    monkeypatch.setattr(Engine, "today", lambda self: day)


def test_a_leave_that_includes_today_says_so_to_the_approver_and_the_requester_sees_the_days(e, monkeypatch):
    pin(monkeypatch, date(2026, 10, 23))  # a Friday
    rid = e.ingest("chat", "e01", "I need Friday and Monday off, family function.")["request_ids"][0]
    r = e.req(rid)
    assert r["details"]["dates"] == ["2026-10-23", "2026-10-26"] and ("Note", "Includes today") in r["details_view"]
    assert "Casual leave: Fri 23 Oct, Mon 26 Oct" in [x["body"] for x in e.s.all("SELECT body FROM outbox WHERE to_person='e01' ORDER BY id")][0]
    assert "Note: Includes today" in [x["body"] for x in e.s.all("SELECT body FROM outbox WHERE kind='approval_request'")][0]


def test_a_leave_that_does_not_include_today_carries_no_note(e, monkeypatch):
    pin(monkeypatch, date(2026, 10, 22))  # the Thursday before
    r = e.req(e.ingest("chat", "e01", "I need Friday and Monday off, family function.")["request_ids"][0])
    assert r["details"]["dates"] == ["2026-10-23", "2026-10-26"] and not [x for x in r["details_view"] if x[0] == "Note"]


def test_dates_already_in_the_past_are_flagged_for_the_approver_and_never_blocked(e, monkeypatch):
    pin(monkeypatch, date(2026, 10, 22))
    r = e.req(e.ingest("chat", "e01", "I need leave 12 Oct to 14 Oct, family function")["request_ids"][0])
    assert ("Note", "Some dates are in the past") in r["details_view"] and r["status"] == "PENDING_APPROVAL"  # the approver decides


def test_a_range_that_spans_today_includes_today():
    r = {"details": {"dates": ["2026-10-21", "2026-10-26"], "date_range": True}, "id": 1}

    class Stub:
        def today(self):
            return date(2026, 10, 23)

        class s:
            @staticmethod
            def q(*a):
                pass

    Engine._date_notes(Stub(), r)
    assert r["details"]["includes_today"] is True and r["details"]["has_past_dates"] is True
    assert ("Note", "Includes today") in details_view(r["details"]) and ("Note", "Some dates are in the past") in details_view(r["details"])


def test_in_a_company_a_friday_request_made_on_a_friday_reaches_the_manager_with_the_note_and_tomorrow_on_a_friday_asks_again(tmp_path, monkeypatch):
    main = apiutil.load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(Engine, "today", lambda self: date(2026, 10, 23))  # a Friday
    owner = apiutil.company(main)
    boss = apiutil.add_person(owner, "boss@acme.test", "Bo Boss")
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee", manager_id=boss.uid)
    rid = ann.post("/api/ingest", {"text": "I need Friday and Monday off, family function.", "source_event_id": "d1"}).json()["request_ids"][0]
    req = owner.engine.req(rid)
    assert req["status"] == "PENDING_APPROVAL" and req["chain"] == [boss.uid] and req["details"]["dates"] == ["2026-10-23", "2026-10-26"]
    assert ("Note", "Includes today") in req["details_view"] and req["details"]["leave_check"]["days"] == 2
    tomorrow = ann.post("/api/ingest", {"text": "I need leave tomorrow, family function", "source_event_id": "d2"}).json()["request_ids"][0]
    asked = owner.engine.req(tomorrow)
    assert asked["status"] == "NEEDS_INFO" and asked["chain"] == []  # Saturday is not a working day: Deskhand asks which days, as before
