"""After-approval steps through the HTTP API: browsing the catalog, saving steps, connecting an app, pickers. Admin-only, validated, audited, isolated, no ids or secrets out
(rules 8, 11, 12, 13, 15, 21). The catalog is the recorded fixture and viaSocket is a fake transport (rule 7)."""
import copy
import json

import pytest

from app import steps
from app import viasocket_adapter as vs
from app import viasocket_catalog as vcat
from tests import apiutil
from tests.apiutil import Browser
from tests.test_viasocket_adapter import AUTH, SCRIPT, SECRET, FakeTransport, ok

FIX = json.load(open(vcat.FIXTURE))
ADD_ID = next(a["action_id"] for a in FIX["apps"]["google-sheets"]["actions"] if a["name"] == "Add New Row to Sheet")
ADD_VERSION = next(a["action_version_id"] for a in FIX["apps"]["google-sheets"]["actions"] if a["name"] == "Add New Row to Sheet")
DELETE_ID = next(a["action_id"] for a in FIX["apps"]["google-sheets"]["actions"] if a["name"] == "Delete Rows")
ROW = {"spreadsheet_Id": "sheet-1", "grid_Id": "0", "column_key": True, "column_selected": ["Name", "Email"], "column_name": {"Name": "{requester.name}", "Email": "{requester.email}"}}
STEP = {"app": "google-sheets", "action_id": ADD_ID, "run_mode": "auto", "label": "Add a row", "input": ROW}
GETS = ("/api/setup/steps/software_seat", "/api/steps/apps?q=sheet", "/api/steps/apps/google-sheets", f"/api/steps/apps/google-sheets/actions/{ADD_ID}", f"/api/steps/apps/google-sheets/connect?actions={ADD_ID}")
POSTS = (("/api/setup/steps/software_seat", {"steps": []}), ("/api/setup/steps/software_seat/1/enabled", {"enabled": False}),
         ("/api/steps/form-state", {"type_code": "software_seat", "app": "google-sheets", "action_id": ADD_ID, "values": {}}),
         ("/api/steps/options", {"app": "google-sheets", "action_id": ADD_ID, "field_key": "spreadsheet_Id"}), ("/api/steps/apps/google-sheets/connected", {"auth_id": "x"}),
         ("/api/steps/apps/google-sheets/disconnect", {}), ("/api/setup/steps/software_seat/1/test", {"confirm": True}))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("VIASOCKET_ORG_ID", "org1")
    monkeypatch.setenv("VIASOCKET_PROJECT_ID", "proj1")
    monkeypatch.setenv("VIASOCKET_EMBED_SECRET", SECRET)
    monkeypatch.setenv("VIASOCKET_API_URL", "https://api.test.invalid")
    monkeypatch.setenv("VIASOCKET_RUN_URL", "https://run.test.invalid")


@pytest.fixture
def main(tmp_path, monkeypatch):
    return apiutil.load_app(monkeypatch, tmp_path)


@pytest.fixture
def owner(main):
    return apiutil.company(main)


@pytest.fixture
def fake(monkeypatch):
    """viaSocket as a fake transport the test can script: t.answers, t.calls."""
    t = FakeTransport(ok({"success": True, "data": []}))
    real = vs.VsClient
    monkeypatch.setattr(vs, "VsClient", lambda tenant: real(tenant, t))
    return t


def connect(b, slug="google-sheets"):
    vs.ConnectionStore(b.engine.s, b.org_id).save(slug, AUTH, SCRIPT)


def events(b, kind):
    return [json.loads(e["data_json"]) for e in b.engine.s.all("SELECT data_json FROM events WHERE type=?", (kind,))]


def test_members_and_signed_out_visitors_are_refused_every_steps_route_and_a_write_needs_the_csrf_token(owner, main):
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee")
    for url in GETS:
        assert ann.get(url).status_code == 403, url
        assert Browser(main).get(url).status_code == 401, url
        assert owner.get(url).status_code in (200, 409), url
    for url, body in POSTS:
        assert ann.post(url, body).status_code == 403, url
        assert Browser(main).post(url, body).status_code == 401, url
    good, owner.csrf = owner.csrf, None
    for url, body in POSTS:
        assert owner.post(url, body).status_code == 403, url
    owner.csrf = good


def test_the_demo_company_can_see_the_steps_page_but_nothing_else_here(main):
    demo = Browser(main)
    assert demo.demo("e10").status_code == 200
    v = demo.get("/api/setup/steps/software_seat").json()
    assert v["demo"] is True and v["eligible"] and v["steps"] == []
    for url in GETS[1:]:
        assert demo.get(url).status_code == 403, url  # it makes no call to the catalog or viaSocket
    for url, body in POSTS:
        assert demo.post(url, body).status_code == 403, url
    assert demo.get("/api/setup/steps/leave").json()["eligible"] is False


def test_the_catalog_is_browsable_and_never_shows_version_or_service_ids(owner):
    r = owner.get("/api/steps/apps?q=sheet").json()
    assert [a["slug"] for a in r["apps"]] == ["google-sheets"] and r["apps"][0]["connected"] is False
    app = owner.get("/api/steps/apps/google-sheets")
    a = app.json()
    assert a["name"] == "Google Sheets" and a["auth_type"] == "Auth2.0" and len(a["actions"]) == 8
    assert {x["name"] for x in a["actions"] if x["destructive"]} == {"Delete Rows", "Clear Spreadsheet Row", "Permanently Delete Spreadsheet"}
    act = owner.get(f"/api/steps/apps/google-sheets/actions/{ADD_ID}")
    j = act.json()
    assert [f["key"] for f in j["schema"]["fields"]][:2] == ["spreadsheet_Id", "grid_Id"] and j["action"]["supported"] and j["sample"]["success"] is True
    for text in (app.text, act.text):
        assert "action_version_id" not in text and "service_id" not in text and "fixture-google-sheets" not in text
    assert owner.get("/api/steps/apps/nope").status_code == 404
    assert owner.get("/api/steps/apps/bad%20slug").status_code == 404
    assert owner.get("/api/steps/apps/google-sheets/actions/nope").status_code == 404
    assert owner.get("/api/steps/apps?q=").json() == {"apps": []}


def test_a_catalog_that_is_down_is_a_readable_503_not_a_crash(owner):
    class Down(vcat.Catalog):
        def search(self, text):
            raise vcat.CatalogError("The app catalog is not available right now.")

        def app(self, slug):
            raise vcat.CatalogError("The app catalog is not available right now.")
    vcat.set_catalog(Down())
    for url in ("/api/steps/apps?q=x", "/api/steps/apps/google-sheets", f"/api/steps/apps/google-sheets/actions/{ADD_ID}"):
        r = owner.get(url)
        assert r.status_code == 503 and r.json()["detail"] == "The app catalog is not available right now.", url


def test_form_state_says_what_is_visible_what_is_missing_and_what_is_wrong(owner):
    def state(values, code="software_seat"):
        return owner.post("/api/steps/form-state", {"type_code": code, "app": "google-sheets", "action_id": ADD_ID, "values": values}).json()
    s = state({})
    assert s["visible"] == ["spreadsheet_Id"] and "spreadsheet_Id" in s["errors"]  # the sheet and the rest appear once a spreadsheet is chosen
    s = state({"spreadsheet_Id": "x", "grid_Id": "0"})
    assert s["visible"] == ["spreadsheet_Id", "grid_Id", "column_key", "column_selected"]  # the form's own default for "first row has column names" lets the columns appear
    assert s["cleaned"]["column_key"] is True
    assert state({"spreadsheet_Id": "x", "grid_Id": "0", "column_selected": ["Name"]})["visible"][-1] == "column_name"
    s = state({**ROW, "column_name": {"Name": "{raw_text}"}})
    assert "not a request field" in s["errors"]["column_name"] and s["cleaned"].get("column_name") is None
    assert state(ROW)["errors"] == {}
    assert owner.post("/api/steps/form-state", {"type_code": "leave", "app": "google-sheets", "action_id": ADD_ID, "values": {}}).status_code == 422


def test_saving_steps_is_validated_audited_and_leaves_no_secret(owner):
    r = owner.post("/api/setup/steps/software_seat", {"steps": [STEP], "close_when_done": False})
    assert r.status_code == 200
    v = r.json()
    assert v["eligible"] and v["steps"][0]["app_name"] == "Google Sheets" and v["steps"][0]["tested"] is False and v["steps"][0]["enabled"] is False and v["steps"][0]["connected"] is False
    ev = events(owner, "setup_changed")[-1]
    assert ev["area"] == "steps" and ev["code"] == "software_seat" and ev["changes"] == {"steps": [[], ["Google Sheets: Add New Row to Sheet (auto)"]]}
    n = len(events(owner, "setup_changed"))
    assert owner.post("/api/setup/steps/software_seat", {"steps": [dict(STEP, id=v["steps"][0]["id"])]}).status_code == 200
    assert len(events(owner, "setup_changed")) == n  # nothing changed, so nothing is audited
    blob = json.dumps(owner.engine.s.all("SELECT data_json FROM events")) + r.text
    assert SECRET not in blob and "action_version_id" not in r.text


@pytest.mark.parametrize("code,body,status,text", [
    ("leave", {"steps": [STEP]}, 422, "handled by Deskhand itself"),
    ("payroll", {"steps": [STEP]}, 422, "handled by Deskhand itself"),
    ("nope", {"steps": [STEP]}, 404, "No such request type"),
    ("software_seat", {"steps": [dict(STEP, run_mode="whenever")]}, 422, "Choose whether"),
    ("software_seat", {"steps": [dict(STEP, action_id=DELETE_ID, input={})]}, 422, "presses Run"),
    ("software_seat", {"steps": [STEP] * 4}, 422, "up to 3"),
    ("software_seat", {"steps": [dict(STEP, app="nope")]}, 422, "not in the catalog"),
    ("software_seat", {"steps": [dict(STEP, surprise=1)]}, 422, None),  # an unknown field is refused, never ignored
    ("software_seat", {"steps": [STEP], "who": "me"}, 422, None),
    ("software_seat", {"steps": [dict(STEP, input={**ROW, "column_name": {"Name": "{request.raw_text}"}})]}, 422, "not a request field"),
])
def test_a_step_that_is_not_allowed_is_refused_with_the_reason_and_nothing_is_stored(owner, code, body, status, text):
    r = owner.post(f"/api/setup/steps/{code}", body)
    assert r.status_code == status, r.text
    if text:
        assert text in json.dumps(r.json())
    assert owner.engine.s.one("SELECT COUNT(*) c FROM type_steps")["c"] == 0


def test_a_step_is_switched_on_only_after_a_test_and_only_by_an_admin(owner):
    sid = owner.post("/api/setup/steps/software_seat", {"steps": [STEP]}).json()["steps"][0]["id"]
    r = owner.post(f"/api/setup/steps/software_seat/{sid}/enabled", {"enabled": True})
    assert r.status_code == 422 and "test" in r.json()["detail"]
    row = owner.engine.s.one("SELECT * FROM type_steps")
    owner.engine.s.q("UPDATE type_steps SET tested_hash=?, tested_at=1.0 WHERE id=?", (steps.step_hash(row["app_slug"], row["action_id"], row["action_version_id"], ROW), sid))
    v = owner.post(f"/api/setup/steps/software_seat/{sid}/enabled", {"enabled": True}).json()
    assert v["steps"][0]["enabled"] is True and v["steps"][0]["tested"] is True
    assert events(owner, "setup_changed")[-1]["changes"]["enabled"] == [False, True]
    assert owner.post("/api/setup/steps/software_seat/9999/enabled", {"enabled": False}).status_code == 404


def test_connecting_an_app_keeps_every_id_on_the_server_and_rejects_a_connection_that_is_not_the_companys(owner, fake):
    r = owner.get(f"/api/steps/apps/google-sheets/connect?actions={ADD_ID}").json()
    assert r["token"].count(".") == 2 and SECRET not in r["token"] and r["service_id"] == "fixture-google-sheets" and r["action_ids"] == [ADD_ID]
    assert owner.get("/api/steps/apps/google-sheets/connect").status_code == 422
    assert owner.get("/api/steps/apps/google-sheets/connect?actions=nope").status_code == 422
    assert owner.post("/api/steps/apps/google-sheets/connected", {"auth_id": "someone-elses"}).status_code == 422
    assert owner.post("/api/steps/apps/google-sheets/connected", {}).status_code == 422
    fake.answers = [ok({"success": True, "data": [{"id": AUTH, "isExpired": False}]}), ok({"success": True, "data": {"flows": []}}), ok({"success": True, "data": {"script_id": SCRIPT}})]
    fake.calls.clear()
    assert owner.post("/api/steps/apps/google-sheets/connected", {"auth_id": AUTH}).json() == {"ok": True}
    assert any(u.endswith(f"/embed/enable/fixture-google-sheets/{AUTH}") for _, u, _, _ in fake.calls)
    row = vs.ConnectionStore(owner.engine.s, owner.org_id).get("google-sheets")
    assert row["auth_id"] == AUTH and row["script_id"] == SCRIPT
    sid = owner.post("/api/setup/steps/software_seat", {"steps": [STEP]})
    assert sid.json()["steps"][0]["connected"] is True
    blob = sid.text + owner.get("/api/steps/apps?q=sheet").text + owner.get("/api/steps/apps/google-sheets").text + json.dumps(owner.engine.s.all("SELECT data_json FROM events"))
    assert AUTH not in blob and SCRIPT not in blob and SECRET not in blob
    assert events(owner, "connection_added")[-1] == {"app": "google-sheets", "reused_runner": False}


def test_an_app_that_needs_no_sign_in_is_not_supported_yet(owner):
    data = copy.deepcopy(FIX)
    data["apps"]["google-sheets"]["requires_auth"] = False
    vcat.set_catalog(vcat.FakeCatalog(data))
    assert owner.get(f"/api/steps/apps/google-sheets/connect?actions={ADD_ID}").status_code == 422
    r = owner.post("/api/setup/steps/software_seat", {"steps": [STEP]})
    assert r.status_code == 422 and "no sign-in" in r.json()["detail"]


def test_pickers_need_a_connection_and_come_back_as_plain_items_without_ids(owner, fake):
    body = {"app": "google-sheets", "action_id": ADD_ID, "field_key": "spreadsheet_Id", "existing": {}, "search": "deskhand"}
    assert owner.post("/api/steps/options", body).status_code == 409  # not connected yet
    connect(owner)
    fake.answers = [ok({"data": [{"label": "DeskHand Spike", "value": "id-1"}, {"label": "Other", "value": "id-2"}], "offset": None})]
    r = owner.post("/api/steps/options", body).json()
    assert r == {"kind": "options", "items": [{"label": "DeskHand Spike", "value": "id-1"}, {"label": "Other", "value": "id-2"}], "more": False}
    sent = fake.calls[-1][3]
    assert fake.calls[-1][1].endswith("/embed/list-options/" + ADD_VERSION)  # the version the catalog published for this action, read from the catalog, not typed
    assert sent["fieldKey"] == "spreadsheet_Id" and sent["auth_id"] == AUTH and sent["existingFields"] == {"_searchText": "deskhand"}
    # the "Column Values" picker answers field definitions, and the key is what the action expects
    fake.answers = [ok([{"key": "Name", "label": "Name", "type": "string", "required": "True", "help": "Enter value for Name"}, {"key": "Email", "label": "Email", "type": "string", "required": "False"}])]
    r = owner.post("/api/steps/options", {**body, "field_key": "column_name", "existing": {"spreadsheet_Id": "id-1", "grid_Id": "0"}, "search": None}).json()
    assert r["kind"] == "fields" and [i["key"] for i in r["items"]] == ["Name", "Email"] and r["items"][0]["required"] is True and r["items"][1]["required"] is False
    fake.answers = [ok({"response": {"status": 400, "data": {"message": "Auth data not found"}}})]
    bad = owner.post("/api/steps/options", body)
    assert bad.status_code == 422 and AUTH not in bad.text and SCRIPT not in bad.text
    assert owner.post("/api/steps/options", {**body, "field_key": "not_a_field"}).status_code == 404
    assert owner.post("/api/steps/options", {**body, "existing": {"x": "y" * 9000}}).status_code == 422


def test_disconnecting_an_app_switches_off_the_steps_that_use_it_and_leaves_slack_and_gmail_to_their_own_page(owner, fake):
    connect(owner)
    sid = owner.post("/api/setup/steps/software_seat", {"steps": [STEP]}).json()["steps"][0]["id"]
    owner.engine.s.q("UPDATE type_steps SET enabled=1 WHERE id=?", (sid,))
    fake.answers = [ok({"success": True, "data": {}})]
    r = owner.post("/api/steps/apps/google-sheets/disconnect", {})
    assert r.json() == {"ok": True, "steps_switched_off": 1}
    assert vs.ConnectionStore(owner.engine.s, owner.org_id).get("google-sheets") is None
    assert owner.get("/api/setup/steps/software_seat").json()["steps"][0]["enabled"] is False
    assert events(owner, "connection_removed")[-1] == {"app": "google-sheets", "steps_switched_off": 1}
    assert owner.post("/api/steps/apps/slack/disconnect", {}).status_code == 422


def test_one_companys_steps_and_connections_are_invisible_to_another(main, owner, fake):
    connect(owner)
    owner.post("/api/setup/steps/software_seat", {"steps": [STEP]})
    other = apiutil.company(main, email="other@beta.test", code="code-2", name="Beta Ltd")
    v = other.get("/api/setup/steps/software_seat").json()
    assert v["steps"] == [] and other.get("/api/steps/apps?q=sheet").json()["apps"][0]["connected"] is False
    assert other.post("/api/steps/options", {"app": "google-sheets", "action_id": ADD_ID, "field_key": "spreadsheet_Id"}).status_code == 409
    mine = owner.engine.s.one("SELECT id FROM type_steps")["id"]
    assert other.post(f"/api/setup/steps/software_seat/{mine}/enabled", {"enabled": False}).status_code == 404  # another company's step id finds nothing here
    assert other.engine.s.one("SELECT COUNT(*) c FROM type_steps")["c"] == 0 and owner.engine.s.one("SELECT COUNT(*) c FROM type_steps")["c"] == 1


# ---------------- 5A.2: testing a step, running steps, and what the request page shows ----------------
GOOD = ok({"success": True, "data": {"Name": "Priya Sharma", "Email": "p@example.com", "_rowNumber": 2}})


def seed_step(owner, run_mode="auto", action_id=ADD_ID, row=ROW):
    r = owner.post("/api/setup/steps/software_seat", {"steps": [dict(STEP, run_mode=run_mode, action_id=action_id, input=row)]})
    assert r.status_code == 200, r.text
    return r.json()["steps"][0]["id"]


def team(owner):
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee")
    return ann, ann.post("/api/ingest", {"text": "Need a Claude seat for my research project"}).json()["request_ids"][0]


def test_a_test_run_needs_an_explicit_confirmation_an_admin_and_a_connected_app(owner, fake):
    sid = seed_step(owner)
    url = f"/api/setup/steps/software_seat/{sid}/test"
    ann = apiutil.add_person(owner, "ann@acme.test", "Ann Employee")
    assert ann.post(url, {"confirm": True}).status_code == 403 and Browser(owner.main).post(url, {"confirm": True}).status_code == 401
    r = owner.post(url, {})
    assert r.status_code == 422 and "really runs" in r.json()["detail"] and fake.calls == []  # nothing is sent without the explicit yes
    assert owner.post(url, {"confirm": True}).json()["outcome"] == "permanent_failure"  # not connected: a readable refusal, nothing learned
    assert owner.get("/api/setup/steps/software_seat").json()["steps"][0]["tested"] is False


def test_a_good_test_run_marks_the_step_tested_and_only_then_can_it_be_switched_on(owner, fake):
    connect(owner)
    sid = seed_step(owner)
    assert owner.post(f"/api/setup/steps/software_seat/{sid}/enabled", {"enabled": True}).status_code == 422
    fake.answers = [GOOD]
    r = owner.post(f"/api/setup/steps/software_seat/{sid}/test", {"confirm": True})
    j = r.json()
    assert r.status_code == 200 and j["outcome"] == "success" and j["tested"] and j["sent"]["column_name"]["Name"] == "Priya Sharma"  # made-up details, shown to the admin
    assert j["view"]["steps"][0]["tested"] is True and fake.calls[-1][3]["inputData"]["column_name"]["Email"] == "priya.sharma@example.com"
    assert owner.post(f"/api/setup/steps/software_seat/{sid}/enabled", {"enabled": True}).json()["steps"][0]["enabled"] is True
    ev = events(owner, "step_tested")[-1]
    assert ev["outcome"] == "success" and SCRIPT not in r.text and AUTH not in r.text and SECRET not in r.text
    assert "Priya" not in json.dumps(owner.engine.s.all("SELECT data_json FROM events"))  # the audit never carries the values


def test_a_destructive_step_is_tested_only_after_its_name_is_typed(owner, fake):
    connect(owner)
    sid = seed_step(owner, "confirm", DELETE_ID, {"spreadsheet_Id": "s", "grid_Id": "0", "deletion_mode": "specific", "row_numbers": "2"})
    url = f"/api/setup/steps/software_seat/{sid}/test"
    r = owner.post(url, {"confirm": True})
    assert r.status_code == 422 and "Delete Rows" in r.json()["detail"] and fake.calls == []
    fake.answers = [GOOD]
    assert owner.post(url, {"confirm": True, "typed": "Delete Rows"}).json()["outcome"] == "success"


def enable(owner, sid):
    row = owner.engine.s.one("SELECT * FROM type_steps WHERE id=?", (sid,))
    owner.engine.s.q("UPDATE type_steps SET tested_hash=?, tested_at=1.0, enabled=1, expect_json=? WHERE id=?",
                     (steps.step_hash(row["app_slug"], row["action_id"], row["action_version_id"], json.loads(row["input_json"])), json.dumps({"kind": "object", "keys": ["Name", "_rowNumber"]}), sid))


def test_the_request_page_shows_the_steps_and_their_values_only_to_an_admin_and_never_the_snapshot(owner):
    enable(owner, seed_step(owner, "confirm"))
    ann, rid = team(owner)
    assert owner.post(f"/api/requests/{rid}/approve", {}).status_code == 200
    mine = ann.get(f"/api/requests/{rid}").json()
    theirs = owner.get(f"/api/requests/{rid}").json()
    assert mine["steps"][0]["status"] == "ready" and mine["steps"][0]["status_text"] == "Waiting for someone to press Run" and "steps_json" not in mine["request"]
    assert "input" not in mine["steps"][0] and "input" in theirs["steps"][0]  # the requester sees progress; an admin also sees what is sent
    assert "action_version_id" not in json.dumps(mine) and SCRIPT not in json.dumps(theirs)


def test_pressing_run_asks_who_you_are_and_the_demo_and_signed_out_visitors_get_nothing(owner, main):
    enable(owner, seed_step(owner, "confirm"))
    ann, rid = team(owner)
    owner.post(f"/api/requests/{rid}/approve", {})
    url = f"/api/requests/{rid}/steps/0/run"
    assert Browser(main).post(url, {}).status_code == 401
    assert ann.post(url, {}).status_code == 403  # the requester is not who completes this kind of request
    good, owner.csrf = owner.csrf, None
    assert owner.post(url, {}).status_code == 403  # no CSRF token
    owner.csrf = good
    assert owner.post(url, {}).status_code == 200
    assert owner.get(f"/api/requests/{rid}").json()["steps"][0]["status"] == "rehearsed"  # a new company is in rehearsal: nothing was sent
    assert owner.post(url, {}).status_code == 409  # pressing it twice does nothing


def test_an_admin_decides_what_happens_to_a_parked_step_and_others_cannot(owner, fake):
    connect(owner)
    enable(owner, seed_step(owner))
    owner.engine.s.set_setting("delivery_mode", "live")
    fake.answers = [vs_timeout()]
    ann, rid = team(owner)
    owner.post(f"/api/requests/{rid}/approve", {})
    owner.engine.stepper.run_pending()
    assert owner.get(f"/api/requests/{rid}").json()["steps"][0]["status"] == "unknown" and ann.get(f"/api/requests/{rid}").json()["request"]["status"] == "PARKED"
    for action, body in (("again", {}), ("mark-done", {"reason": "checked the sheet"})):
        assert ann.post(f"/api/requests/{rid}/steps/0/{action}", body).status_code == 403
    assert owner.post(f"/api/requests/{rid}/steps/0/mark-done", {"reason": ""}).status_code == 422
    assert owner.post(f"/api/requests/{rid}/steps/0/mark-done", {"reason": "Row 2 is in the sheet"}).status_code == 200
    assert owner.get(f"/api/requests/{rid}").json()["steps"][0]["status"] == "succeeded"
    assert events(owner, "step_marked_done")[-1]["reason"] == "Row 2 is in the sheet" and len(fake.calls) == 1  # no second call


def vs_timeout():
    return vs.TransportError("ReadTimeout")


def test_the_background_worker_runs_queued_steps_of_every_live_company(owner, fake):
    connect(owner)
    enable(owner, seed_step(owner))
    owner.engine.s.set_setting("delivery_mode", "live")
    fake.answers = [GOOD]
    ann, rid = team(owner)
    owner.post(f"/api/requests/{rid}/approve", {})
    assert owner.get(f"/api/requests/{rid}").json()["steps"][0]["status"] == "pending" and fake.calls == []  # approving only queues it
    owner.main._worker_pass(1)  # what the worker does every ten seconds
    assert owner.get(f"/api/requests/{rid}").json()["steps"][0]["status"] == "succeeded" and len(fake.calls) == 1
    owner.main._worker_pass(2)
    assert len(fake.calls) == 1


def test_a_failing_round_is_logged_without_message_text_and_does_not_stop_the_worker(owner, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(type(owner.engine.stepper), "run_pending", lambda self, limit=3: (_ for _ in ()).throw(RuntimeError("secret text about Priya")))
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        owner.main._worker_pass(1)
    assert "RuntimeError" in caplog.text and "secret text" not in caplog.text and "Priya" not in caplog.text
