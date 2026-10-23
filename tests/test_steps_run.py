"""Running after-approval steps (rule 21): what happens when a request is approved, in rehearsal and live, and every way a step can end. viaSocket is a fake transport (rule 7).
e06 is the approver and the person who completes software seats, e10 is the admin, e01 is the requester."""
import json
import threading
import time

import pytest

from app import delivery as dl
from app import steps
from app import step_runner as sr
from app import viasocket_adapter as vs
from app import viasocket_catalog as vcat
from app.engine import InvalidTransition, NotAuthorized, Refused
from tests.test_viasocket_adapter import AUTH, SCRIPT, SECRET, FakeTransport, ok

FIX = json.load(open(vcat.FIXTURE))
ADD = next(a for a in FIX["apps"]["google-sheets"]["actions"] if a["name"] == "Add New Row to Sheet")
DELETE = next(a for a in FIX["apps"]["google-sheets"]["actions"] if a["name"] == "Delete Rows")
ROW = {"spreadsheet_Id": "sheet-1", "grid_Id": "0", "column_key": True, "column_selected": ["Name", "Email"], "column_name": {"Name": "{requester.name}", "Email": "{requester.email}"}}
GOOD = ok({"success": True, "data": {"Name": "Priya Sharma", "Email": "priya.sharma@acme-demo.example", "_rowNumber": 2, "_sheet_title": "Sheet1"}})
EXPECT = {"kind": "object", "keys": ["Name", "Email", "_rowNumber", "_sheet_title"]}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("VIASOCKET_ORG_ID", "org1")
    monkeypatch.setenv("VIASOCKET_PROJECT_ID", "proj1")
    monkeypatch.setenv("VIASOCKET_EMBED_SECRET", SECRET)
    monkeypatch.setenv("VIASOCKET_API_URL", "https://api.test.invalid")
    monkeypatch.setenv("VIASOCKET_RUN_URL", "https://run.test.invalid")


@pytest.fixture
def w(e):
    """A company engine whose viaSocket is a fake transport. Starts in rehearsal, like every new company."""
    t = FakeTransport(GOOD)
    vs.ConnectionStore(e.s, "acme-demo").save("google-sheets", AUTH, SCRIPT)
    integ = vs.ViaSocketIntegration(e.s, "acme-demo", vs.VsClient("acme-demo", t))
    e.live = dl.Delivery(e, lambda: integ, tenant="acme-demo", fanout=False)
    e.t = t
    return e


def go_live(e):
    e.s.set_setting("delivery_mode", "live")


def make_steps(e, modes=("auto",), close=False, expect=EXPECT, enabled=True, tested=True, code="software_seat", action=ADD, row=ROW):
    payload = [{"app": "google-sheets", "action_id": action["action_id"], "run_mode": m, "label": f"Step {i + 1}", "input": row} for i, m in enumerate(modes)]
    steps.save_steps(e.s, e.d, code, payload, vcat.FakeCatalog(), "e10", close=close)
    for r in e.s.all("SELECT * FROM type_steps WHERE type_code=?", (code,)):
        h = steps.step_hash(r["app_slug"], r["action_id"], r["action_version_id"], json.loads(r["input_json"]))
        e.s.q("UPDATE type_steps SET tested_hash=?, tested_at=1.0, enabled=?, expect_json=? WHERE id=?", (h if tested else None, int(enabled), json.dumps(expect) if expect else None, r["id"]))


def routed(e):
    return e.ingest("chat", "e01", "Need a Claude seat for my work on the research project")["request_ids"][0]


def approved(e):
    rid = routed(e)
    e.approve(rid, "e06")
    return rid


def kinds(e, rid=None):
    return [(r["kind"], r["to_person"]) for r in e.s.all("SELECT kind, to_person FROM outbox" + (" WHERE request_id=?" if rid else "") + " ORDER BY id", (rid,) if rid else ())]


def audit(e, type_):
    return [json.loads(r["data_json"]) for r in e.s.all("SELECT data_json FROM events WHERE type=?", (type_,))]


def statuses(e, rid):
    return [x["status"] for x in e.stepper._rows(rid)]


# ---------------- rehearsal: nothing reaches the app ----------------
def test_in_rehearsal_a_step_is_recorded_not_run_and_the_flow_carries_on_as_if_it_had_worked(w):
    make_steps(w)
    rid = approved(w)
    assert statuses(w, rid) == ["rehearsed"] and w.t.calls == []  # the app was never called
    assert w.req(rid)["status"] == "FULFILLING" and w.req(rid)["fulfiller_id"] == "e06"  # still waits for a person's Done, as before
    sent = [r for r in w.s.all("SELECT * FROM outbox WHERE kind='step_rehearsed'")]
    assert len(sent) == 1 and sent[0]["to_person"] == "e06" and "Priya Sharma" in sent[0]["body"] and "Nothing was sent to the app" in sent[0]["body"]
    assert ("approved", "e01") in kinds(w, rid) and ("task", "e06") in kinds(w, rid) and ("assigned", "e01") in kinds(w, rid)
    assert [a["steps"] for a in audit(w, "step_planned")] == [["Step 1"]] and audit(w, "step_rehearsed")[0]["position"] == 0
    w.done(rid, "e06")
    assert w.req(rid)["status"] == "CLOSED"


def test_a_type_that_closes_by_itself_closes_when_the_steps_finish_and_tells_the_requester(w):
    make_steps(w, close=True)
    rid = approved(w)
    assert w.req(rid)["status"] == "CLOSED" and ("fulfilled", "e01") in kinds(w, rid) and ("task", "e06") not in kinds(w, rid)


def test_the_demo_company_and_chaos_runs_never_call_the_app_even_when_live(w):
    make_steps(w)
    go_live(w)
    w.is_demo = True
    rid = approved(w)
    assert statuses(w, rid) == ["rehearsed"] and w.t.calls == [] and w.stepper.run_pending() == {"ran": 0}


# ---------------- live ----------------
def test_live_approval_queues_the_step_and_the_worker_runs_it_once_with_the_real_values(w):
    make_steps(w)
    go_live(w)
    rid = approved(w)
    assert statuses(w, rid) == ["pending"] and w.t.calls == [] and w.req(rid)["status"] == "FULFILLING"  # approving only writes; the sweep runs it
    assert w.stepper.run_pending() == {"ran": 1}
    assert statuses(w, rid) == ["succeeded"] and len(w.t.calls) == 1
    method, url, _, body = w.t.calls[0]
    assert url.endswith(f"/func/{SCRIPT}") and body["action_version_id"] == ADD["action_version_id"]
    assert body["inputData"] == {**ROW, "column_name": {"Name": "Priya Sharma", "Email": "priya.sharma@acme-demo.example"}}
    assert w.stepper.run_pending() == {"ran": 0} and len(w.t.calls) == 1  # nothing queued any more: nothing runs twice
    row = w.stepper._rows(rid)[0]
    assert json.loads(row["result_summary"]) == {"keys": ["Email", "Name", "_rowNumber", "_sheet_title"], "ref": "2"} and "Priya" not in row["result_summary"]  # field names and a reference, never the answer itself
    assert [a["attempt"] for a in audit(w, "step_succeeded")] == [1] and ("task", "e06") in kinds(w, rid)


def test_two_workers_at_once_run_a_step_exactly_once(w):
    class Slow(FakeTransport):
        def request(self, *a, **k):
            time.sleep(0.3)
            return super().request(*a, **k)
    slow = Slow(GOOD)
    w.live.factory = lambda: vs.ViaSocketIntegration(w.s, "acme-demo", vs.VsClient("acme-demo", slow))
    make_steps(w)
    go_live(w)
    rid = approved(w)
    other = sr.StepRunner(w)  # a second runner object, as if a second process shared the database
    out = []
    ts = [threading.Thread(target=lambda r=r: out.append(r.run_pending())) for r in (w.stepper, other)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(slow.calls) == 1 and sum(o["ran"] for o in out) == 1 and statuses(w, rid) == ["succeeded"]


def test_steps_run_in_order_and_the_second_starts_only_after_the_first_succeeded(w):
    make_steps(w, modes=("auto", "auto"))
    go_live(w)
    rid = approved(w)
    assert statuses(w, rid) == ["pending", "waiting"]
    w.stepper.run_pending()
    assert statuses(w, rid) == ["succeeded", "pending"] and len(w.t.calls) == 1
    w.stepper.run_pending()
    assert statuses(w, rid) == ["succeeded", "succeeded"] and len(w.t.calls) == 2 and ("task", "e06") in kinds(w, rid)


def test_a_first_step_that_fails_stops_the_second_from_ever_running(w):
    make_steps(w, modes=("auto", "auto"))
    go_live(w)
    w.t.answers = [ok({"message": "Requested entity was not found.", "status": 404})]
    rid = approved(w)
    w.stepper.run_pending()
    w.stepper.run_pending()
    assert statuses(w, rid) == ["failed", "waiting"] and len(w.t.calls) == 1 and w.req(rid)["status"] == "PARKED"


# ---------------- every way a step can end ----------------
def parked(e, rid, status_, calls=1):
    assert e.req(rid)["status"] == "PARKED" and statuses(e, rid) == [status_] and len(e.t.calls) == calls
    assert ("parked", "e10") in kinds(e, rid) and ("delay", "e01") in kinds(e, rid)  # the admin and the requester are told
    assert audit(e, "action_failed")[-1]["outcome"] == status_


def test_a_refusal_that_arrives_as_http_200_is_a_permanent_failure_that_parks_the_request_and_is_never_retried(w):
    make_steps(w)
    go_live(w)
    w.t.answers = [ok({"message": f"Requested entity was not found. {SCRIPT}", "status": 404, "originalError": {"stack": "at secret/internal.js:1"}})]
    rid = approved(w)
    w.stepper.run_pending()
    w.stepper.run_pending()
    parked(w, rid, "failed")
    err = w.stepper._rows(rid)[0]["error"]
    assert "not found" in err and SCRIPT not in err and "internal.js" not in err
    assert SCRIPT not in json.dumps(w.s.all("SELECT body FROM outbox")) + json.dumps(w.s.all("SELECT data_json FROM events"))


@pytest.mark.parametrize("answer", [ok({"success": True, "data": "channel_not_found"}), ok({"success": True, "data": {"foo": 1}}), ok({"success": True, "data": []}), ok({"success": True, "data": None})])
def test_a_200_that_does_not_look_like_the_tested_answer_is_not_success(w, answer):
    make_steps(w)
    go_live(w)
    w.t.answers = [answer]
    rid = approved(w)
    w.stepper.run_pending()
    parked(w, rid, "failed")
    assert "different shape" in w.stepper._rows(rid)[0]["error"]


def test_a_timeout_is_outcome_unknown_and_never_run_again_by_itself(w):
    make_steps(w)
    go_live(w)
    w.t.answers = [vs.TransportError("ReadTimeout")]
    rid = approved(w)
    w.stepper.run_pending()
    w.stepper.run_pending()
    parked(w, rid, "unknown")
    assert "may or may not have happened" in [r["body"] for r in w.s.all("SELECT body FROM outbox WHERE kind='parked'")][0]


def test_an_unexpected_crash_while_running_is_unknown_not_a_retry(w):
    make_steps(w)
    go_live(w)

    class Boom:
        def run_step(self, *a, **k):
            raise RuntimeError("boom")
    w.live.factory = lambda: Boom()
    rid = approved(w)
    w.stepper.run_pending()
    assert w.req(rid)["status"] == "PARKED" and statuses(w, rid) == ["unknown"]


def test_a_rate_limit_is_retried_with_backoff_a_few_times_and_then_parked_as_failed(w):
    make_steps(w)
    go_live(w)
    w.t.answers = [(429, "{}")]
    rid = approved(w)
    w.stepper.run_pending()
    assert statuses(w, rid) == ["pending"] and len(w.t.calls) == 1 and audit(w, "step_retry")[0]["next_backoff_s"] == 1
    assert w.stepper.run_pending() == {"ran": 0}  # not yet: the backoff has not passed
    w.advance_hours(1)
    w.stepper.run_pending()
    w.advance_hours(1)
    w.stepper.run_pending()
    assert len(w.t.calls) == 3 and statuses(w, rid) == ["failed"] and w.req(rid)["status"] == "PARKED"
    w.t.answers = [GOOD]
    w.advance_hours(1)
    w.stepper.run_pending()
    assert len(w.t.calls) == 3  # parked: nothing runs by itself any more


def test_a_rate_limit_that_clears_lets_the_step_succeed_on_the_second_attempt(w):
    make_steps(w)
    go_live(w)
    w.t.answers = [(429, "{}"), GOOD]
    rid = approved(w)
    w.stepper.run_pending()
    w.advance_hours(1)
    w.stepper.run_pending()
    assert statuses(w, rid) == ["succeeded"] and w.stepper._rows(rid)[0]["attempts"] == 2 and w.req(rid)["status"] == "FULFILLING"


def test_a_connection_that_is_gone_parks_with_a_readable_reason(w):
    make_steps(w)
    go_live(w)
    vs.ConnectionStore(w.s, "acme-demo").delete("google-sheets")
    rid = approved(w)
    w.stepper.run_pending()
    parked(w, rid, "failed", calls=0)
    assert "not connected any more" in w.stepper._rows(rid)[0]["error"]


def test_a_step_that_was_running_when_the_app_stopped_is_parked_as_unknown_not_run_again(w):
    make_steps(w)
    go_live(w)
    rid = approved(w)
    w.s.q("UPDATE step_runs SET status='running', attempts=1, started_at=? WHERE request_id=?", (time.time() - sr.STUCK_S - 5, rid))
    w.stepper.run_pending()
    assert statuses(w, rid) == ["unknown"] and w.t.calls == [] and w.req(rid)["status"] == "PARKED"


# ---------------- people decide what happens to a parked step ----------------
def test_an_admin_can_run_a_parked_step_again_and_it_goes_on_to_finish_the_request(w):
    make_steps(w)
    go_live(w)
    w.t.answers = [ok({"message": "nope", "status": 400}), GOOD]
    rid = approved(w)
    w.stepper.run_pending()
    with pytest.raises(NotAuthorized):
        w.stepper.replay(rid, "e01")
    with pytest.raises(NotAuthorized):
        w.replay(rid, "e06")  # the person who completes seats is not an admin
    w.replay(rid, "e10")  # the request-level replay picks the parked step
    assert w.req(rid)["status"] == "FULFILLING" and statuses(w, rid) == ["pending"] and audit(w, "step_replayed")
    w.stepper.run_pending()
    assert statuses(w, rid) == ["succeeded"] and len(w.t.calls) == 2


def test_an_admin_can_say_a_step_was_done_after_checking_the_app_and_must_say_what_they_checked(w):
    make_steps(w, modes=("auto", "auto"))
    go_live(w)
    w.t.answers = [vs.TransportError("ReadTimeout"), GOOD]
    rid = approved(w)
    w.stepper.run_pending()
    assert statuses(w, rid) == ["unknown", "waiting"]
    with pytest.raises(Refused):
        w.stepper.replay(rid, "e10", how="mark_done", reason="")
    with pytest.raises(NotAuthorized):
        w.stepper.replay(rid, "e06", how="mark_done", reason="I looked at the sheet")
    w.stepper.replay(rid, "e10", how="mark_done", reason="Row 7 is in the sheet")
    assert statuses(w, rid) == ["succeeded", "pending"] and w.req(rid)["status"] == "FULFILLING"
    assert audit(w, "step_marked_done") == [{"position": 0, "label": "Step 1", "reason": "Row 7 is in the sheet"}] and len(w.t.calls) == 1  # no second call for the first step
    w.stepper.run_pending()
    assert statuses(w, rid) == ["succeeded", "succeeded"]


def test_a_request_that_is_not_parked_cannot_be_replayed_and_done_waits_for_unfinished_steps(w):
    make_steps(w)
    go_live(w)
    rid = approved(w)
    with pytest.raises(InvalidTransition):
        w.stepper.replay(rid, "e10")
    with pytest.raises(Refused):
        w.done(rid, "e06")  # Deskhand is still doing the steps
    w.stepper.run_pending()
    w.done(rid, "e06")
    assert w.req(rid)["status"] == "CLOSED"


def test_a_step_that_waits_for_run_waits_for_the_right_person_and_in_rehearsal_pressing_it_still_sends_nothing(w):
    make_steps(w, modes=("confirm",))
    rid = approved(w)
    assert statuses(w, rid) == ["ready"] and ("task", "e06") in kinds(w, rid)
    for nobody in ("e01", "e02"):
        with pytest.raises(NotAuthorized):
            w.stepper.confirm(rid, 0, nobody)
    assert audit(w, "unauthorized_action_refused")
    w.stepper.confirm(rid, 0, "e06")
    assert statuses(w, rid) == ["rehearsed"] and w.t.calls == [] and audit(w, "step_confirmed")[0]["position"] == 0
    with pytest.raises(InvalidTransition):
        w.stepper.confirm(rid, 0, "e06")  # pressing it twice does nothing


def test_in_live_pressing_run_queues_the_step_and_it_runs_once(w):
    make_steps(w, modes=("confirm",))
    go_live(w)
    rid = approved(w)
    w.stepper.confirm(rid, 0, "e10")
    w.stepper.run_pending()
    assert statuses(w, rid) == ["succeeded"] and len(w.t.calls) == 1


# ---------------- the mode, the snapshot, cancelling ----------------
def test_going_live_skips_every_step_prepared_in_rehearsal_and_parks_the_request_for_an_admin(w):
    make_steps(w, modes=("confirm",))
    rid = approved(w)
    assert statuses(w, rid) == ["ready"]
    w.live.set_mode("live", "e10")
    assert statuses(w, rid) == ["skipped"] and w.req(rid)["status"] == "PARKED" and w.t.calls == []
    assert audit(w, "delivery_mode_changed")[-1]["unfinished_steps_skipped"] == 1 and ("parked", "e10") in kinds(w, rid)
    w.stepper.replay(rid, "e10")  # now in live mode
    assert statuses(w, rid) == ["ready"] and w.req(rid)["status"] == "FULFILLING"
    w.stepper.confirm(rid, 0, "e06")
    w.stepper.run_pending()
    assert statuses(w, rid) == ["succeeded"] and len(w.t.calls) == 1


def test_changing_the_mode_without_steps_in_flight_skips_nothing(w):
    make_steps(w)
    rid = approved(w)  # rehearsed already: finished steps are not touched
    w.live.set_mode("live", "e10")
    assert statuses(w, rid) == ["rehearsed"] and w.req(rid)["status"] == "FULFILLING"


def test_a_request_keeps_the_steps_it_was_routed_with_whatever_is_edited_later(w):
    make_steps(w)
    go_live(w)
    rid = routed(w)  # routed now: the snapshot is taken
    make_steps(w, row={**ROW, "column_name": {"Name": "CHANGED", "Email": "CHANGED"}})  # an admin edits the step afterwards
    w.e = None
    w.approve(rid, "e06")
    w.stepper.run_pending()
    assert w.t.calls[0][3]["inputData"]["column_name"]["Name"] == "Priya Sharma"  # the old values, as routed
    late = routed(w)
    w.approve(late, "e06")
    w.stepper.run_pending()
    assert w.t.calls[1][3]["inputData"]["column_name"]["Name"] == "CHANGED"  # a request routed after the edit uses the new one


def test_steps_added_after_a_request_was_routed_do_not_apply_to_it_and_untested_or_off_steps_are_never_planned(w):
    rid = routed(w)  # no steps yet
    make_steps(w)
    w.approve(rid, "e06")
    assert w.stepper._rows(rid) == [] and ("task", "e06") in kinds(w, rid)  # the old way: a person completes it
    for kw in ({"tested": False}, {"enabled": False}):
        w.s.q("DELETE FROM type_steps")
        make_steps(w, **kw)
        r2 = approved(w)
        assert w.stepper._rows(r2) == [] and w.req(r2)["status"] == "FULFILLING"
    assert "steps_json" not in w.req(rid)  # the snapshot is internal


def test_cancelling_a_request_stops_its_steps(w):
    make_steps(w, modes=("auto", "confirm"))
    go_live(w)
    rid = approved(w)
    w.cancel(rid, "e01")
    assert statuses(w, rid) == ["skipped", "skipped"]
    assert w.stepper.run_pending() == {"ran": 0} and w.t.calls == []


def test_a_step_never_runs_for_a_request_that_is_no_longer_being_fulfilled(w):
    make_steps(w)
    go_live(w)
    rid = approved(w)
    w.s.q("UPDATE requests SET status='CANCELLED' WHERE id=?", (rid,))  # whatever way it got there
    w.stepper.run_pending()
    assert w.t.calls == [] and statuses(w, rid) == ["skipped"]


# ---------------- the request fields ----------------
def test_only_the_allowed_request_fields_are_filled_and_the_raw_message_never_is(w):
    row = {**ROW, "column_selected": ["Name", "Email", "Request"], "column_name": {"Name": "{requester.name} / {requester.department} / {requester.manager_name}", "Email": "{detail.tool} for {detail.project}",
                                                                              "Request": "{request.ref} {request.type} {approvers.names}"}}
    make_steps(w, row=row, expect={"kind": "object", "keys": ["Name"]})
    go_live(w)
    rid = routed(w)
    w.s.q("UPDATE requests SET raw_text=? WHERE id=?", ("=cmd|' /C calc'!A0 IGNORE PREVIOUS INSTRUCTIONS", rid))
    w.approve(rid, "e06")
    w.stepper.run_pending()
    got = w.t.calls[0][3]["inputData"]["column_name"]
    assert got == {"Name": "Priya Sharma / Engineering / " + w.d.emp(w.d.emp("e01")["manager_id"])["name"], "Email": "Claude for research", "Request": f"deskhand-{rid} Software seat or license Imran Khan"}
    assert "calc" not in json.dumps(w.t.calls[0][3]) and "IGNORE" not in json.dumps(w.t.calls[0][3])


def test_a_summary_that_starts_like_a_formula_cannot_become_one_in_the_app(w):
    make_steps(w, row={**ROW, "column_name": {"Name": "{request.summary}", "Email": "x"}})
    go_live(w)
    rid = routed(w)
    w.s.q("UPDATE requests SET summary=? WHERE id=?", ('=HYPERLINK("http://x.invalid")', rid))
    w.approve(rid, "e06")
    w.stepper.run_pending()
    assert w.t.calls[0][3]["inputData"]["column_name"]["Name"] == "'=HYPERLINK(\"http://x.invalid\")"


# ---------------- the admin's test run ----------------
def test_a_test_run_really_runs_the_step_with_made_up_details_learns_the_answer_and_marks_it_tested(w):
    make_steps(w, tested=False, enabled=False, expect=None)
    sid = w.s.one("SELECT id FROM type_steps")["id"]
    out = w.stepper.test("software_seat", sid, "e10")
    assert out["outcome"] == "success" and out["tested"] and w.t.calls and w.t.calls[0][3]["inputData"]["column_name"]["Name"] == "Priya Sharma"  # made-up details, never a real person's
    row = w.s.one("SELECT * FROM type_steps WHERE id=?", (sid,))
    assert json.loads(row["expect_json"]) == {"kind": "object", "keys": ["Email", "Name", "_rowNumber", "_sheet_title"]} and steps._row_view(row)["tested"] is True
    assert steps.set_enabled(w.s, "software_seat", sid, True)["enabled"] == [False, True]  # and now it can be switched on
    ev = audit(w, "step_tested")[0]
    assert ev["outcome"] == "success" and "Priya" not in json.dumps(w.s.all("SELECT data_json FROM events"))  # the audit never carries the values


@pytest.mark.parametrize("answer,outcome", [(ok({"message": "nope", "status": 404}), "permanent_failure"), (ok({"success": True, "data": "ok"}), "unrecognisable"), (ok({"success": True, "data": {}}), "unrecognisable")])
def test_a_test_run_that_fails_or_has_nothing_to_recognise_does_not_mark_the_step_tested(w, answer, outcome):
    make_steps(w, tested=False, enabled=False, expect=None)
    sid = w.s.one("SELECT id FROM type_steps")["id"]
    w.t.answers = [answer]
    out = w.stepper.test("software_seat", sid, "e10")
    assert out["outcome"] == outcome and out["tested"] is False and out["error"]
    assert steps._row_view(w.s.one("SELECT * FROM type_steps"))["tested"] is False and w.s.one("SELECT expect_json FROM type_steps")["expect_json"] is None


def test_testing_a_destructive_step_needs_its_name_typed(w):
    make_steps(w, modes=("confirm",), action=DELETE, row={"spreadsheet_Id": "s", "grid_Id": "0", "deletion_mode": "specific", "row_numbers": "2"}, tested=False, enabled=False, expect=None)
    sid = w.s.one("SELECT id FROM type_steps")["id"]
    with pytest.raises(steps.StepError) as err:
        w.stepper.test("software_seat", sid, "e10")
    assert "Delete Rows" in str(err.value) and w.t.calls == []
    assert w.stepper.test("software_seat", sid, "e10", typed="delete rows")["outcome"] == "success" and len(w.t.calls) == 1


# ---------------- regression: replay no longer touches delivery records ----------------
def test_replaying_a_parked_leave_leaves_the_delivery_records_of_that_request_alone(e):
    rid = e.ingest("chat", "e01", "I need leave tomorrow, family function")["request_ids"][0]
    e.hr.down = True
    e.approve(rid, e.current_approver(e.req(rid)))
    assert e.req(rid)["status"] == "PARKED"
    e.s.q("INSERT INTO action_runs(request_id,step,idempotency_key,attempts,status,updated_at) VALUES(?,?,?,?,?,?)", (rid, "deliver", "deliver:keep", 1, "succeeded", e.now()))
    e.hr.heal()
    e.replay(rid, "e10")
    assert e.req(rid)["status"] == "CLOSED" and e.s.one("SELECT status FROM action_runs WHERE idempotency_key='deliver:keep'")["status"] == "succeeded"


# ---------------- 5A.4: what people see and who is asked ----------------
def inbox_kinds(e, person):
    return [(i["id"], i["inbox_kind"]) for i in e.inbox(person)]


def test_before_approval_the_request_already_shows_the_steps_that_will_follow(w):
    make_steps(w, modes=("auto", "confirm"))
    rid = routed(w)
    v = w.stepper.view(rid)
    assert [(s["label"], s["status"], s["status_text"]) for s in v] == [("Step 1", "planned", "Planned: runs after the final approval"), ("Step 2", "planned", "Planned: runs after the final approval")]
    assert all(not s["can_run"] and not s["can_decide"] and "input" not in s for s in v)
    w.approve(rid, "e06")
    assert [s["status"] for s in w.stepper.view(rid)] == ["rehearsed", "ready"]  # the real runs replace the plan
    w.cancel(rid, "e01")
    assert [s["status"] for s in w.stepper.view(rid)] == ["rehearsed", "skipped"]  # what already happened stays; what had not yet is skipped


def test_a_step_that_waits_for_run_is_a_task_for_those_who_may_press_it_and_the_fulfiller_waits_until_the_steps_are_done(w):
    make_steps(w, modes=("confirm",))
    rid = approved(w)
    assert inbox_kinds(w, "e06") == [(rid, "run")] and inbox_kinds(w, "e10") == [(rid, "run")] and inbox_kinds(w, "e01") == []  # not the requester, and nobody is asked to click Done yet
    flags = {a: [s["can_run"] for s in w.stepper.view(rid, actor=a)] for a in ("e06", "e10", "e01")}
    assert flags == {"e06": [True], "e10": [True], "e01": [False]}
    w.stepper.confirm(rid, 0, "e06")
    assert inbox_kinds(w, "e06") == [(rid, "task")]  # the steps are done: now the person who completes it clicks Done


def test_while_steps_run_by_themselves_nobody_is_asked_to_click_done(w):
    make_steps(w)
    go_live(w)
    rid = approved(w)
    assert inbox_kinds(w, "e06") == [] and inbox_kinds(w, "e10") == []
    w.stepper.run_pending()
    assert inbox_kinds(w, "e06") == [(rid, "task")]


def test_only_an_admin_is_offered_a_decision_about_a_parked_step(w):
    make_steps(w)
    go_live(w)
    w.t.answers = [vs.TransportError("ReadTimeout")]
    rid = approved(w)
    w.stepper.run_pending()
    assert [s["can_decide"] for s in w.stepper.view(rid, actor="e10")] == [True] and [s["can_decide"] for s in w.stepper.view(rid, actor="e06")] == [False]
    assert inbox_kinds(w, "e10") == [(rid, "parked")] and inbox_kinds(w, "e06") == []


def test_the_demo_company_shows_one_example_step_that_never_runs(tmp_path, monkeypatch):
    from tests import apiutil
    main = apiutil.load_app(monkeypatch, tmp_path)
    demo = apiutil.Browser(main)
    demo.demo("e01")
    rid = demo.post("/api/ingest", {"text": "Need a Claude seat for my work on the research project"}).json()["request_ids"][0]
    assert [s["status"] for s in demo.get(f"/api/requests/{rid}").json()["steps"]] == ["planned"]
    admin = apiutil.Browser(main)
    admin.demo("e06")  # Imran approves the seat
    assert admin.post(f"/api/requests/{rid}/approve", {}).status_code == 200
    steps_ = admin.get(f"/api/requests/{rid}").json()["steps"]
    assert [(s["label"], s["status"]) for s in steps_] == [("Add a row to the seats sheet", "rehearsed")]
    assert admin.engine.s.one("SELECT COUNT(*) c FROM outbox WHERE body LIKE '%would have run%'")["c"] == 1 and admin.engine.stepper.run_pending() == {"ran": 0}
    assert main.registry.engine_for(admin.org_id).s.one("SELECT COUNT(*) c FROM type_steps")["c"] == 1  # seeded once, not on every start


def test_the_preflight_report_counts_steps_and_their_runs(tmp_path):
    from app import preflight
    from app.db import Store
    path = str(tmp_path / "p.db")
    store = Store(path)
    store.q("INSERT INTO type_steps(type_code,position,app_slug,action_id,action_version_id,label,input_json,enabled) VALUES('software_seat',0,'google-sheets','a','v','L','{}',1)")
    store.q("INSERT INTO step_runs(request_id,position,step_key,status) VALUES(1,0,'1:0','rehearsed')")
    lines = dict(preflight.report(path, "x"))
    assert lines["steps in other apps"] == "1 set up, 1 switched on" and lines["step runs"] == "rehearsed 1"
