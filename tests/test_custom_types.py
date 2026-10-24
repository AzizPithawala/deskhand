"""Request types an admin creates (5B): bounds, safety, audit, isolation, remove versus archive. No network."""
import json

import pytest

from app import classifier, steps
from app import viasocket_catalog as vcat
from tests import apiutil
from tests.apiutil import Browser

NEW = {"name": "Business travel", "description": "Flights and hotels for work trips", "trigger_words": ["business trip", "work travel"], "approver_rule": "manager"}


@pytest.fixture
def main(tmp_path, monkeypatch):
    return apiutil.load_app(monkeypatch, tmp_path)


@pytest.fixture
def owner(main):
    return apiutil.company(main)


def events(b, kind="setup_changed"):
    return [json.loads(e["data_json"]) for e in b.engine.s.all("SELECT data_json FROM events WHERE type=?", (kind,))]


def make(owner, **kw):
    r = owner.post("/api/setup/types", {**NEW, **kw})
    assert r.status_code == 200, r.text
    return r.json()["code"]


# ---------------- creating ----------------
def test_an_admin_creates_a_type_which_is_stored_audited_and_known_to_the_directory(owner):
    r = owner.post("/api/setup/types", NEW)
    v = r.json()
    assert r.status_code == 200 and v["code"] == "x_business_travel" and v["custom"] is True and v["name"] == "Business travel"
    assert v["trigger_words"] == ["business trip", "work travel"] and v["requires_approval"] is True and v["approver_rule"] == "manager" and v["fulfilment_kind"] == "manual_task"
    assert events(owner)[-1] == {"area": "types", "code": "x_business_travel", "changes": {"added": True, "name": "Business travel"}}
    d = owner.engine.d
    assert list(d.types)[-1] == "x_business_travel" and d.types["x_business_travel"]["custom"] and d.triggers["x_business_travel"] == ["business trip", "work travel"]  # after the built-ins
    listed = owner.get("/api/setup/types").json()["types"]
    assert [t["code"] for t in listed][-1] == "x_business_travel" and len(listed) == 9


def test_what_every_member_can_read_has_the_name_and_description_but_never_the_trigger_words(owner, main):
    make(owner)
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee")
    text = ann.get("/api/state").text
    assert "Business travel" in text and "Flights and hotels" in text and "business trip" not in text and "work travel" not in text and "trigger" not in text
    assert ann.get("/api/setup/types").status_code == 403  # the admin list is where the words are


@pytest.mark.parametrize("change,why", [
    ({"name": "x"}, "type name"), ({"name": "Trips <b>"}, "type name"), ({"name": "Equipment"}, "built-in"), ({"name": "software seat"}, "built-in"), ({"name": "Leave"}, "built-in"),
    ({"description": "tiny"}, "description"), ({"description": "x" * 141}, "description"), ({"description": "uses {braces} here"}, "description"),
    ({"trigger_words": []}, "1 to 10"), ({"trigger_words": [f"phrase {i}x" for i in range(11)]}, "1 to 10"), ({"trigger_words": ["ab"]}, "not a usable trigger"),
    ({"trigger_words": ["a;b c"]}, "not a usable trigger"), ({"trigger_words": [".*"]}, "not a usable trigger"), ({"trigger_words": ["x" * 41]}, "not a usable trigger"),
    ({"trigger_words": ["laptop"]}, "built-in"), ({"trigger_words": ["my salary"]}, "built-in"), ({"trigger_words": ["vpn"]}, "built-in"), ({"trigger_words": ["I need leave"]}, "built-in"),
    ({"approver_rule": "role:nobody"}, "role"), ({"approver_rule": "person:ghost"}, "choose someone"), ({"approver_rule": "hacker"}, "valid choice"), ({"approver_rule": None}, None),
])
def test_a_type_that_is_not_safe_is_refused_and_nothing_is_stored(owner, change, why):
    before = len(events(owner))
    r = owner.post("/api/setup/types", {**NEW, **change})
    assert r.status_code == 422, r.text
    if why:
        assert why in json.dumps(r.json())
    assert owner.engine.s.one("SELECT COUNT(*) c FROM custom_types")["c"] == 0 and len(events(owner)) == before


def test_a_description_is_always_one_line(owner):
    v = owner.post("/api/setup/types", {**NEW, "description": "Flights\nand   hotels\tfor work trips"}).json()
    assert v["description"] == "Flights and hotels for work trips"


def test_unknown_fields_are_refused_not_ignored(owner):
    assert owner.post("/api/setup/types", {**NEW, "code": "leave"}).status_code == 422
    assert owner.post("/api/setup/types", {**NEW, "fulfilment_kind": "auto_action"}).status_code == 422
    assert owner.engine.s.one("SELECT COUNT(*) c FROM custom_types")["c"] == 0


def test_names_and_triggers_must_be_unique_in_the_company_and_there_is_a_limit(owner):
    make(owner)
    assert owner.post("/api/setup/types", {**NEW, "trigger_words": ["parking"]}).status_code == 422  # same name
    r = owner.post("/api/setup/types", {**NEW, "name": "Work trips", "trigger_words": ["Business   Trip"]})
    assert r.status_code == 422 and "already a trigger of Business travel" in r.json()["detail"]  # same words, any case or spacing
    assert owner.post("/api/setup/types", {**NEW, "name": "Parking pass", "description": "A parking space at the office", "trigger_words": ["parking space"]}).status_code == 200
    for i in range(18):
        make(owner, name=f"Type number {i}", trigger_words=[f"phrase number {i}x"])
    r = owner.post("/api/setup/types", {**NEW, "name": "One too many", "trigger_words": ["one too many"]})
    assert r.status_code == 422 and "up to 20" in r.json()["detail"]


def test_trigger_words_are_trimmed_lowercased_and_deduplicated(owner):
    v = owner.post("/api/setup/types", {**NEW, "trigger_words": ["  Business   Trip ", "business trip", "WORK TRAVEL"]}).json()
    assert v["trigger_words"] == ["business trip", "work travel"]


# ---------------- editing ----------------
def test_an_admin_edits_a_type_and_every_change_is_audited(owner):
    code = make(owner)
    other = apiutil.add_person(owner, "it@acme.test", "Ira IT")
    r = owner.post(f"/api/setup/types/{code}", {"name": "Work travel", "description": "Trips for work, booked by the office", "trigger_words": ["trip abroad"], "approver_rule": f"person:{other.uid}",
                                                 "reminder_hours": 4, "escalate_hours": 12, "requires_approval": True})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["name"] == "Work travel" and v["trigger_words"] == ["trip abroad"] and v["approver_rule"] == f"person:{other.uid}" and v["reminder_hours"] == 4
    ch = events(owner)[-1]["changes"]
    assert ch["name"] == ["Business travel", "Work travel"] and ch["trigger_words"] == [["business trip", "work travel"], ["trip abroad"]] and ch["approver_rule"][1] == f"person:{other.uid}"
    assert owner.engine.d.types[code]["name"] == "Work travel" and owner.engine.d.triggers[code] == ["trip abroad"]
    assert owner.post(f"/api/setup/types/{code}", {"escalate_hours": 3}).status_code == 422  # later than the reminder of 4


@pytest.mark.parametrize("body", [{"details_needed": ["tool"]}, {"auto_allow_up_to": 5}, {"redirect": "manager"}, {"extra_approvers": [{"rule": "manager", "when": {"cost_above": 100}}]},
                                  {"extra_approvers": [{"rule": "manager", "when": {"detail_contains": {"field": "reason", "words": ["x"]}}}]}])
def test_a_created_type_has_no_detail_fields_cost_or_word_conditions(owner, body):
    code = make(owner)
    assert owner.post(f"/api/setup/types/{code}", body).status_code == 422


def test_a_created_type_can_have_an_unconditional_extra_approver(owner):
    code = make(owner)
    assert owner.post(f"/api/setup/types/{code}", {"extra_approvers": [{"rule": "role:admin", "when": None}]}).status_code == 200


def test_the_built_in_types_still_refuse_a_name_description_or_trigger_words_and_cannot_be_removed_or_reset_for_custom(owner):
    for body in ({"name": "Hacked"}, {"description": "A new description here"}, {"trigger_words": ["something"]}):
        assert owner.post("/api/setup/types/equipment", body).status_code == 422
    assert owner.post("/api/setup/types/equipment/remove", {}).status_code == 422
    code = make(owner)
    r = owner.post(f"/api/setup/types/{code}/reset", {})
    assert r.status_code == 422 and "no default" in r.json()["detail"]
    assert owner.post(f"/api/setup/types/{code}/reviewed", {}).status_code == 200  # a no-op: it was reviewed when it was made


# ---------------- removing ----------------
def test_a_type_nobody_used_is_deleted_and_one_in_use_is_archived(owner):
    a, b = make(owner), make(owner, name="Parking pass", description="A parking space at the office", trigger_words=["parking space"])
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee")
    owner.engine.s.q("INSERT INTO requests(type_code,employee_id,channel,status,created_at) VALUES(?,?,?,?,?)", (b, ann.uid, "chat", "CLOSED", 1.0))
    r = owner.post(f"/api/setup/types/{a}/remove", {})
    assert r.json()["removed"] == "deleted" and a not in owner.engine.d.types and owner.engine.s.one("SELECT COUNT(*) c FROM custom_types WHERE code=?", (a,))["c"] == 0
    r = owner.post(f"/api/setup/types/{b}/remove", {})
    assert r.json()["removed"] == "archived" and [t["code"] for t in r.json()["types"] if t.get("custom")] == []  # hidden from the list
    t = owner.engine.d.types[b]
    assert t["archived"] and not t["enabled"] and owner.engine.d.custom_matchers() == []  # but it is still known, so its old requests work, and it no longer matches
    assert events(owner)[-1]["changes"] == {"removed": "archived"} and owner.post(f"/api/setup/types/{a}/remove", {}).status_code == 404
    assert owner.post("/api/setup/types", {**NEW, "name": "Parking pass", "trigger_words": ["new words"]}).status_code == 422  # the archived name is not reused


def test_a_type_with_steps_is_archived_not_deleted(owner):
    code = make(owner)
    steps.save_steps(owner.engine.s, owner.engine.d, code, [{"app": "google-sheets", "action_id": next(a["action_id"] for a in vcat.FakeCatalog().app("google-sheets")["actions"] if a["name"] == "Add New Row to Sheet"),
                                                              "run_mode": "auto", "input": {"spreadsheet_Id": "s", "grid_Id": "0", "column_key": True, "column_selected": ["A"], "column_name": {"A": "x"}}}],
                     vcat.FakeCatalog(), "u")
    assert owner.post(f"/api/setup/types/{code}/remove", {}).json()["removed"] == "archived"


# ---------------- approval and steps ----------------
def add_step(owner, code):
    row = {"spreadsheet_Id": "s", "grid_Id": "0", "column_key": True, "column_selected": ["A"], "column_name": {"A": "{requester.name}"}}
    aid = next(a["action_id"] for a in vcat.FakeCatalog().app("google-sheets")["actions"] if a["name"] == "Add New Row to Sheet")
    return owner.post(f"/api/setup/steps/{code}", {"steps": [{"app": "google-sheets", "action_id": aid, "run_mode": "auto", "input": row}]})


def test_a_created_type_can_have_steps_but_only_while_a_person_approves(owner):
    code = make(owner)
    assert add_step(owner, code).status_code == 200  # a created type is eligible like the built-ins
    s = owner.engine.s
    row = s.one("SELECT * FROM type_steps")
    s.q("UPDATE type_steps SET tested_hash=?, tested_at=1.0, enabled=1 WHERE id=?", (steps.step_hash(row["app_slug"], row["action_id"], row["action_version_id"], json.loads(row["input_json"])), row["id"]))
    r = owner.post(f"/api/setup/types/{code}", {"requires_approval": False})
    assert r.status_code == 422 and "no one deciding" in r.json()["detail"]
    s.q("UPDATE type_steps SET enabled=0")
    assert owner.post(f"/api/setup/types/{code}", {"requires_approval": False}).status_code == 200
    r = add_step(owner, code)
    assert r.status_code == 422 and "no one deciding" in r.json()["detail"]


# ---------------- isolation and the checklist ----------------
def test_one_companys_types_and_trigger_words_are_invisible_to_another(main, owner):
    make(owner)
    other = apiutil.company(main, email="o@beta.test", code="code-2", name="Beta Ltd")
    assert len(other.get("/api/setup/types").json()["types"]) == 8 and "Business travel" not in other.get("/api/state").text
    assert other.engine.d.triggers == {} and other.engine.d.custom_matchers() == []
    assert other.post("/api/setup/types/x_business_travel", {"reminder_hours": 3}).status_code == 404
    assert other.post("/api/setup/types/x_business_travel/remove", {}).status_code == 404
    assert other.post("/api/setup/types", {**NEW}).status_code == 200  # the same name and words are free there


def test_creating_a_type_does_not_make_the_checklist_ask_for_a_review_of_it(owner):
    before = {s["key"]: s["detail"] for s in owner.get("/api/setup/checklist").json()["steps"]}["types"]
    make(owner)
    after = {s["key"]: s["detail"] for s in owner.get("/api/setup/checklist").json()["steps"]}["types"]
    assert before == after == "0 of 8 reviewed"


def test_the_demo_company_never_has_created_types(main):
    demo = Browser(main)
    demo.demo("e10")
    assert len(demo.get("/api/setup/types").json()["types"]) == 8
    assert demo.post("/api/setup/types", NEW).status_code == 403
    assert not any(t.get("custom") for t in demo.get("/api/state").json()["types"])
