"""A person's notification list, read marks and preferences through the HTTP API. Own data only, every filter, paging, audited, isolated. No network."""
import json
import time

import pytest

from app import notifications as nt
from tests import apiutil
from tests.apiutil import Browser


@pytest.fixture
def main(tmp_path, monkeypatch):
    return apiutil.load_app(monkeypatch, tmp_path)


@pytest.fixture
def team(main):
    owner = apiutil.company(main)
    boss = apiutil.add_person(owner, "boss@acme.test", "Bo Boss")
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee", manager_id=boss.uid)
    bob = apiutil.add_person(owner, "bob@acme.test", "Bob Employee", manager_id=boss.uid)
    return owner, boss, ann, bob


def ask(b, text, eid=None):
    return b.post("/api/ingest", {"text": text, "source_event_id": eid or text[:20]}).json()["request_ids"][0]


def items(b, **q):
    r = b.get("/api/notifications", params=q)
    assert r.status_code == 200, r.text
    return r.json()


def ids(b, **q):
    return [i["id"] for i in items(b, **q)["items"]]


def put(owner, rid, person, kind, text="Something happened.", n=0, at=None):
    """Write a message straight into the outbox, so a test can have exactly the mix it needs."""
    e = owner.engine
    e._notify(rid, person, text, kind, n=n)
    row = e.s.one("SELECT id FROM outbox WHERE idempotency_key=?", (f"{rid}:{kind}:{person}:{n}",))
    if at is not None:
        e.s.q("UPDATE outbox SET created_at=? WHERE id=? OR idempotency_key=?", (at, row["id"], f"{rid}:{kind}:{person}:{n}:email"))
    return row["id"]


# ---------------- who sees what ----------------
def test_a_person_sees_only_their_own_notifications_and_not_chat_replies(team):
    owner, boss, ann, bob = team
    rid = ask(ann, "I need Friday and Monday off, family function.")
    seat = ask(ann, "Need a Claude seat for my research project")
    ask(ann, "What is the work from home policy?")  # an answer: stays in Ask
    j = items(boss)
    assert [(i["request_id"], i["kind"], i["category"], i["needs_action"], i["read"]) for i in j["items"]] == [(rid, "approval_request", "decide", True, False)] and j["unread"] == 1
    assert j["items"][0]["subject"] == f"Approval needed: Leave #{rid}" and j["items"][0]["type_code"] == "leave" and j["items"][0]["type_name"] == "Leave"
    assert [i["request_id"] for i in items(owner)["items"]] == [seat]  # the seat goes to the admin, not the manager
    assert items(ann)["items"] == [] and items(bob)["items"] == [] and items(ann)["unread"] == 0  # "request received" and the policy answer are replies
    kinds = {r["kind"] for r in owner.engine.s.all("SELECT kind FROM outbox")}
    assert {"submitted", "answer"} <= kinds  # they exist in the outbox; they are just not notifications


def test_the_list_can_never_be_pointed_at_someone_else(team):
    owner, boss, ann, bob = team
    ask(ann, "I need Friday and Monday off, family function.")
    for q in ({"person": boss.uid}, {"person_id": boss.uid}, {"to": boss.uid}, {"user": boss.uid}):
        assert items(ann, **q)["items"] == []  # unknown parameters are ignored, never honoured
    assert items(owner)["items"] == [] and items(bob)["items"] == []  # even an admin sees only their own
    assert Browser(owner.main).get("/api/notifications").status_code == 401


def test_nothing_crosses_companies(main, team):
    owner, boss, ann, bob = team
    ask(ann, "I need Friday and Monday off, family function.")
    other = apiutil.company(main, "o@two.test", "code-2", "Two Ltd")
    ann2 = apiutil.add_person(other, "ann@two.test", "Ann Two")
    ask(ann2, "I need a second monitor for my desk.")
    assert json.dumps(items(other)) .count("Leave") == 0 and len(items(other)["items"]) == 1 and items(other)["items"][0]["type_code"] == "equipment"
    assert len(items(boss)["items"]) == 1 and items(boss)["items"][0]["type_code"] == "leave"
    assert other.post("/api/notifications/read", {"all": True}).json()["unread"] == 0 and items(boss)["unread"] == 1  # reading in one company does nothing in another


# ---------------- unread and read ----------------
def test_unread_is_on_the_clock_and_opening_marks_it_read_and_it_is_safe_to_repeat(team):
    owner, boss, ann, bob = team
    assert boss.get("/api/clock").json()["unread"] == 0
    a = ask(ann, "I need Friday and Monday off, family function.")
    b_ = ask(ann, "Need a Claude seat for my research project")
    assert boss.get("/api/clock").json()["unread"] == 1 and owner.get("/api/clock").json()["unread"] == 1 and ann.get("/api/clock").json()["unread"] == 0
    first = ids(boss)[0]
    r = boss.post("/api/notifications/read", {"ids": [first]})
    assert r.json() == {"changed": 1, "unread": 0} and items(boss)["items"][0]["read"] is True
    assert boss.post("/api/notifications/read", {"ids": [first]}).json() == {"changed": 0, "unread": 0}  # again: nothing more to change
    assert boss.get("/api/clock").json()["unread"] == 0 and owner.get("/api/clock").json()["unread"] == 1
    assert (a, b_) == (1, 2)


def test_nobody_can_mark_someone_elses_notification_read(team):
    owner, boss, ann, bob = team
    ask(ann, "I need Friday and Monday off, family function.")
    mine = ids(boss)
    for who in (ann, bob, owner):
        assert who.post("/api/notifications/read", {"ids": mine}).json()["changed"] == 0
        assert who.post("/api/notifications/read", {"request_id": 1}).json()["changed"] == 0
    assert items(boss)["unread"] == 1 and items(boss)["items"][0]["read"] is False
    assert boss.post("/api/notifications/read", {"request_id": 1}).json() == {"changed": 1, "unread": 0}


def test_read_by_request_marks_everything_about_that_request_and_all_marks_the_rest(team):
    owner, boss, ann, bob = team
    r1, r2 = ask(ann, "I need Friday and Monday off, family function."), ask(bob, "I need Tuesday off, personal errand.")
    put(owner, r1, boss.uid, "reminder")
    put(owner, r1, boss.uid, "escalation_1")
    assert items(boss)["unread"] == 4
    assert boss.post("/api/notifications/read", {"request_id": r1}).json() == {"changed": 3, "unread": 1}
    assert [(i["request_id"], i["read"]) for i in items(boss)["items"] if i["request_id"] == r2] == [(r2, False)]
    assert boss.post("/api/notifications/read", {"all": True}).json() == {"changed": 1, "unread": 0}
    assert boss.post("/api/notifications/read", {"all": True}).json()["changed"] == 0
    assert boss.post("/api/notifications/read", {}).json()["changed"] == 0  # naming nothing marks nothing
    assert boss.post("/api/notifications/read", {"ids": "x"}).status_code == 422


def test_reading_does_not_touch_messages_that_are_not_notifications(team):
    owner, boss, ann, bob = team
    ask(ann, "What is the work from home policy?")
    assert ann.post("/api/notifications/read", {"all": True}).json()["changed"] == 0
    assert owner.engine.s.one("SELECT read_at FROM outbox WHERE kind='answer'")["read_at"] is None


# ---------------- filters ----------------
@pytest.fixture
def mixed(team):
    """Six notifications for the manager: different categories, types and ages, some read, with known delivery states."""
    owner, boss, ann, bob = team
    t0 = time.time() - 6 * 86400
    leave, seat = ask(ann, "I need Friday and Monday off, family function."), ask(bob, "Need a Claude seat for my research project")
    n = [put(owner, leave, boss.uid, "approval_request", "Please decide the leave of Ann (100% sure)", n=1, at=t0),
         put(owner, seat, boss.uid, "approval_request", "A seat for Bob, the research one", n=1, at=t0 + 86400),
         put(owner, leave, boss.uid, "reminder", "It has been waiting for a day", at=t0 + 2 * 86400),
         put(owner, seat, boss.uid, "escalation_1", "Waited three days", at=t0 + 3 * 86400),
         put(owner, leave, boss.uid, "approved", "Your request was approved", at=t0 + 4 * 86400),
         put(owner, seat, boss.uid, "task", "Task: give the seat", at=t0 + 5 * 86400)]
    s = owner.engine.s
    s.q("DELETE FROM outbox WHERE to_person=? AND kind='approval_request' AND (idempotency_key LIKE '%:0' OR idempotency_key LIKE '%:0:email')", (boss.uid,))  # the real ones: the six below are exactly what the list has
    s.q("UPDATE outbox SET read_at=? WHERE id IN (?,?)", (t0 + 100, n[0], n[2]))
    s.q("UPDATE outbox SET delivery_status='delivered', delivery_note='' WHERE id=?", (n[4],))
    s.q("UPDATE outbox SET delivery_status='delivered' WHERE idempotency_key=?", (f"{seat}:task:{boss.uid}:0:email",))
    s.q("UPDATE outbox SET delivery_status='failed', delivery_error='boom' WHERE idempotency_key=?", (f"{leave}:reminder:{boss.uid}:0:email",))
    s.q("UPDATE requests SET status='APPROVED' WHERE id=?", (leave,))
    return owner, boss, leave, seat, n, t0


def test_each_filter_on_its_own_and_combined(mixed):
    owner, boss, leave, seat, n, t0 = mixed
    every = ids(boss)
    assert every == sorted(n, reverse=True)  # all six, newest first
    assert ids(boss, unread=True) == [n[5], n[4], n[3], n[1]]
    assert ids(boss, category="nudge") == [n[3], n[2]] and ids(boss, category="task") == [n[5]]
    assert ids(boss, category="mine") == [n[4]] and ids(boss, category="system") == []
    assert ids(boss, type="leave") == [n[4], n[2], n[0]] and ids(boss, type="software_seat") == [n[5], n[3], n[1]]
    assert ids(boss, status="APPROVED") == [n[4], n[2], n[0]]
    assert ids(boss, channel="slack") == [n[4]]  # only Slack copies that really arrived
    assert ids(boss, channel="email") == [n[5]]  # a failed email copy does not count as delivered
    assert ids(boss, since=t0 + 3 * 86400 - 1, until=t0 + 5 * 86400) == [n[4], n[3]]  # until is exclusive, since inclusive
    assert ids(boss, category="nudge", type="leave", unread=False) == [n[2]]  # combined: a reminder about a leave request
    assert ids(boss, category="nudge", unread=True) == [n[3]] and ids(boss, category="decide", status="APPROVED", unread=True) == []
    assert ids(boss, q="seat", category="decide") == [n[1]]


def test_search_is_literal_and_cannot_be_used_to_inject_or_match_everything(mixed):
    owner, boss, leave, seat, n, t0 = mixed
    assert ids(boss, q="research") == [n[1]] and ids(boss, q="RESEARCH") == [n[1]]  # either case, in the text
    assert ids(boss, q="is waiting") == [n[2]]  # that phrase is only in the subject of the reminder, not in its text
    assert ids(boss, q="100%") == [n[0]] and ids(boss, q="%") == [n[0]]  # a percent sign is just a character
    assert ids(boss, q="_") == [] and ids(boss, q="\\") == []  # nor is an underscore or a backslash a wildcard
    for evil in ("' OR 1=1 --", "x') UNION SELECT * FROM users --", '"; DROP TABLE outbox; --', "%' AND '%'='"):
        assert ids(boss, q=evil) == []
    assert owner.engine.s.one("SELECT COUNT(*) c FROM outbox")["c"] >= 6  # still there
    assert ids(boss, q="   ") == ids(boss)  # blank means no search
    assert len(items(boss, q="a" * 5000)["items"]) == 0  # an absurd search is cut short, not an error


def test_bad_filters_are_refused_with_a_clear_message(team):
    owner, boss, ann, bob = team
    assert boss.get("/api/notifications", params={"category": "nope"}).status_code == 422
    assert boss.get("/api/notifications", params={"channel": "pigeon"}).status_code == 422
    assert boss.get("/api/notifications", params={"since": "yesterday"}).status_code == 422
    assert boss.get("/api/notifications", params={"before": "x"}).status_code == 422
    assert items(boss, type="no_such_type")["items"] == [] and items(boss, status="NOPE")["items"] == []  # unknown but harmless values simply match nothing


# ---------------- paging ----------------
def test_paging_walks_everything_once_even_while_new_messages_arrive(team):
    owner, boss, ann, bob = team
    rid = ask(ann, "I need Friday and Monday off, family function.")
    for k in range(1, 13):
        put(owner, rid, boss.uid, "reminder", f"Reminder {k}", n=k)
    total = len(ids(boss, limit=100))
    assert total == 13
    seen, page, calls = [], items(boss, limit=5), 0
    while True:
        seen += [i["id"] for i in page["items"]]
        calls += 1
        if calls == 1:
            put(owner, rid, boss.uid, "escalation_1", "Arrived while paging")  # newer than everything: must not shift the later pages
        if not page["next_before"]:
            break
        page = items(boss, limit=5, before=page["next_before"])
    assert calls == 3 and len(seen) == total == len(set(seen)) and seen == sorted(seen, reverse=True)
    assert items(boss, limit=5)["next_before"] is not None and items(boss, limit=100)["next_before"] is None
    assert len(items(boss, limit=0)["items"]) == 1 and len(items(boss, limit=10 ** 6)["items"]) == 14  # limits are clamped to something sensible


def test_the_unread_total_counts_everything_not_just_the_page_or_the_filter(team):
    owner, boss, ann, bob = team
    rid = ask(ann, "I need Friday and Monday off, family function.")
    for k in range(1, 8):
        put(owner, rid, boss.uid, "reminder", n=k)
    j = items(boss, limit=3, category="decide")
    assert len(j["items"]) == 1 and j["unread"] == 8


# ---------------- what each entry says about delivery ----------------
def test_an_entry_says_how_its_slack_and_email_copies_fared(team):
    owner, boss, ann, bob = team
    rid = ask(ann, "I need Friday and Monday off, family function.")
    s = owner.engine.s
    row = s.one("SELECT id FROM outbox WHERE kind='approval_request' AND channel='chat'")
    mail = s.one("SELECT id FROM outbox WHERE kind='approval_request' AND channel='email'")
    e = items(boss)["items"][0]
    assert e["slack"]["state"] == "skipped" and e["email"]["state"] == "skipped" and e["email"]["note"]  # nothing connected yet: said honestly
    s.q("UPDATE outbox SET delivery_status='delivered', sent_to=? WHERE id=?", (owner.uid, row["id"]))  # rehearsal: the admin got it, not the approver
    s.q("UPDATE outbox SET delivery_status='failed', delivery_note='Gmail refused it' WHERE id=?", (mail["id"],))
    e = items(boss)["items"][0]
    assert e["slack"]["state"] == "rehearsal" and e["email"]["state"] == "failed" and len(items(boss)["items"]) == 1  # the email copy is a chip, not a second entry
    s.q("UPDATE outbox SET delivery_status='delivered', sent_to=? WHERE id=?", (boss.uid, row["id"]))
    assert items(boss)["items"][0]["slack"]["state"] == "delivered"
    s.q("DELETE FROM outbox WHERE id=?", (mail["id"],))
    assert items(boss)["items"][0]["email"] is None  # a kind that is not emailed has no email chip
    assert rid == 1


def test_a_request_that_came_in_by_email_still_shows_for_its_approver(team):
    owner, boss, ann, bob = team
    rid = ask(ann, "I need Friday and Monday off, family function.")
    owner.engine.s.q("UPDATE outbox SET channel='email' WHERE kind='approval_request' AND idempotency_key NOT LIKE '%:email'")  # such rows are written on the email channel
    assert [i["request_id"] for i in items(boss)["items"]] == [rid] and items(boss)["unread"] == 1


def test_needs_action_is_true_only_while_there_is_still_something_to_do(team):
    owner, boss, ann, bob = team
    rid = ask(ann, "I need Friday and Monday off, family function.")
    assert items(boss)["items"][0]["needs_action"] is True
    assert boss.post(f"/api/requests/{rid}/approve").status_code == 200
    assert items(boss)["items"][0]["needs_action"] is False and items(boss)["items"][0]["request_status"] != "PENDING_APPROVAL"
    assert [i["kind"] for i in items(ann)["items"]][-1] in ("approved", "fulfilled", "assigned") and items(ann)["unread"] >= 1


# ---------------- preferences ----------------
def test_preferences_start_all_on_and_describe_each_category(team):
    owner, boss, ann, bob = team
    p = boss.get("/api/notifications/preferences").json()
    assert [c["code"] for c in p["categories"]] == ["decide", "nudge", "mine", "task", "system"] and p["demo"] is False and p["mode"] == "rehearsal"
    assert all(c["slack"] and c["email"] and c["description"] and c["title"] for c in p["categories"])
    assert [c["code"] for c in p["categories"] if c["protected"]] == ["decide"]
    assert items(boss)["categories"] == [{"code": c, "title": t} for c, t, _ in nt.CATEGORIES]


def test_a_person_saves_their_own_preferences_and_it_is_audited_without_touching_anyone_else(team):
    owner, boss, ann, bob = team
    r = ann.post("/api/notifications/preferences", {"prefs": {"mine": {"email": False}, "nudge": {"slack": False, "email": False}}})
    assert r.status_code == 200
    by = {c["code"]: (c["slack"], c["email"]) for c in r.json()["categories"]}
    assert by["mine"] == (True, False) and by["nudge"] == (False, False) and by["decide"] == (True, True)
    assert {c["code"]: (c["slack"], c["email"]) for c in ann.get("/api/notifications/preferences").json()["categories"]} == by
    assert all(c["slack"] and c["email"] for c in boss.get("/api/notifications/preferences").json()["categories"])  # someone else's are untouched
    ev = [json.loads(r["data_json"]) for r in owner.engine.s.all("SELECT data_json FROM events WHERE type='notification_prefs_changed'")]
    assert ev == [{"categories": ["mine", "nudge"]}]
    ann.post("/api/notifications/preferences", {"prefs": {"mine": {"email": False}}})  # nothing changed: nothing to record
    assert len(owner.engine.s.all("SELECT 1 FROM events WHERE type='notification_prefs_changed'")) == 1


def test_a_save_that_would_hide_a_decision_or_is_malformed_is_refused_and_changes_nothing(team):
    owner, boss, ann, bob = team
    r = ann.post("/api/notifications/preferences", {"prefs": {"decide": {"slack": False, "email": False}}})
    assert r.status_code == 422 and "at least one" in r.json()["detail"]
    assert ann.post("/api/notifications/preferences", {"prefs": {"decide": {"slack": False}, "mine": {"email": False}}}).status_code == 200
    r = ann.post("/api/notifications/preferences", {"prefs": {"decide": {"email": False}}})  # slack is already off: this would leave none
    assert r.status_code == 422
    for bad in ({"nope": {"slack": True}}, {"mine": {"sms": True}}, {"mine": {"slack": "yes"}}, {"mine": "off"}, {"mine": {"slack": None}}):
        assert ann.post("/api/notifications/preferences", {"prefs": bad}).status_code == 422, bad
    assert ann.post("/api/notifications/preferences", {"prefs": ["decide"]}).status_code == 422
    by = {c["code"]: (c["slack"], c["email"]) for c in ann.get("/api/notifications/preferences").json()["categories"]}
    assert by["decide"] == (False, True) and by["mine"] == (True, False)  # exactly what the one good save left
    assert Browser(owner.main).post("/api/notifications/preferences", {"prefs": {}}).status_code in (401, 403)


def test_preferences_need_the_csrf_token(team):
    owner, boss, ann, bob = team
    good, ann.csrf = ann.csrf, None
    assert ann.post("/api/notifications/preferences", {"prefs": {"mine": {"email": False}}}).status_code == 403
    assert ann.post("/api/notifications/read", {"all": True}).status_code == 403
    ann.csrf = good
    assert all(c["email"] for c in ann.get("/api/notifications/preferences").json()["categories"])


# ---------------- the demo company ----------------
def test_the_demo_has_notifications_for_whoever_you_act_as_but_preferences_are_read_only(main):
    d = Browser(main)
    assert d.demo("e01").status_code == 200
    d.post("/api/ingest", {"text": "Need a Claude seat for my research project"})
    d.act_as("e06")  # the approver
    j = items(d)
    assert j["unread"] >= 1 and j["items"][0]["kind"] == "approval_request" and d.get("/api/clock").json()["unread"] == j["unread"]
    assert d.post("/api/notifications/read", {"all": True}).json()["unread"] == 0
    p = d.get("/api/notifications/preferences").json()
    assert p["demo"] is True and all(c["slack"] and c["email"] for c in p["categories"])
    assert d.post("/api/notifications/preferences", {"prefs": {"mine": {"email": False}}}).status_code == 403
    d.act_as("e01")
    assert items(d)["unread"] == 0 and items(d)["items"] == []  # the requester has nothing but replies


def test_a_copy_the_person_switched_off_says_so_in_its_own_word(team):
    owner, boss, ann, bob = team
    ask(ann, "I need Friday and Monday off, family function.")
    owner.engine.s.q("UPDATE outbox SET delivery_note=? WHERE kind='approval_request' AND channel='email'", (nt.OFF_NOTE,))
    e = items(boss)["items"][0]
    assert e["email"]["state"] == "off" and e["slack"]["state"] == "skipped"  # off is only for what the person chose; "nothing connected" stays "skipped"
