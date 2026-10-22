"""Slack Approve / Reject buttons: the one-time token, who may click, replays, races, and the fallbacks. Fake transport; no network."""
import json
import threading
import time

import pytest

from app import delivery as dl, slack_decisions as sd, viasocket_adapter as vs
from app.engine import Engine
from tests.test_delivery import SCRIPT_F, SCRIPT_S, Fake, env, make_delivery, rows, setup_apps  # noqa: F401 - env is used by name

IDS = {"e01": "U0PRIYA01", "e02": "U0AMAN001", "e04": "U0RAHUL01", "e06": "U0IMRAN01", "e10": "U0ARJUN01"}


def realistic_slack_ids(e):
    for pid, sid in IDS.items():
        e.d.emp(pid)["slack_id"] = sid


@pytest.fixture
def eng(env):
    e = Engine(api_key="")
    realistic_slack_ids(e)
    return e


def seat_request(e):
    return e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]  # waits for Imran Khan (e06)


def token_for(e, rid, person="e06", outbox=None):
    return sd.issue(e, "acme-demo", "approval", rid, person, outbox)


def approvals(e, rid):
    return e.s.all("SELECT * FROM events WHERE request_id=? AND type='approved_stage'", (rid,))


def test_the_right_approver_pressing_approve_approves_once_and_a_repeat_does_nothing(eng):
    e = eng
    rid = seat_request(e)
    t = token_for(e, rid)
    assert sd.decide(e, t, "U0IMRAN01", "approve") == {"status": "approved"}
    assert e.req(rid)["status"] == "FULFILLING" and len(approvals(e, rid)) == 1
    assert json.loads(approvals(e, rid)[0]["data_json"])["via"] == "slack" and approvals(e, rid)[0]["actor"] == "e06"  # who and how are in the audit record
    assert [r["kind"] for r in rows(e, to_person="e06") if r["kind"] == "decision_ack"] == ["decision_ack"]
    assert sd.decide(e, t, "U0IMRAN01", "approve") == {"status": "already_used"}  # viaSocket redelivers: nothing happens twice
    assert len(approvals(e, rid)) == 1


def test_forged_unknown_expired_and_tampered_tokens_do_nothing(eng):
    e = eng
    rid = seat_request(e)
    t = token_for(e, rid)
    for bad in ("", "nonsense", "acme-demo.", "acme-demo.AAAA", "other-co." + t.split(".", 1)[1], t[:-1] + ("A" if t[-1] != "A" else "B"), "x" * 500):
        assert sd.decide(e, bad, "U0IMRAN01", "approve") == {"status": "invalid"}, bad
    assert e.req(rid)["status"] == "PENDING_APPROVAL"
    e.s.q("UPDATE slack_decisions SET expires_at=?", (time.time() - 1,))
    assert sd.decide(e, t, "U0IMRAN01", "approve") == {"status": "expired"} and e.req(rid)["status"] == "PENDING_APPROVAL"
    assert sd.org_of("acme-demo.abc") == "acme-demo" and sd.org_of("no-dot") is None and sd.org_of(".x") is None and sd.org_of("../x.y") is None and sd.org_of(None) is None


def test_only_the_hash_of_a_token_is_stored(eng):
    e = eng
    t = token_for(e, seat_request(e))
    assert t.split(".", 1)[1] not in json.dumps(e.s.all("SELECT * FROM slack_decisions")) and len(e.s.all("SELECT * FROM slack_decisions")[0]["token_hash"]) == 64


def test_someone_who_cannot_act_is_refused_and_the_token_stays_usable_for_the_right_person(eng):
    e = eng
    rid = seat_request(e)
    t = token_for(e, rid)
    assert sd.decide(e, t, "U0AMAN001", "approve") == {"status": "not_allowed"}  # Aman is a colleague, not the approver
    assert sd.decide(e, t, "U0RAHUL01", "approve") == {"status": "not_allowed"}  # the requester's manager is not this request's approver
    assert e.req(rid)["status"] == "PENDING_APPROVAL" and "slack_click_refused" in [x["type"] for x in e.s.all("SELECT type FROM events")]
    assert sd.decide(e, t, "U0IMRAN01", "approve") == {"status": "approved"}


def test_an_admin_may_decide_from_slack_like_in_the_web_page(eng):
    e = eng
    rid = seat_request(e)
    assert sd.decide(e, token_for(e, rid), "U0ARJUN01", "approve") == {"status": "approved"} and approvals(e, rid)[0]["actor"] == "e10"


def test_only_a_slack_id_identifies_the_clicker_never_a_directory_id_or_email(eng):
    e = eng
    rid = seat_request(e)
    t = token_for(e, rid)
    for who in ("e06", "imran.khan@acme-demo.example", "U0NOBODY01", "", "<@U0IMRAN01>", "u0imran01", "U0IMRAN01; approve", "U0IMRAN0", "U0IMRAN011"):
        assert sd.decide(e, t, who, "approve") == {"status": "unknown_person"}, who
    assert e.req(rid)["status"] == "PENDING_APPROVAL"
    assert sd.decide(e, t, " U0IMRAN01 ", "approve") == {"status": "approved"}  # the genuine id, whitespace aside


def test_a_deactivated_person_cannot_click(eng):
    e = eng
    rid = seat_request(e)
    e.d.emp("e06")["active"] = False
    assert sd.decide(e, token_for(e, rid), "U0IMRAN01", "approve") == {"status": "unknown_person"} and e.req(rid)["status"] == "PENDING_APPROVAL"


def test_reject_in_slack_sends_the_person_to_the_web_page_for_the_reason(eng):
    e = eng
    rid = seat_request(e)
    t = token_for(e, rid)
    assert sd.decide(e, t, "U0IMRAN01", "reject") == {"status": "needs_reason"}
    assert e.req(rid)["status"] == "PENDING_APPROVAL" and e.req(rid)["rejection_reason"] is None  # a reason is mandatory: nothing was rejected
    link = [r for r in rows(e, kind="reject_link")]
    assert link and link[0]["to_person"] == "e06" and "reason" in link[0]["body"]
    assert sd.decide(e, t, "U0IMRAN01", "reject") == {"status": "already_used"}


def test_a_click_after_the_web_page_decided_changes_nothing(eng):
    e = eng
    rid = seat_request(e)
    t = token_for(e, rid)
    e.approve(rid, "e06")
    before = len(e.s.all("SELECT * FROM events"))
    assert sd.decide(e, t, "U0IMRAN01", "approve") == {"status": "already_decided"} and len(e.s.all("SELECT * FROM events")) == before and len(approvals(e, rid)) == 1
    assert sd.decide(e, t, "U0IMRAN01", "approve") == {"status": "already_used"}


def test_an_unknown_decision_word_is_refused_and_keeps_the_token(eng):
    e = eng
    rid = seat_request(e)
    t = token_for(e, rid)
    for word in ("", "yes", "APPROVE; DROP", "cancel"):
        assert sd.decide(e, t, "U0IMRAN01", word)["status"] in ("bad_decision",), word
    assert sd.decide(e, t, "U0IMRAN01", "Approve") == {"status": "approved"}  # case does not matter


def test_when_the_buttons_could_not_be_shown_the_person_still_gets_the_plain_message(eng):
    e = eng
    rid = seat_request(e)
    t = token_for(e, rid)
    assert sd.decide(e, t, "", "error") == {"status": "fallback_sent"} and rows(e, kind="approval_fallback")[0]["to_person"] == "e06"
    assert sd.decide(e, t, "", "error") == {"status": "already_used"} and len(rows(e, kind="approval_fallback")) == 1
    assert e.req(rid)["status"] == "PENDING_APPROVAL"


def test_eight_presses_at_the_same_moment_approve_exactly_once(eng):
    e = eng
    rid = seat_request(e)
    t = token_for(e, rid)
    barrier, out = threading.Barrier(8), []

    def press():
        barrier.wait()
        out.append(sd.decide(e, t, "U0IMRAN01", "approve")["status"])

    ts = [threading.Thread(target=press) for _ in range(8)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert out.count("approved") == 1 and out.count("already_used") == 7 and len(approvals(e, rid)) == 1


def test_sending_the_same_message_again_makes_the_earlier_token_useless(eng):
    e = eng
    rid = seat_request(e)
    first = token_for(e, rid, outbox=77)
    second = token_for(e, rid, outbox=77)  # a retry of the same outbox row
    assert sd.decide(e, first, "U0IMRAN01", "approve") == {"status": "already_used"} and e.req(rid)["status"] == "PENDING_APPROVAL"
    assert sd.decide(e, second, "U0IMRAN01", "approve") == {"status": "approved"}


def test_a_test_approval_proves_the_click_without_touching_any_request(eng):
    e = eng
    t = sd.issue(e, "acme-demo", "test", None, "e10")
    assert sd.latest_test(e, "e10") == {"state": "waiting", "since": e.s.all("SELECT created_at FROM slack_decisions")[0]["created_at"]}
    assert sd.decide(e, t, "U0IMRAN01", "approve") == {"status": "not_allowed"}  # only the admin it was sent to
    assert sd.decide(e, t, "U0ARJUN01", "reject") == {"status": "test_ok", "decision": "reject"} and sd.latest_test(e, "e10")["state"] == "received"
    assert not e.s.all("SELECT * FROM requests") and sd.latest_test(e, "e01") == {"state": "none"}


# ---------------- through delivery: who gets buttons and who gets plain text ----------------
FLOW_CALLBACK = "https://deskhand.test/api/hooks/slack-decision"


def live_with_buttons(e, fake, mode="live", flow=True, base="https://deskhand.test"):
    setup_apps(e)
    realistic_slack_ids(e)
    d = e.live = make_delivery(e, fake)
    d.base_url = base
    if flow:
        e.s.q("INSERT INTO connections(tenant,app,auth_id,script_id,settings_json,updated_at) VALUES('acme-demo','slack_flow',NULL,?, '{}', 0)", (SCRIPT_F,))
    d.set_mode(mode, "e10")
    return d


def plain_dms(fake):
    return [c for c in fake.calls if c[0] == "slack"]


def flow_runs(fake):
    return [c for c in fake.calls if c[0] == "flow"]


def test_a_live_approval_goes_out_with_buttons_and_a_token_that_works(env):
    e, fake = Engine(api_key=""), Fake()
    d = live_with_buttons(e, fake)
    rid = seat_request(e)
    d.deliver_pending()
    runs = flow_runs(fake)
    assert len(runs) == 1 and set(runs[0][1]) == {"slack_user", "text", "token"} and runs[0][1]["slack_user"] == "U0IMRAN01" and "inputData" not in runs[0][1]
    assert f"https://deskhand.test/#needs/{rid}" in runs[0][1]["text"] and "Approval needed" in runs[0][1]["text"]
    assert not [c for c in plain_dms(fake) if "Approval needed" in c[1]["inputData"]["content"]]  # not also sent plain
    assert sd.decide(e, runs[0][1]["token"], "U0IMRAN01", "approve") == {"status": "approved"} and e.req(rid)["status"] == "FULFILLING"
    assert e.s.one("SELECT outbox_id FROM slack_decisions")["outbox_id"] == rows(e, kind="approval_request", channel="chat")[0]["id"]


@pytest.mark.parametrize("mode,flow,base", [("rehearsal", True, "https://deskhand.test"), ("live", False, "https://deskhand.test"), ("live", True, "http://localhost:8000")])
def test_without_live_mode_a_switched_on_flow_or_a_public_address_it_is_a_plain_message(env, mode, flow, base):
    e, fake = Engine(api_key=""), Fake()
    d = live_with_buttons(e, fake, mode, flow, base)
    seat_request(e)
    d.deliver_pending()
    assert not flow_runs(fake) and [c for c in plain_dms(fake) if "Approval needed" in c[1]["inputData"]["content"]] and not e.s.all("SELECT * FROM slack_decisions")


def test_reminders_and_decisions_are_never_sent_with_buttons(env):
    e, fake = Engine(api_key=""), Fake()
    d = live_with_buttons(e, fake)
    rid = seat_request(e)
    e.advance_hours(10)
    e.sweep()
    d.deliver_pending()
    assert len(flow_runs(fake)) == 1 and [c for c in plain_dms(fake) if "Reminder" in c[1]["inputData"]["content"]]


def test_if_the_buttons_cannot_be_started_for_certain_the_plain_message_goes_instead_once(env):
    e, fake = Engine(api_key=""), Fake()
    d = live_with_buttons(e, fake)
    fake.plan["flow"] = [(200, json.dumps({"success": False, "message": "flow is deleted"}))]  # a certain failure: nothing went out
    seat_request(e)
    d.deliver_pending()
    row = rows(e, kind="approval_request", channel="chat")[0]
    assert row["delivery_status"] == "delivered" and len(flow_runs(fake)) == 1 and len([c for c in plain_dms(fake) if "Approval needed" in c[1]["inputData"]["content"]]) == 1


def test_if_it_is_unknown_whether_the_buttons_went_out_nothing_is_sent_twice(env):
    e, fake = Engine(api_key=""), Fake()
    d = live_with_buttons(e, fake)
    fake.plan["flow"] = [vs.TransportError("ReadTimeout")]
    seat_request(e)
    d.deliver_pending()
    row = rows(e, kind="approval_request", channel="chat")[0]
    assert row["delivery_status"] == "failed" and "outcome unknown" in row["delivery_error"] and not [c for c in plain_dms(fake) if "Approval needed" in c[1]["inputData"]["content"]]
