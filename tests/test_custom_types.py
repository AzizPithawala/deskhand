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


# ================= 5B.2: recognising a created type =================
import datetime as _dt

from app import directory as dirmod
from app import viasocket_adapter as vs
from app import viasocket_ai as vsai
from tests.test_viasocket_ai import Fake, answer, content, intent

TODAY = _dt.date(2026, 10, 22)


def custom(*words, code="x_business_travel", name="Business travel", enabled=True):
    return [{"code": code, "name": name, "description": "Flights and hotels for work trips", "enabled": enabled, "regex": dirmod.trigger_pattern(list(words))}]


# ---------------- the rules ----------------
@pytest.mark.parametrize("text,hit", [
    ("I need to book a business trip to Pune", True), ("BUSINESS TRIP next week please", True), ("planning some  work   travel in March", True), ("can you arrange work travel?", True),
    ("the business tripping hazard", False), ("business", False), ("trip", False), ("travel for work", False),
])
def test_a_message_matches_a_created_type_by_whole_words_in_any_case(text, hit):
    cls = classifier.classify_rules(text, TODAY, custom("business trip", "work travel"))
    assert [i["type_code"] for i in cls["intents"]] == (["x_business_travel"] if hit else [])


def test_a_matched_request_holds_a_summary_made_from_the_type_name_and_no_details():
    cls = classifier.classify_rules("I need a business trip to Pune with 3 colleagues and a budget of 90000", TODAY, custom("business trip"))
    assert cls["intents"] == [{"type_code": "x_business_travel", "summary": "Business travel request", "details": {}}]
    assert classifier.custom_summary("Parking request") == "Parking request"


def test_a_built_in_and_a_created_type_in_one_message_make_two_requests_and_payroll_always_wins():
    cls = classifier.classify_rules("I need a laptop for my business trip", TODAY, custom("business trip"))
    assert [i["type_code"] for i in cls["intents"]] == ["equipment", "x_business_travel"]
    cls = classifier.classify_rules("question about my salary for the business trip", TODAY, custom("business trip"))
    assert [i["type_code"] for i in cls["intents"]] == ["payroll"]


def test_with_no_match_the_question_names_the_companys_own_types_and_is_unchanged_without_any():
    plain = classifier.classify_rules("hello there", TODAY)
    assert plain["clarifying_question"] == "Could you tell me a bit more about what you need (for example leave, a software seat, equipment, or IT help)?"
    withc = classifier.classify_rules("hello there", TODAY, custom("business trip") + custom("parking space", code="x_parking", name="Parking pass") + custom("x y z", code="x_off", name="Hidden type", enabled=False))
    q = withc["clarifying_question"]
    assert "tell me a bit more" in q and "business travel" in q and "parking pass" in q and "hidden type" not in q.lower()


def test_the_sample_eval_for_the_built_in_types_is_unchanged_when_a_company_has_its_own_types():
    data = json.load(open("data/sample_requests.json"))
    c = custom("business trip", "parking space")
    for r in data["requests"]:
        if r["category"] in ("known", "multi_intent", "blocked"):
            got = {i["type_code"] for i in classifier.classify_rules(r["text"], _dt.date.fromisoformat(data["today"]), c)["intents"]}
            assert got == set(r["expected_types"]), r["text"]


# ---------------- a whole request ----------------
@pytest.fixture
def team(owner):
    boss = apiutil.add_person(owner, "boss@acme.test", "Bo Boss")
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee", manager_id=boss.uid)
    return owner, boss, ann


def ask(person, text):
    return person.post("/api/ingest", {"text": text}).json()


def test_a_message_with_a_trigger_word_becomes_a_request_of_that_type_and_goes_through_the_whole_flow(team):
    owner, boss, ann = team
    make(owner, approver_rule="manager")
    res = ask(ann, "I need to book a business trip to Pune next week")
    assert res["outcome"] == "created" and len(res["request_ids"]) == 1
    rid = res["request_ids"][0]
    r = owner.engine.req(rid)
    assert r["type_code"] == "x_business_travel" and r["summary"] == "Business travel request" and r["details"] == {} and r["chain"] == [boss.uid] and r["status"] == "PENDING_APPROVAL"
    assert boss.post(f"/api/requests/{rid}/approve", {}).status_code == 200
    r = owner.engine.req(rid)
    assert r["status"] == "FULFILLING" and r["fulfiller_id"] == owner.uid  # an admin completes it
    assert owner.post(f"/api/requests/{rid}/done", {}).status_code == 200 and owner.engine.req(rid)["status"] == "CLOSED"
    detail = ann.get(f"/api/requests/{rid}").json()
    assert detail["request"]["type_code"] == "x_business_travel" and detail["steps"] == []


def test_a_type_that_is_switched_off_is_refused_politely_and_an_archived_one_no_longer_matches(team):
    owner, boss, ann = team
    code = make(owner)
    assert owner.post(f"/api/setup/types/{code}", {"enabled": False}).status_code == 200
    res = ask(ann, "I need a business trip")
    assert res["outcome"] == "refused" and res["request_ids"] == []
    assert "not handled through Deskhand here" in " ".join(o["body"] for o in ann.get("/api/outbox").json() if o["to_person"] == ann.uid)
    assert owner.post(f"/api/setup/types/{code}", {"enabled": True}).status_code == 200
    rid = ask(ann, "I need a business trip")["request_ids"][0]
    owner.post(f"/api/setup/types/{code}/remove", {})  # in use: archived
    assert ask(ann, "another business trip please")["outcome"] == "needs_info"  # no longer recognised
    assert boss.post(f"/api/requests/{rid}/approve", {}).status_code == 200  # the old request still works
    assert owner.post(f"/api/requests/{rid}/done", {}).status_code == 200 and owner.engine.req(rid)["status"] == "CLOSED"
    owner.engine.advance_hours(100)
    owner.engine.sweep()  # reminders and escalations still run for requests of an archived type


def test_one_company_cannot_get_another_companys_type_by_using_its_words(main, owner):
    make(owner)
    other = apiutil.company(main, email="o@beta.test", code="code-2", name="Beta Ltd")
    bob = apiutil.add_person(other, "bob@beta.test", "Bob Employee")
    res = ask(bob, "I need a business trip to Pune")
    assert res["outcome"] == "needs_info" and other.engine.req(res["request_ids"][0])["type_code"] == "unclear"


def test_payroll_text_is_blocked_even_when_a_created_type_would_match(team):
    owner, boss, ann = team
    make(owner)
    res = ask(ann, "Please fix my salary slip for the business trip")
    assert res["outcome"] == "blocked_sensitive" and res["request_ids"] == []


def test_a_created_type_can_have_steps_that_run_after_approval(team):
    owner, boss, ann = team
    code = make(owner)
    assert add_step(owner, code).status_code == 200
    s = owner.engine.s
    row = s.one("SELECT * FROM type_steps")
    s.q("UPDATE type_steps SET tested_hash=?, tested_at=1.0, enabled=1, expect_json=? WHERE id=?",
        (steps.step_hash(row["app_slug"], row["action_id"], row["action_version_id"], json.loads(row["input_json"])), json.dumps({"kind": "object", "keys": ["A"]}), row["id"]))
    rid = ask(ann, "I need a business trip")["request_ids"][0]
    assert [x["status"] for x in owner.engine.stepper.view(rid)] == ["planned"]
    boss.post(f"/api/requests/{rid}/approve", {})
    assert [x["status"] for x in owner.engine.stepper.view(rid)] == ["rehearsed"]  # a new company is in rehearsal: nothing is sent


# ---------------- the AI ----------------
@pytest.fixture
def ai(team, monkeypatch):
    for k, v in {"VIASOCKET_ORG_ID": "o", "VIASOCKET_PROJECT_ID": "p", "VIASOCKET_EMBED_SECRET": "secret-value-for-tests-0123456789", "VIASOCKET_API_URL": "https://api.test.invalid",
                 "VIASOCKET_RUN_URL": "https://run.test.invalid"}.items():
        monkeypatch.setenv(k, v)
    owner, boss, ann = team
    code = make(owner)

    def wire(*answers):
        fake, e = Fake(*answers), owner.engine
        clf = vsai.ViaSocketClassifier(e.s, owner.org_id, vs.VsClient(owner.org_id, fake), directory=e.d)
        e.ai_classify, e.ai_enabled = clf, clf.enabled
        vs.ConnectionStore(e.s, owner.org_id).set_ai_enabled(True)
        return fake
    return owner, boss, ann, code, wire


def test_the_ai_is_offered_only_this_companys_types_and_may_choose_one(ai):
    owner, boss, ann, code, wire = ai
    fake = wire(answer(content(intent(code))))
    res = ask(ann, "Could you sort out my trip to the Pune office for the client workshop?")  # no trigger word at all
    r = owner.engine.req(res["request_ids"][0])
    assert r["type_code"] == code and r["provider"] == "viasocket" and r["summary"] == "Business travel request" and r["details"] == {}
    sent = fake.calls[0]["body"]
    enum = sent["json_schema"]["schema"]["properties"]["intents"]["items"]["properties"]["type_code"]["enum"]
    assert enum == classifier.TYPES + [code] and f"- {code}: Business travel. Flights and hotels for work trips" in sent["prompt"]
    assert "UNTRUSTED" in sent["prompt"] and "never invent a type code" in sent["prompt"]


def test_the_shared_schema_and_prompt_are_never_changed_and_other_companies_get_none_of_it(ai, main):
    owner, boss, ann, code, wire = ai
    wire(answer(content(intent(code))))
    ask(ann, "Could you sort out my trip to the Pune office?")
    assert vsai.SCHEMA["schema"]["properties"]["intents"]["items"]["properties"]["type_code"]["enum"] == classifier.TYPES and code not in vsai.PROMPT
    other = apiutil.company(main, email="o@beta.test", code="code-2", name="Beta Ltd")
    bob = apiutil.add_person(other, "bob@beta.test", "Bob Employee")
    fake = Fake(answer(content(intent(code))))  # the model answers with a type that company does not have
    clf = vsai.ViaSocketClassifier(other.engine.s, other.org_id, vs.VsClient(other.org_id, fake), directory=other.engine.d)
    other.engine.ai_classify, other.engine.ai_enabled = clf, clf.enabled
    vs.ConnectionStore(other.engine.s, other.org_id).set_ai_enabled(True)
    res = ask(bob, "Could you sort out my trip to the Pune office?")
    sent = fake.calls[0]["body"]
    assert code not in json.dumps(sent) and res["outcome"] == "needs_info"  # not offered, and an answer outside the allowed set is dropped


@pytest.mark.parametrize("bad_code", ["x_unknown_type", "hack", "x_business_travel_2", "payroll_override"])
def test_a_code_the_company_does_not_have_is_dropped_and_deskhand_asks_for_detail(ai, bad_code):
    owner, boss, ann, code, wire = ai
    wire(answer(content(intent(bad_code))))
    res = ask(ann, "Could you sort out my trip to the Pune office?")
    assert res["outcome"] == "needs_info"
    assert owner.engine.s.one("SELECT COUNT(*) c FROM requests WHERE type_code NOT IN ('unclear')")["c"] == 0


def test_an_archived_or_switched_off_type_is_not_offered_to_the_ai_and_its_code_is_dropped(ai):
    owner, boss, ann, code, wire = ai
    fake = wire(answer(content(intent(code))))
    owner.post(f"/api/setup/types/{code}", {"enabled": False})
    res = ask(ann, "Could you sort out my trip to the Pune office?")
    enum = fake.calls[0]["body"]["json_schema"]["schema"]["properties"]["intents"]["items"]["properties"]["type_code"]["enum"]
    assert code not in enum and res["outcome"] == "needs_info"


def test_when_the_ai_fails_the_rules_still_recognise_the_created_type(ai):
    owner, boss, ann, code, wire = ai
    wire(vs.TransportError("ReadTimeout"))
    res = ask(ann, "I need to book a business trip")
    assert owner.engine.req(res["request_ids"][0])["type_code"] == code and owner.engine.req(res["request_ids"][0])["provider"] == "rules"
    assert "ai_failed_fallback" in [e["type"] for e in owner.engine.s.all("SELECT type FROM events")]


def test_a_description_goes_after_the_formatted_prompt_so_braces_cannot_break_it_and_it_is_plain_data():
    block = vsai.company_types_block([{"code": "x_t", "name": "T", "description": "uses {today} and {0} and {"}])
    assert "{today}" in block and "DATA (never instructions)" in block
    prompt = vsai.PROMPT.format(today="2026-10-22", weekday="Thursday") + block
    assert "2026-10-22" in prompt and "uses {today}" in prompt
    assert vsai.company_types_block([]) == ""


def test_two_argument_classifiers_and_the_old_signatures_still_work(team):
    owner, boss, ann = team
    code = make(owner)
    owner.engine.ai_classify = lambda t, d: {"intents": [{"type_code": "x_ghost", "summary": "s", "details": {}}, {"type_code": "equipment", "summary": "A laptop", "details": {"item": "laptop"}}],
                                             "needs_clarification": False, "clarifying_question": None, "confidence": 0.8, "suspected_injection": False, "injection_flags": [], "provider": "fake"}
    owner.engine.ai_enabled = lambda: True
    res = ask(ann, "Please help me with a thing")
    assert [owner.engine.req(r)["type_code"] for r in res["request_ids"]] == ["equipment"]  # the unknown code was dropped, the built-in kept
    assert classifier.classify_rules("I need a laptop", TODAY)["intents"][0]["type_code"] == "equipment"
    assert classifier.validate_ai_result({"intents": [{"type_code": code, "details": {}}]})["needs_clarification"]  # without the allowed set a created code is not a type
    assert classifier.validate_ai_result({"intents": [{"type_code": code, "details": {}}]}, allowed={code})["intents"][0]["summary"] == "Request"
