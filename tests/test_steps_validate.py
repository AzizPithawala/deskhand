"""After-approval steps, the pure parts: reading an action's form, the condition evaluator, validating values, filling request fields, and the stored list of steps.
Uses the recorded Google Sheets catalog and the synthetic Demo Tickets app in data/steps_catalog_fixture.json. No network."""
import json
import sqlite3

import pytest

from app import steps
from app import viasocket_catalog as vcat
from app.db import Store

FIX = json.load(open(vcat.FIXTURE))


def raw(app, name):
    return next(a for a in FIX["apps"][app]["actions"] if a["name"] == name)


def action(app, name):
    return steps.describe_action(raw(app, name))


ADD = action("google-sheets", "Add New Row to Sheet")
TICKET = action("demo-tickets", "Create Ticket")
ALLOWED = steps.tokens_for("software_seat")
ROW = {"spreadsheet_Id": "sheet-1", "grid_Id": "0", "column_key": True, "column_selected": ["Name", "Email"], "column_name": {"Name": "{requester.name}", "Email": "{requester.email}"}}


def check(act, values, code="software_seat"):
    return steps.validate_values(act["schema"], values, steps.tokens_for(code))


# ---------------- reading a form ----------------
def test_the_add_row_form_is_read_in_order_with_its_pickers_and_fetched_columns():
    f = {n["key"]: n for n in ADD["schema"]["fields"]}
    assert list(f) == ["spreadsheet_Id", "grid_Id", "column_key", "column_selected", "column_name"] and ADD["supported"]
    assert f["spreadsheet_Id"]["type"] == "dropdown" and f["spreadsheet_Id"]["dynamic"] and f["spreadsheet_Id"]["search"]
    assert f["grid_Id"]["depends_on"] == ["spreadsheet_Id"] and f["column_key"]["type"] == "boolean" and f["column_key"]["default"] is True
    assert f["column_selected"]["type"] == "multiselect" and f["column_name"]["type"] == "fetched_group" and f["column_name"]["required"]


def test_every_recorded_sheets_action_is_read_and_the_destructive_ones_are_flagged():
    for a in FIX["apps"]["google-sheets"]["actions"]:
        d = steps.describe_action(a)
        assert d["supported"], (d["name"], d["reasons"])
        assert d["destructive"] == (d["name"] in ("Delete Rows", "Clear Spreadsheet Row", "Permanently Delete Spreadsheet"))


def test_a_required_field_of_an_unknown_kind_makes_the_action_unsupported_and_an_optional_one_is_dropped():
    bad = steps.describe_action(raw("demo-tickets", "Import From Map"))
    assert not bad["supported"] and "Mapping" in bad["reasons"][0]
    schema = {"steps": {"root": ["a", "b"]}, "blocks": {"a": {"type": "string", "label": "A", "required": True}, "b": {"type": "map", "label": "B"}}}
    n = steps.normalize_schema(schema)
    assert n["supported"] and [f["key"] for f in n["fields"]] == ["a"] and n["dropped"] == ["B"]


def test_blocks_not_reachable_from_the_root_are_ignored_and_children_are_choices():
    n = steps.normalize_schema({"steps": {"root": ["x"]}, "blocks": {"x": {"type": "dropdown", "label": "X", "children": [{"label": "Yes", "value": True}, {"label": "No", "value": False}]},
                                                                      "orphan": {"type": "string", "label": "Never shown"}}})
    assert [f["key"] for f in n["fields"]] == ["x"] and [o["value"] for o in n["fields"][0]["options"]] == [True, False] and not n["fields"][0]["dynamic"]


# ---------------- conditions ----------------
@pytest.mark.parametrize("expr,values,want", [
    ("context?.inputData?.grid_Id", {}, (False, True)),
    ("context?.inputData?.grid_Id", {"grid_Id": "0"}, (True, True)),  # "0" is a non-empty string, so it counts, as in JavaScript
    ("context?.inputData?.spreadsheet_Id && context?.inputData?.grid_Id", {"spreadsheet_Id": "s"}, (False, True)),
    ("context?.inputData?.spreadsheet_Id && context?.inputData?.grid_Id", {"spreadsheet_Id": "s", "grid_Id": "g"}, (True, True)),
    ('context?.inputData?.["spreadsheet_Id"] && context?.inputData?.["sheet_Id"]', {"spreadsheet_Id": "s"}, (False, True)),
    ("context.inputData.deletion_mode === 'range'", {"deletion_mode": "range"}, (True, True)),
    ("context.inputData.deletion_mode === 'range'", {"deletion_mode": "specific"}, (False, True)),
    ("!context?.inputData?.sorting_limit?.is_last_row", {}, (True, True)),
    ("!context?.inputData?.sorting_limit?.is_last_row", {"sorting_limit": {"is_last_row": True}}, (False, True)),
    ("context?.inputData?.search_filter?.type_search_filter === 'basic'  ", {"search_filter": {"type_search_filter": "basic"}}, (True, True)),
    ("context.inputData.a === 1 || context.inputData.b !== 'x'", {"a": 2, "b": "y"}, (True, True)),
    ("context.inputData.flag === true", {"flag": 1}, (False, True)),  # strict equality: 1 is not true
    ("(context.inputData.a)", {"a": []}, (True, True)),  # an empty list is truthy in JavaScript
])
def test_conditions_the_catalog_publishes_are_evaluated_like_javascript(expr, values, want):
    assert steps.eval_condition(expr, values) == want


@pytest.mark.parametrize("expr", ["window.alert(1)", "context.inputData.a.includes('x')", "context.inputData.a > 3", "__import__('os')", "context.inputData.a ? 1 : 2", "((", "a b c", "`x`"])
def test_a_condition_that_is_not_understood_is_never_run_the_field_stays_visible_and_is_reported(expr):
    assert steps.eval_condition(expr, {"a": 5}) == (True, False)


# ---------------- validating values ----------------
def test_good_values_come_back_in_the_shape_the_action_expects_with_request_fields_kept_as_text():
    cleaned, errors, dropped, unreadable = check(ADD, ROW)
    assert errors == {} and cleaned == ROW and dropped == [] and unreadable == []


def test_a_missing_required_value_an_unknown_field_and_a_value_for_a_hidden_field_are_each_reported():
    _, errors, _, _ = check(ADD, {**ROW, "column_name": {}})
    assert errors["column_name"] == "Column Values is required."
    _, errors, _, _ = check(ADD, {**ROW, "surprise": 1})
    assert "surprise" in errors
    cleaned, errors, dropped, _ = check(TICKET, {"project": "IT", "title": "t", "priority": "low", "escalation_note": "not needed"})
    assert errors == {} and "escalation_note" in dropped and "escalation_note" not in cleaned  # only visible fields are sent
    _, errors, _, _ = check(TICKET, {"project": "IT", "title": "t", "priority": "high"})
    assert errors["escalation_note"].endswith("is required.")
    cleaned, errors, dropped, _ = check(TICKET, {"title": "t", "assignee": "someone"})
    assert "project" in errors and "assignee" in dropped  # a field that depends on one with no value is hidden


def test_a_forms_own_defaults_count_as_values_so_dependent_fields_appear_but_a_given_value_wins():
    cleaned, errors, *_ = check(ADD, {"spreadsheet_Id": "s", "grid_Id": "0", "column_selected": ["Name"], "column_name": {"Name": "x"}})
    assert errors == {} and cleaned["column_key"] is True  # "does your first row contain column names" was never touched, so its default is sent
    cleaned, errors, *_ = check(ADD, {**ROW, "column_key": False})
    assert errors == {} and cleaned["column_key"] is False
    cleaned, *_ = check(TICKET, {"project": "IT", "title": "t"})
    assert cleaned["priority"] == "medium" and cleaned["notify"] is False  # a default that is a {label, value} pair gives its value
    assert steps.visible_keys(ADD["schema"], {"spreadsheet_Id": "s", "grid_Id": "0"}) == ["spreadsheet_Id", "grid_Id", "column_key", "column_selected"]


def test_a_choice_must_be_one_the_app_offers_and_cannot_hold_a_request_field():
    assert check(TICKET, {"project": "HR", "title": "t"})[1]["project"].endswith("offers.")
    assert "cannot use request fields" in check(TICKET, {"project": "{request.summary}", "title": "t"})[1]["project"]
    assert check(TICKET, {"project": "IT", "title": "t", "labels": ["hw", "zz"]})[1]["labels"].endswith("offers.")
    assert check(TICKET, {"project": "IT", "title": "t", "assignee": "any id the app lists"})[1] == {}  # a picker the app fills is checked by the test run, not here


def test_only_request_fields_deskhand_offers_for_that_type_can_be_used_and_never_the_raw_message():
    ok = {"project": "IT", "title": "{requester.name} wants {detail.tool}"}
    assert check(TICKET, ok)[1] == {}
    assert "not a request field" in check(TICKET, {"project": "IT", "title": "{detail.tool}"}, "equipment")[1]["title"]  # tool is not a detail of an equipment request
    for token in ("{raw_text}", "{request.raw_text}", "{requester.password}", "{secret}", "{detail.api_key}"):
        assert "title" in check(TICKET, {"project": "IT", "title": f"x {token}"})[1], token
    assert check(TICKET, {"project": "IT", "title": "braces {not a token} and {} and {__class__} and {0} stay text"})[1] == {}  # only {name} starting with a letter is a request field


def test_numbers_dates_and_yes_no_are_typed_and_a_request_field_fits_only_a_matching_kind():
    delete = action("google-sheets", "Delete Rows")
    base = {"spreadsheet_Id": "s", "grid_Id": "0", "deletion_mode": "range"}
    assert check(delete, {**base, "start_row": 2, "end_row": "{request.id}"})[1] == {}
    for bad in (True, "5", float("nan"), "{request.summary}"):
        assert "end_row" in check(delete, {**base, "start_row": 2, "end_row": bad})[1], bad
    assert check(TICKET, {"project": "IT", "title": "t", "due": "{request.approved_date}"})[1] == {}
    assert check(TICKET, {"project": "IT", "title": "t", "due": "2026-10-22"})[1] == {}
    for bad in ("2026-13-40", "22/10/2026", "{request.summary}", "2026-10-22T10:00", 5):
        assert "due" in check(TICKET, {"project": "IT", "title": "t", "due": bad})[1], bad
    assert "notify" in check(TICKET, {"project": "IT", "title": "t", "notify": "yes"})[1]
    assert check(TICKET, {"project": "IT", "title": "t", "notify": False})[1] == {}


def test_texts_are_bounded_and_stripped_of_control_characters():
    cleaned, errors, *_ = check(TICKET, {"project": "IT", "title": "a\x00b\x07c\nd"})
    assert errors == {} and cleaned["title"] == "abc\nd"
    assert "longer than" in check(TICKET, {"project": "IT", "title": "x" * 1001})[1]["title"]
    assert check(TICKET, {"project": "IT", "title": "x" * 1000})[1] == {}
    assert "up to 60" in check(ADD, {**ROW, "column_name": {str(i): "v" for i in range(61)}})[1]["column_name"]
    assert "column" in check(ADD, {**ROW, "column_name": {"a" * 121: "v"}})[1]["column_name"]
    assert check(ADD, {**ROW, "column_name": "not a dict"})[1]
    assert check(ADD, "not a dict")[1] and check(ADD, {"spreadsheet_Id": "x" * 40000})[1]


# ---------------- filling request fields at run time ----------------
def test_request_fields_are_filled_and_a_single_number_field_stays_a_number():
    ctx = steps.sample_context("software_seat")
    assert steps.render("{requester.name} needs {detail.tool} ({request.ref})", ctx) == "Priya Sharma needs Claude (deskhand-12)"
    assert steps.render("{request.id}", ctx) == 12 and steps.render("{detail.estimated_cost}", ctx) == 250 and steps.render(" {request.id} ", ctx) == 12
    assert steps.render({"a": ["{requester.email}", {"b": "{request.type}"}], "n": 3, "t": True}, ctx) == {"a": ["priya.sharma@example.com", {"b": "Software seat"}], "n": 3, "t": True}
    assert steps.render("gone: {detail.reason}.", ctx) == "gone: ."  # a field this request does not have is empty, never an error or a literal


def test_text_that_came_from_a_request_cannot_act_as_a_command_in_the_app_it_lands_in():
    ctx = {"request.summary": '=HYPERLINK("http://x.invalid","click")', "requester.name": "<!channel> and <@U123>", "detail.tool": "+cmd|' /C calc'!A0"}
    assert steps.render("{request.summary}", ctx).startswith("'=HYPERLINK")  # a leading = + - @ would make a spreadsheet cell a formula
    assert steps.render("{detail.tool}", ctx).startswith("'+cmd")
    out = steps.render("Hi {requester.name}", ctx)
    assert "<!" not in out and "<@" not in out  # no @channel or @person from request text
    assert steps.render("=SUM(A1:A2)", ctx) == "=SUM(A1:A2)"  # what the admin typed is the admin's
    assert len(steps.render("{request.summary}", {"request.summary": "x" * 5000})) <= steps.MAX_TOKEN_VALUE + 1
    assert len(steps.render("{request.summary}" * 20, {"request.summary": "y" * 400})) <= steps.MAX_RENDERED


def test_a_test_hash_changes_with_anything_that_was_tested():
    h = steps.step_hash("a", "x", "v1", {"k": 1})
    assert h == steps.step_hash("a", "x", "v1", {"k": 1}) and len({h, steps.step_hash("b", "x", "v1", {"k": 1}), steps.step_hash("a", "y", "v1", {"k": 1}),
                                                                  steps.step_hash("a", "x", "v2", {"k": 1}), steps.step_hash("a", "x", "v1", {"k": 2})}) == 5


# ---------------- which request types may have steps ----------------
def test_only_request_types_a_person_fulfils_may_have_steps(e):
    ok = [c for c in e.d.types if steps.eligible(e.d, c)[0]]
    assert set(ok) == {"software_seat", "equipment", "accessories", "it_help", "access"}
    for c in ("leave", "payroll", "policy_question"):
        assert not steps.eligible(e.d, c)[0]
    assert steps.eligible(e.d, "nope") == (False, "No such request type.")


# ---------------- the stored list ----------------
def step(app="google-sheets", name="Add New Row to Sheet", **kw):
    a = FIX["apps"].get(app, FIX["apps"]["google-sheets"])["actions"]  # an unknown app borrows a real action id so only the app is wrong
    return {"app": app, "action_id": next(x["action_id"] for x in a if x["name"] == name), "run_mode": "auto", "input": ROW, "label": "Add a row", **kw}


def save(e, steps_, code="software_seat", **kw):
    return steps.save_steps(e.s, e.d, code, steps_, vcat.FakeCatalog(), "u1", **kw)


def test_a_valid_step_is_stored_untested_and_off_and_the_change_is_described(e):
    changes = save(e, [step()])
    got = steps.list_steps(e.s, "software_seat")
    assert len(got) == 1 and got[0]["app_name"] == "Google Sheets" and got[0]["action_name"] == "Add New Row to Sheet" and got[0]["input"] == ROW
    assert got[0]["tested"] is False and got[0]["enabled"] is False and got[0]["run_mode"] == "auto"
    assert changes == {"steps": [[], ["Google Sheets: Add New Row to Sheet (auto)"]]}
    assert save(e, [step()]) == {}  # saving the same thing again changes nothing


@pytest.mark.parametrize("bad,match", [
    ([step(), step(), step(), step()], "up to 3"),
    ([step(app="nope")], "not in the catalog"),
    ([step(app="../x")], "not an app name"),
    ([dict(step(), action_id="nope")], "not in the catalog for this app"),
    ([step("demo-tickets", "Import From Map", input={})], "cannot be set up in Deskhand yet"),
    ([dict(step(), run_mode="whenever")], "Choose whether"),
    ([step("google-sheets", "Delete Rows", input={})], "presses Run"),
    ([dict(step(), input={**ROW, "column_name": {"Name": "{raw_text}"}})], "not a request field"),
    ([dict(step(), label="x" * 61)], "The step name must be"),
])
def test_a_step_that_is_not_safe_or_not_possible_is_refused_and_nothing_is_stored(e, bad, match):
    with pytest.raises(steps.StepError) as err:
        save(e, bad)
    assert str(err.value).startswith("Step ") or "up to 3" in str(err.value)
    if match:
        assert match in str(err.value)
    assert steps.list_steps(e.s, "software_seat") == []


def test_a_step_with_no_name_is_called_after_its_action(e):
    save(e, [dict(step(), label="")])
    assert steps.list_steps(e.s, "software_seat")[0]["label"] == "Add New Row to Sheet"


def test_a_destructive_action_is_accepted_only_as_run_by_a_person(e):
    d = step("google-sheets", "Delete Rows", run_mode="confirm", input={"spreadsheet_Id": "s", "grid_Id": "0", "deletion_mode": "specific", "row_numbers": "2"})
    save(e, [d])
    assert steps.list_steps(e.s, "software_seat")[0]["destructive"] is True


@pytest.mark.parametrize("code", ["leave", "payroll", "policy_question", "nope"])
def test_leave_payroll_and_unknown_types_cannot_have_steps(e, code):
    with pytest.raises(steps.StepError):
        save(e, [step()], code)


def test_a_step_keeps_its_test_only_while_it_is_exactly_what_was_tested(e):
    save(e, [step()])
    row = e.s.one("SELECT * FROM type_steps")
    h = steps.step_hash(row["app_slug"], row["action_id"], row["action_version_id"], ROW)
    e.s.q("UPDATE type_steps SET tested_hash=?, tested_at=1.0, enabled=1 WHERE id=?", (h, row["id"]))
    assert steps.list_steps(e.s, "software_seat")[0]["tested"] is True
    save(e, [dict(step(), id=row["id"], label="Renamed")])  # a new name is not a new test
    assert steps.list_steps(e.s, "software_seat")[0]["tested"] is True and steps.list_steps(e.s, "software_seat")[0]["enabled"] is True
    save(e, [dict(step(), id=row["id"], input={**ROW, "column_name": {"Name": "x", "Email": "y"}})])  # different values: untested and off again
    s = steps.list_steps(e.s, "software_seat")[0]
    assert s["tested"] is False and s["enabled"] is False
    save(e, [step()])  # a step saved with no id is a new step: no borrowed test
    assert steps.list_steps(e.s, "software_seat")[0]["tested"] is False


def test_a_step_can_be_switched_on_only_after_a_test_of_exactly_that_step(e):
    save(e, [step()])
    sid = e.s.one("SELECT id FROM type_steps")["id"]
    with pytest.raises(steps.StepError) as err:
        steps.set_enabled(e.s, "software_seat", sid, True)
    assert "test" in str(err.value)
    row = e.s.one("SELECT * FROM type_steps")
    e.s.q("UPDATE type_steps SET tested_hash=?, tested_at=1.0 WHERE id=?", (steps.step_hash(row["app_slug"], row["action_id"], row["action_version_id"], ROW), sid))
    assert steps.set_enabled(e.s, "software_seat", sid, True)["enabled"] == [False, True] and steps.list_steps(e.s, "software_seat")[0]["enabled"]
    steps.set_enabled(e.s, "software_seat", sid, False)  # switching off never needs a test
    assert not steps.list_steps(e.s, "software_seat")[0]["enabled"]
    with pytest.raises(steps.StepError) as err:
        steps.set_enabled(e.s, "equipment", sid, True)  # a step of another type is not found
    assert err.value.status == 404


def test_close_when_done_is_a_setting_of_the_type_and_the_view_lists_the_fields_a_step_may_use(e):
    assert steps.view(e.d, e.s, "software_seat")["close_when_done"] is False
    assert save(e, [], close=True) == {"close_when_done": True} and steps.view(e.d, e.s, "software_seat")["close_when_done"] is True
    v = steps.view(e.d, e.s, "software_seat")
    assert v["eligible"] and v["max_steps"] == 3 and {"requester.name", "detail.tool", "request.ref"} <= {t["token"] for t in v["tokens"]} and "request.raw_text" not in {t["token"] for t in v["tokens"]}
    leave = steps.view(e.d, e.s, "leave")
    assert not leave["eligible"] and leave["tokens"] == []


def test_a_database_from_before_steps_gets_the_snapshot_column_when_it_opens(tmp_path):
    path = str(tmp_path / "old.db")
    s = Store(path)
    s.conn.execute("ALTER TABLE requests DROP COLUMN steps_json")
    s.conn.commit()
    s.conn.close()
    assert "steps_json" not in {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(requests)")}
    assert "steps_json" in {r[1] for r in Store(path).conn.execute("PRAGMA table_info(requests)")}
