"""Notification categories, preferences and how they shape delivery. Fake transport; no network."""
import ast
import os
import sqlite3

import pytest

from app import delivery as dl, notifications as nt
from app.db import Store
from tests.test_delivery import env, live, rows  # noqa: F401 - fixtures are used by name

APP = os.path.join(os.path.dirname(__file__), "..", "app")


def notify_kinds_in_source():
    """Every message kind any code passes to _notify, read from the source so a new kind cannot be forgotten."""
    found = set()
    for name in ("engine.py", "delivery.py", "slack_decisions.py", "chaos.py", "main.py", "setup.py"):
        tree = ast.parse(open(os.path.join(APP, name)).read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_notify":
                arg = n.args[3] if len(n.args) > 3 else next((k.value for k in n.keywords if k.arg == "kind"), None)
                if arg is not None:  # a plain string, or a choice between two ("approved" if x else "assigned")
                    found.update(c.value for c in ast.walk(arg) if isinstance(c, ast.Constant) and isinstance(c.value, str))
    return found


def test_every_kind_the_code_can_write_is_a_notification_or_deliberately_not():
    found = notify_kinds_in_source()
    assert len(found) >= 20  # the scan really found them
    unknown = found - set(nt.KIND_CATEGORY) - nt.EXCLUDED
    assert not unknown, f"classify these kinds in app/notifications.py: {sorted(unknown)}"
    assert set(nt.KIND_CATEGORY) <= found, f"kinds that nothing writes any more: {sorted(set(nt.KIND_CATEGORY) - found)}"
    assert nt.EXCLUDED <= found
    assert (dl.SLACK_KINDS | dl.EMAIL_KINDS) <= set(nt.KIND_CATEGORY) | nt.EXCLUDED  # nothing is delivered that the list cannot show or deliberately leave out


def test_a_kind_belongs_to_exactly_one_category_and_the_excluded_kinds_to_none():
    kinds = [k for _, _, ks in nt.CATEGORIES for k in ks]
    assert len(kinds) == len(set(kinds)) and not set(kinds) & nt.EXCLUDED
    assert [c for c, _, _ in nt.CATEGORIES] == ["decide", "nudge", "mine", "task", "system"]
    assert nt.category_of("approval_request") == "decide" and nt.category_of("escalated") == "nudge" and nt.category_of("reply") is None and nt.category_of("nonsense") is None


# ---------------- preferences ----------------
@pytest.fixture
def st():
    return Store()


def test_everything_is_on_until_a_person_turns_something_off_and_people_are_separate(st):
    assert all(v == {"slack": True, "email": True} for v in nt.get_prefs(st, "u1").values())
    out = nt.set_prefs(st, "u1", {"mine": {"email": False}, "task": {"slack": False, "email": False}})
    assert out["mine"] == {"slack": True, "email": False} and out["task"] == {"slack": False, "email": False} and out["decide"] == {"slack": True, "email": True}
    assert nt.get_prefs(st, "u1") == out and all(v == {"slack": True, "email": True} for v in nt.get_prefs(st, "u2").values())
    assert nt.set_prefs(st, "u1", {"mine": {"email": True}})["mine"] == {"slack": True, "email": True}  # a partial save keeps the rest


def test_a_decision_can_never_be_made_unreachable(st):
    nt.set_prefs(st, "u1", {"decide": {"email": False}})  # one channel off is fine
    with pytest.raises(nt.PrefError, match="at least one of Slack or email"):
        nt.set_prefs(st, "u1", {"decide": {"slack": False}})
    assert nt.get_prefs(st, "u1")["decide"] == {"slack": True, "email": False}  # the refused save changed nothing
    with pytest.raises(nt.PrefError):
        nt.set_prefs(st, "u2", {"decide": {"slack": False, "email": False}, "mine": {"email": False}})
    assert nt.get_prefs(st, "u2")["mine"] == {"slack": True, "email": True}  # and not even the other half of the same save


@pytest.mark.parametrize("bad", [None, [], "x", {"nope": {"email": False}}, {"mine": "off"}, {"mine": {"sms": False}}, {"mine": {"email": "no"}}, {"mine": {"email": 0}}, {"mine": {"slack": None}}])
def test_malformed_preferences_are_refused(st, bad):
    with pytest.raises(nt.PrefError):
        nt.set_prefs(st, "u1", bad)
    assert not st.all("SELECT * FROM notification_prefs")


def test_allowed_follows_the_choice_per_category_and_channel_and_ignores_non_notifications(st):
    nt.set_prefs(st, "u1", {"mine": {"email": False}, "nudge": {"slack": False}})
    assert not nt.allowed(st, "u1", "approved", "email") and nt.allowed(st, "u1", "approved", "chat")
    assert not nt.allowed(st, "u1", "reminder", "chat") and nt.allowed(st, "u1", "reminder", "email") and nt.allowed(st, "u2", "approved", "email")
    assert nt.allowed(st, "u1", "reply", "email") and nt.allowed(st, "u1", "decision_ack", "chat")  # not governed: they are not notifications


# ---------------- in delivery ----------------
def test_a_copy_a_person_turned_off_is_written_skipped_with_the_reason_in_live_but_ignored_in_rehearsal(live):
    e, d, fake = live
    nt.set_prefs(e.s, "e06", {"decide": {"email": False}})
    d.set_mode("live", "e10")
    e.ingest("chat", "e01", "Need a Claude seat for my research project")  # waits for Imran Khan (e06)
    by = {r["channel"]: r for r in rows(e, kind="approval_request")}
    assert by["chat"]["delivery_status"] == "pending" and by["email"]["delivery_status"] == "skipped" and by["email"]["delivery_note"] == "turned off in this person's notification settings"
    d.deliver_pending()
    assert [c[1]["inputData"]["to"] for c in fake.calls if c[0] == "gmail"] == [] and any(c[0] == "slack" for c in fake.calls)
    d.set_mode("rehearsal", "e10")
    e.ingest("chat", "e02", "Need a Figma seat for design")
    again = {r["channel"]: r["delivery_status"] for r in rows(e, kind="approval_request") if r["id"] > by["email"]["id"]}
    assert again == {"chat": "pending", "email": "pending"}  # rehearsal ignores preferences: the admin sees everything


def test_the_row_remembers_who_was_really_messaged(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    d.deliver_pending()
    sent = {r["channel"]: r["sent_to"] for r in rows(e, kind="approval_request")}
    assert sent == {"chat": "e10", "email": "e10"}  # rehearsal: the rehearsing admin, not the approver it is for
    assert rows(e, kind="approval_request")[0]["to_person"] == "e06" and all(r["sent_to"] is None for r in rows(e) if r["delivery_status"] == "skipped")
    d.set_mode("live", "e10")
    e.ingest("chat", "e02", "Need a Figma seat for design")
    d.deliver_pending()
    assert {r["sent_to"] for r in rows(e, kind="approval_request") if r["delivery_status"] == "delivered" and r["id"] > 4} == {"e06"}


def test_a_database_made_before_notifications_gets_the_new_columns_and_index(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, channel TEXT, to_person TEXT, subject TEXT, body TEXT, kind TEXT, idempotency_key TEXT UNIQUE, status TEXT DEFAULT 'sent', created_at REAL)")
    c.execute("INSERT INTO outbox(request_id,channel,to_person,subject,body,kind,idempotency_key,created_at) VALUES(1,'chat','e01','s','b','approved','k',0)")
    c.commit()
    c.close()
    s = Store(path)
    cols = {r["name"] for r in s.all("PRAGMA table_info(outbox)")}
    assert {"read_at", "sent_to", "from_person", "delivery_status"} <= cols
    assert s.one("SELECT read_at, sent_to FROM outbox")["read_at"] is None
    assert "outbox_person" in [r["name"] for r in s.all("PRAGMA index_list(outbox)")]
