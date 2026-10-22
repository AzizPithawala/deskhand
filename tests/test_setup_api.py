"""Company setup through the HTTP API: roles, request-type rules, policies, routing test, checklist. Admin-only, validated, audited, isolated. No network."""
import json

import pytest

from tests import apiutil
from tests.apiutil import Browser

SETUP_GETS = ("/api/setup/roles", "/api/setup/types", "/api/setup/policies", "/api/setup/checklist")
SETUP_POSTS = (("/api/setup/roles", {"name": "Facilities"}), ("/api/setup/roles/it_admin", {"holder_id": None}), ("/api/setup/roles/it_admin/delete", {}),
               ("/api/setup/types/equipment", {"reminder_hours": 3}), ("/api/setup/types/equipment/reset", {}), ("/api/setup/types/equipment/reviewed", {}),
               ("/api/setup/policies", {"title": "Dress code", "text": "Smart casual."}), ("/api/setup/policies/dress_code/delete", {}),
               ("/api/setup/simulate", {"person_id": "x", "type_code": "leave"}), ("/api/setup/checklist/dismiss", {}))


@pytest.fixture
def main(tmp_path, monkeypatch):
    return apiutil.load_app(monkeypatch, tmp_path)


@pytest.fixture
def team(main):
    """An owner, an IT person, a manager and two employees. Everyone has joined."""
    owner = apiutil.company(main)
    it = apiutil.add_person(owner, "it@acme.test", "Ira IT")
    boss = apiutil.add_person(owner, "boss@acme.test", "Bo Boss")
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee", manager_id=boss.uid)
    bob = apiutil.add_person(owner, "bob@acme.test", "Bob Employee", manager_id=boss.uid)
    return owner, it, boss, ann, bob


def chain_of(owner, rid):
    return owner.engine.req(rid)["chain"]


def test_members_and_signed_out_visitors_are_refused_every_setup_route(team, main):
    owner, it, boss, ann, bob = team
    for url in SETUP_GETS:
        assert ann.get(url).status_code == 403, url
        assert Browser(main).get(url).status_code == 401, url
    for url, body in SETUP_POSTS:
        assert ann.post(url, body).status_code == 403, url
    assert "unauthorized_action_refused" in [e["type"] for e in owner.engine.s.all("SELECT type FROM events")]
    for url in SETUP_GETS:
        assert owner.get(url).status_code == 200, url


def test_every_setup_write_needs_the_csrf_token(team):
    owner = team[0]
    good = owner.csrf
    owner.csrf = None
    assert owner.post("/api/setup/roles", {"name": "Facilities"}).status_code == 403
    owner.csrf = good
    assert owner.post("/api/setup/roles", {"name": "Facilities"}).status_code == 200


def test_the_demo_company_can_be_read_but_never_changed(main):
    demo = Browser(main)
    assert demo.demo("e10").status_code == 200  # Arjun, the demo admin
    roles = {r["code"]: r["holder_name"] for r in demo.get("/api/setup/roles").json()["roles"]}
    assert roles == {"it_admin": "Imran Khan", "office_admin": "Sunita Joshi", "hr_lead": "Deepa Nair", "finance_lead": "Vikram Singh"}
    assert {t["code"] for t in demo.get("/api/setup/types").json()["types"]} >= {"leave", "access", "payroll"}
    assert demo.post("/api/setup/simulate", {"person_id": "e01", "type_code": "software_seat", "estimated_cost": 9000}).json()["chain"][0]["name"] == "Imran Khan"
    assert demo.get("/api/setup/checklist").json()["demo"] is True
    for url, body in SETUP_POSTS:
        if "simulate" not in url:
            assert demo.post(url, body).status_code == 403, url
    member = Browser(main)
    member.demo("e01")
    assert member.get("/api/setup/types").status_code == 403


# ---------------- roles ----------------
def test_roles_start_without_holders_and_a_holder_changes_routing(team):
    owner, it, boss, ann, bob = team
    roles = {r["code"]: r for r in owner.get("/api/setup/roles").json()["roles"]}
    assert set(roles) == {"it_admin", "office_admin", "hr_lead", "finance_lead"} and all(r["holder_id"] is None and r["builtin"] for r in roles.values())
    rid = ann.post("/api/ingest", {"text": "Need a Claude seat for my research project", "source_event_id": "a1"}).json()["request_ids"][0]
    assert chain_of(owner, rid) == [owner.uid]  # nobody holds IT admin: backup rule, then an admin
    r = owner.post("/api/setup/roles/it_admin", {"holder_id": it.uid})
    assert r.status_code == 200 and {x["code"]: x["holder_name"] for x in r.json()["roles"]}["it_admin"] == "Ira IT"
    rid2 = ann.post("/api/ingest", {"text": "Need a Figma seat for design", "source_event_id": "a2"}).json()["request_ids"][0]
    assert chain_of(owner, rid2) == [it.uid]
    assert chain_of(owner, rid) == [owner.uid]  # a request already routed keeps its chain
    assert it.get("/api/inbox").json()[0]["id"] == rid2  # and the new holder sees it in their own inbox
    assert {r["code"]: r["holder_state"] for r in owner.get("/api/setup/roles").json()["roles"]}["it_admin"] == "ok"


def test_a_holder_must_be_an_active_person_of_this_company(main, team):
    owner, it, boss, ann, bob = team
    other = apiutil.company(main, "other@else.test", "code-2", "Else Co")
    for bad in ("nobody", other.uid, "", "x" * 200):
        r = owner.post("/api/setup/roles/it_admin", {"holder_id": bad})
        assert (r.status_code == 422) == bool(bad), (bad, r.status_code)  # an empty holder clears the role
    invited = owner.post("/api/people/invite", {"email": "new@acme.test", "name": "Not Yet", "access_role": "member"}).json()["user"]
    assert owner.post("/api/setup/roles/it_admin", {"holder_id": invited["id"]}).status_code == 422  # has not joined
    assert owner.post("/api/setup/roles/nope", {"holder_id": it.uid}).status_code == 404
    assert owner.post("/api/setup/roles/it_admin", {"holder_id": it.uid}).status_code == 200
    assert owner.post(f"/api/people/{it.uid}", {"status": "disabled"}).status_code == 200
    r = {x["code"]: x for x in owner.get("/api/setup/roles").json()["roles"]}["it_admin"]
    assert r["holder_state"] == "inactive"  # the page can warn that this role is effectively empty
    rid = ann.post("/api/ingest", {"text": "Need a Claude seat for my research project", "source_event_id": "z"}).json()["request_ids"][0]
    assert chain_of(owner, rid) == [owner.uid]  # a deactivated holder falls back like an empty role


def test_custom_roles_can_be_added_and_deleted_only_when_unused(team):
    owner, it, boss, ann, bob = team
    r = owner.post("/api/setup/roles", {"name": "Facilities lead"})
    assert r.status_code == 200 and r.json()["code"] == "facilities_lead"
    for bad in ("", "x", "Admin", "manager", "IT admin", "Facilities lead", "<", "a" * 41):
        assert owner.post("/api/setup/roles", {"name": bad}).status_code == 422, bad
    assert owner.post("/api/setup/types/equipment", {"approver_rule": "role:facilities_lead"}).status_code == 200
    r = owner.post("/api/setup/roles/facilities_lead/delete")
    assert r.status_code == 422 and "Equipment" in r.json()["detail"]
    assert owner.post("/api/setup/types/equipment/reset").status_code == 200
    assert owner.post("/api/setup/roles/facilities_lead/delete").status_code == 200
    assert owner.post("/api/setup/roles/it_admin/delete").status_code == 422  # built-in roles stay
    assert owner.post("/api/setup/roles/facilities_lead/delete").status_code == 404


# ---------------- request types ----------------
def test_types_list_what_can_be_edited_and_labels_the_rules(team):
    owner, it, boss, ann, bob = team
    owner.post("/api/setup/roles/it_admin", {"holder_id": it.uid})
    types = {t["code"]: t for t in owner.get("/api/setup/types").json()["types"]}
    assert set(types) == {"leave", "software_seat", "equipment", "accessories", "it_help", "access", "payroll", "policy_question"}
    assert types["payroll"]["fields"] == ["redirect"] and types["policy_question"]["fields"] == ["enabled"] and "auto_allow_up_to" in types["accessories"]["fields"]
    assert "fulfiller_rule" not in types["leave"]["fields"] and "fulfiller_rule" in types["equipment"]["fields"]
    seat = types["software_seat"]
    assert seat["labels"]["role:it_admin"] == {"rule": "role:it_admin", "label": "IT admin (Ira IT)", "ok": True, "note": None}
    assert seat["labels"]["manager"]["label"] == "The requester's manager" and types["equipment"]["labels"]["role:office_admin"]["ok"] is False
    assert seat["changed"] == [] and seat["reviewed_at"] is None and seat["condition_fields"] == ["tool", "project", "justification"]


def test_an_edit_changes_routing_for_new_requests_and_is_audited(team):
    owner, it, boss, ann, bob = team
    r = owner.post("/api/setup/types/software_seat", {"approver_rule": f"person:{it.uid}", "extra_approvers": [{"rule": "manager", "when": {"cost_above": 100}}], "reminder_hours": 2, "escalate_hours": 5})
    assert r.status_code == 200 and r.json()["changed"] == ["approver_rule", "extra_approvers", "reminder_hours", "escalate_hours"] and r.json()["reviewed_at"]
    rid = ann.post("/api/ingest", {"text": "Need a Figma seat for design, approx 1500", "source_event_id": "e1"}).json()["request_ids"][0]
    assert chain_of(owner, rid) == [it.uid, boss.uid]  # the person we named, then the manager because the cost is above the limit
    assert owner.engine.d.type("software_seat")["reminder_hours"] == 2
    rows = owner.engine.s.all("SELECT actor, data_json FROM events WHERE type='setup_changed'")
    last = json.loads(rows[-1]["data_json"])
    assert rows[-1]["actor"] == owner.uid and last["area"] == "types" and last["code"] == "software_seat" and set(last["changes"]) == {"approver_rule", "extra_approvers", "reminder_hours", "escalate_hours"}
    assert last["changes"]["reminder_hours"] == [8, 2]


def test_putting_a_value_back_to_the_default_forgets_the_edit_and_reset_restores_everything(team):
    owner, it, boss, ann, bob = team
    owner.post("/api/setup/types/equipment", {"reminder_hours": 3})
    assert owner.get("/api/setup/types").json()["types"][2]["changed"] == ["reminder_hours"]
    owner.post("/api/setup/types/equipment", {"reminder_hours": 12})  # the template value
    assert owner.get("/api/setup/types").json()["types"][2]["changed"] == [] and owner.engine.s.one("SELECT rules_json FROM type_rules WHERE code='equipment'")["rules_json"] == "{}"
    owner.post("/api/setup/types/equipment", {"approver_rule": "manager", "backup_rule": None, "extra_approvers": []})
    t = owner.post("/api/setup/types/equipment/reset").json()
    assert t["changed"] == [] and t["approver_rule"] == "role:office_admin" and t["extra_approvers"][0]["when"]["cost_above"] == 20000


BAD_EDITS = [
    ("equipment", {"name": "Hacked"}, 422), ("equipment", {"code": "x"}, 422), ("equipment", {"fulfilment_kind": "auto_action"}, 422),
    ("payroll", {"requires_approval": True}, 422), ("payroll", {"enabled": False}, 422), ("policy_question", {"approver_rule": "manager"}, 422),
    ("equipment", {"auto_allow_up_to": 5}, 422), ("leave", {"fulfiller_rule": "manager"}, 422),
    ("equipment", {"approver_rule": "role:nonexistent"}, 422), ("equipment", {"approver_rule": "person:nobody"}, 422), ("equipment", {"approver_rule": "Manager"}, 422),
    ("equipment", {"approver_rule": "manager; drop table"}, 422), ("equipment", {"approver_rule": None}, 422), ("equipment", {"approver_rule": "role:Admin"}, 422),
    ("equipment", {"extra_approvers": [{"rule": "manager"}] * 3}, 422), ("equipment", {"extra_approvers": [{"rule": "manager", "when": {"cost_above": -1}}]}, 422),
    ("equipment", {"extra_approvers": [{"rule": "manager", "when": {"cost_above": "lots"}}]}, 422), ("equipment", {"extra_approvers": [{"rule": "manager", "when": {"bogus": 1}}]}, 422),
    ("equipment", {"extra_approvers": [{"rule": "manager", "when": {"detail_contains": {"field": "resource", "words": ["x"]}}}]}, 422),
    ("access", {"extra_approvers": [{"rule": "manager", "when": {"detail_contains": {"field": "resource", "words": ["a;b"]}}}]}, 422),
    ("access", {"extra_approvers": [{"rule": "manager", "when": {"detail_contains": {"field": "resource", "words": []}}}]}, 422),
    ("access", {"extra_approvers": [{"rule": "manager", "when": {"detail_contains": {"field": "resource", "words": ["w"] * 11}}}]}, 422),
    ("equipment", {"reminder_hours": 0}, 422), ("equipment", {"reminder_hours": 721}, 422), ("equipment", {"escalate_hours": 0}, 422),
    ("equipment", {"reminder_hours": 30, "escalate_hours": 30}, 422), ("equipment", {"escalate_hours": 5}, 422),  # template reminder is 12: escalation must be later
    ("equipment", {"details_needed": ["password"]}, 422), ("accessories", {"auto_allow_up_to": -5}, 422), ("equipment", {"enabled": "maybe"}, 422),
    ("nonexistent", {"enabled": False}, 404),
]


@pytest.mark.parametrize("code,body,status", BAD_EDITS)
def test_bad_edits_are_refused_and_change_nothing(team, code, body, status):
    owner = team[0]
    before = owner.engine.s.all("SELECT * FROM type_rules")
    r = owner.post(f"/api/setup/types/{code}", body)
    assert r.status_code == status, r.text
    assert owner.engine.s.all("SELECT * FROM type_rules") == before
    assert not owner.engine.s.all("SELECT * FROM events WHERE type='setup_changed'")


def test_accessories_allowance_and_conditions_on_words(team):
    owner, it, boss, ann, bob = team
    owner.post("/api/setup/roles/finance_lead", {"holder_id": it.uid})
    owner.post("/api/setup/types/accessories", {"auto_allow_up_to": 100})
    rid = ann.post("/api/ingest", {"text": "I need a keyboard, cost 1500", "source_event_id": "k1"}).json()["request_ids"][0]
    assert owner.engine.req(rid)["status"] == "PENDING_APPROVAL" and chain_of(owner, rid)  # above the new allowance it needs approval
    r = owner.post("/api/setup/types/access", {"extra_approvers": [{"rule": "role:finance_lead", "when": {"detail_contains": {"field": "resource", "words": ["Payroll Sheet", "BUDGET"]}}}]})
    assert r.status_code == 200 and r.json()["extra_approvers"][0]["when"]["detail_contains"]["words"] == ["payroll sheet", "budget"]
    rid2 = bob.post("/api/ingest", {"text": "Need access to the budget folder on Drive.", "source_event_id": "k2"}).json()["request_ids"][0]
    assert chain_of(owner, rid2) == [boss.uid, it.uid]


def test_reviewed_marks_and_the_checklist_follow_the_data(team):
    owner, it, boss, ann, bob = team
    steps = {s["key"]: s for s in owner.get("/api/setup/checklist").json()["steps"]}
    assert steps["people"]["done"] and steps["managers"]["done"] is False and not steps["roles"]["done"] and not steps["types"]["done"] and not steps["apps"]["done"] and not steps["test"]["done"]
    assert steps["managers"]["detail"] == "2 of 4 have a manager" and steps["roles"]["detail"] == "0 of 4 roles have a holder"
    for code in ("leave", "software_seat", "equipment", "accessories", "it_help", "access", "payroll", "policy_question"):
        assert owner.post(f"/api/setup/types/{code}/reviewed").json()["reviewed_at"]
    for i, role in enumerate(("it_admin", "office_admin", "hr_lead", "finance_lead")):
        owner.post(f"/api/setup/roles/{role}", {"holder_id": [it, boss, ann, bob][i].uid})
    owner.post(f"/api/people/{it.uid}", {"manager_id": boss.uid})
    owner.post(f"/api/people/{boss.uid}", {"manager_id": owner.uid})
    ann.post("/api/ingest", {"text": "I need Friday and Monday off, family function.", "source_event_id": "c"})
    c = owner.get("/api/setup/checklist").json()
    assert [s["done"] for s in c["steps"]] == [True, True, True, True, False, True] and c["done"] == 5 and c["total"] == 6 and c["dismissed"] is False
    assert owner.post("/api/setup/checklist/dismiss").status_code == 200 and owner.get("/api/setup/checklist").json()["dismissed"] is True


# ---------------- policies ----------------
def test_policies_are_the_companys_own_and_start_empty(team):
    owner, it, boss, ann, bob = team
    assert owner.get("/api/setup/policies").json()["policies"] == []  # Acme's work-from-home text never reaches a new company
    r = owner.post("/api/setup/policies", {"title": "Work from home", "text": "Three days a week.\x00"})
    assert r.status_code == 200 and r.json()["policies"] == [{"key": "work_from_home", "title": "Work from home", "text": "Three days a week."}]
    assert owner.post("/api/setup/policies", {"title": "Work from home", "text": "Two days."}).json()["policies"][0]["text"] == "Two days."  # same name updates
    for bad in ({"title": "x", "text": "t"}, {"title": "Fine", "text": ""}, {"title": "Fine", "text": "y" * 1001}, {"title": "!!", "text": "t"}):
        assert owner.post("/api/setup/policies", bad).status_code == 422, bad
    assert owner.post("/api/setup/policies/work_from_home/delete").status_code == 200 and owner.post("/api/setup/policies/work_from_home/delete").status_code == 404


# ---------------- routing test ----------------
def test_the_routing_test_matches_what_a_real_request_does(team):
    owner, it, boss, ann, bob = team
    owner.post("/api/setup/roles/it_admin", {"holder_id": it.uid})
    owner.post("/api/setup/roles/finance_lead", {"holder_id": boss.uid})
    cases = [("leave", "I need Friday and Monday off, family function.", None, None), ("software_seat", "Need a Claude seat for my research project", None, None),
             ("software_seat", "Need a Figma seat for design, approx 9000", 9000, None), ("access", "Need access to the shared finance folder on Drive.", None, "shared finance folder on Drive"),
             ("access", "Need admin access to the AWS console", None, "AWS console"), ("equipment", "I need a second monitor for my desk, cost 25000", 25000, None),
             ("it_help", "VPN is not connecting since morning.", None, None)]
    for i, (code, text, cost, res) in enumerate(cases):
        sim = owner.post("/api/setup/simulate", {"person_id": ann.uid, "type_code": code, "estimated_cost": cost, "resource": res}).json()
        rid = ann.post("/api/ingest", {"text": text, "source_event_id": f"s{i}"}).json()["request_ids"][0]
        real = owner.engine.req(rid)
        assert real["type_code"] == code, (code, real["type_code"])
        assert [c["id"] for c in sim["chain"]] == real["chain"], (code, sim["chain"], real["chain"])
        if real["fulfiller_id"]:
            assert sim["fulfiller"]["id"] == real["fulfiller_id"]
    s = owner.post("/api/setup/simulate", {"person_id": ann.uid, "type_code": "access", "resource": "AWS console"}).json()
    assert [c["reason"] for c in s["chain"]] == ["approver", "extra approver"] and s["outcome"] == "approval" and "Needs approval from Bo Boss then Ira IT" in s["text"]
    assert s["escalates_to"]["id"] == owner.uid  # the manager approves, so the backup rule (an admin) is who it escalates to


def test_the_routing_test_warns_about_empty_roles_and_covers_blocked_and_disabled_types(team):
    owner, it, boss, ann, bob = team
    s = owner.post("/api/setup/simulate", {"person_id": ann.uid, "type_code": "software_seat"}).json()
    assert s["chain"][0]["id"] == owner.uid and "fallback" in s["chain"][0]["reason"] and any("IT admin" in w and "no holder" in w for w in s["warnings"])
    assert owner.post("/api/setup/simulate", {"person_id": ann.uid, "type_code": "payroll"}).json()["outcome"] == "blocked"
    assert owner.post("/api/setup/simulate", {"person_id": ann.uid, "type_code": "policy_question"}).json()["outcome"] == "answered"
    owner.post("/api/setup/types/it_help", {"enabled": False})
    assert owner.post("/api/setup/simulate", {"person_id": ann.uid, "type_code": "it_help"}).json()["outcome"] == "disabled"
    nomgr = owner.post("/api/setup/simulate", {"person_id": it.uid, "type_code": "leave"}).json()
    assert any("no manager" in w for w in nomgr["warnings"]) and nomgr["chain"][0]["id"] == owner.uid  # no manager: the backup rule, then an admin
    for bad in ({"person_id": "nobody", "type_code": "leave"}, {"person_id": ann.uid, "type_code": "nope"}, {"person_id": ann.uid, "type_code": "leave", "estimated_cost": -1}):
        assert owner.post("/api/setup/simulate", bad).status_code in (404, 422), bad


# ---------------- isolation ----------------
def test_one_companys_setup_never_reaches_another(main, team):
    owner, it, boss, ann, bob = team
    other = apiutil.company(main, "o@else.test", "code-2", "Else Co")
    ed = apiutil.add_person(other, "ed@else.test", "Ed Else")
    owner.post("/api/setup/roles/it_admin", {"holder_id": it.uid})
    owner.post("/api/setup/types/software_seat", {"approver_rule": f"person:{it.uid}"})
    owner.post("/api/setup/policies", {"title": "Secret policy", "text": "Only for Acme."})
    owner.post("/api/setup/roles", {"name": "Acme only"})
    assert other.post("/api/setup/types/software_seat", {"approver_rule": f"person:{it.uid}"}).status_code == 422  # another company's person is not a person here
    assert other.post("/api/setup/roles/it_admin", {"holder_id": it.uid}).status_code == 422
    blob = json.dumps([other.get(u).json() for u in SETUP_GETS])
    for leak in ("Ira IT", it.uid, "Secret policy", "Acme only", "acme.test"):
        assert leak not in blob
    rid = ed.post("/api/ingest", {"text": "Need a Claude seat for my research project", "source_event_id": "i1"}).json()["request_ids"][0]
    assert other.engine.req(rid)["chain"] == [other.uid]  # Else Co still uses its own defaults
    assert other.engine.d.type("software_seat")["approver_rule"] == "role:it_admin" and other.engine.s.all("SELECT * FROM type_rules") == []


# ---------------- what employees experience ----------------
def replies(person):
    """Chat replies, newest first (the outbox endpoint's order)."""
    return [m["body"] for m in person.get("/api/outbox").json() if m["channel"] == "chat"]


def test_a_switched_off_type_creates_no_request_and_the_employee_is_told_who_to_ask(team):
    owner, it, boss, ann, bob = team
    assert owner.post("/api/setup/types/equipment", {"enabled": False}).status_code == 200
    res = ann.post("/api/ingest", {"text": "I need a second monitor for my desk.", "source_event_id": "d1"}).json()
    assert res["request_ids"] == [] and res["outcome"] == "refused" and ann.get("/api/requests").json() == []
    assert any("Equipment requests are not handled" in m and "Owner of Acme Ltd" in m for m in replies(ann))
    assert "disabled_type_refused" in [e["type"] for e in owner.engine.s.all("SELECT type FROM events")]
    mixed = ann.post("/api/ingest", {"text": "I need a second monitor and VPN is not connecting since morning.", "source_event_id": "d2"}).json()
    assert [owner.engine.req(i)["type_code"] for i in mixed["request_ids"]] == ["it_help"]  # the other item still goes through
    owner.post("/api/setup/types/equipment", {"enabled": True})
    assert ann.post("/api/ingest", {"text": "I need a second monitor for my desk.", "source_event_id": "d3"}).json()["outcome"] == "created"


def test_payroll_is_always_blocked_and_the_contact_is_the_one_you_set(team):
    owner, it, boss, ann, bob = team
    ask = lambda eid: ann.post("/api/ingest", {"text": "Why is my salary lower this month?", "source_event_id": eid}).json()
    assert ask("p1")["outcome"] == "blocked_sensitive" and "contact HR privately" in replies(ann)[0]  # nobody holds HR yet: a generic answer
    owner.post("/api/setup/roles/hr_lead", {"holder_id": boss.uid})
    ask("p2")
    assert "contact Bo Boss privately" in replies(ann)[0]
    assert owner.post("/api/setup/types/payroll", {"redirect": f"person:{it.uid}"}).status_code == 200
    ask("p3")
    assert "contact Ira IT privately" in replies(ann)[0]
    assert owner.post("/api/setup/types/payroll", {"enabled": False}).status_code == 422 and ann.get("/api/requests").json() == []


def test_policy_answers_come_from_the_company_and_balances_are_never_invented(team):
    owner, it, boss, ann, bob = team
    ask = lambda text, eid: (ann.post("/api/ingest", {"text": text, "source_event_id": eid}), replies(ann)[0])[1]
    assert "don't have that policy" in ask("What is the work from home policy?", "q1")  # Acme's wording never leaks into a new company
    assert "can't see leave balances yet" in ask("How many leaves do I have left?", "q2") and "6" not in replies(ann)[0]
    owner.post("/api/setup/policies", {"title": "Work from home", "text": "Three days a week with manager approval."})
    assert "Three days a week with manager approval." in ask("What is the work from home policy?", "q3")
    rid = ann.post("/api/ingest", {"text": "I need Friday and Monday off, family function.", "source_event_id": "q4"}).json()["request_ids"][0]
    assert "balance" not in owner.engine.req(rid)["details"]  # the approver is not shown figures nobody recorded
    demo = Browser(owner.main)
    demo.demo("e01")
    demo.post("/api/ingest", {"text": "How many leaves do I have left?", "source_event_id": "q5"})
    assert "Your leave balance" in [m["body"] for m in demo.get("/api/outbox").json()][0]  # the demo keeps its prepared balances


def test_an_edited_backup_is_who_a_late_request_escalates_to_and_a_routed_request_keeps_its_chain(team):
    owner, it, boss, ann, bob = team
    rid = ann.post("/api/ingest", {"text": "Need a Figma seat for design", "source_event_id": "x1"}).json()["request_ids"][0]
    assert chain_of(owner, rid) == [owner.uid]
    assert owner.post("/api/setup/types/software_seat", {"approver_rule": f"person:{it.uid}", "backup_rule": f"person:{boss.uid}", "reminder_hours": 1, "escalate_hours": 2}).status_code == 200
    assert chain_of(owner, rid) == [owner.uid]  # already routed: the chain does not move under the approver
    rid2 = bob.post("/api/ingest", {"text": "Need a Notion seat for planning", "source_event_id": "x2"}).json()["request_ids"][0]
    assert chain_of(owner, rid2) == [it.uid]
    owner.engine.advance_hours(3)
    owner.engine.sweep()
    assert owner.engine.req(rid2)["escalated_to"] == boss.uid  # the backup we named
    assert boss.post(f"/api/requests/{rid2}/approve").status_code == 200  # and they can decide it
    assert owner.engine.req(rid)["escalated_to"] == boss.uid and chain_of(owner, rid) == [owner.uid]  # reminders and escalation use the current hours; the chain itself stayed
