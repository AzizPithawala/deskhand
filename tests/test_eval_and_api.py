import json
import os
from datetime import date

from fastapi.testclient import TestClient

from app import chaos, classifier

DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SAMPLES = json.load(open(os.path.join(DATA, "sample_requests.json")))["requests"]


def test_classification_eval_on_sample_file():
    """NOTE: the rules were tuned while looking at this file, so this score is optimistic. Use real requests to measure."""
    ok = tot = 0
    for r in SAMPLES:
        if r["category"] not in ("known", "multi_intent", "blocked"):
            continue
        got = {i["type_code"] for i in classifier.classify_rules(r["text"], today=date(2026, 10, 21))["intents"]}
        tot += 1
        ok += got == set(r["expected_types"])
    print(f"classification accuracy {ok}/{tot}")
    assert ok / tot >= 0.9


def test_adversarial_and_ambiguous_samples_are_handled_safely(e):
    for r in SAMPLES:
        if r["category"] not in ("adversarial", "ambiguous"):
            continue
        out = e.ingest(r["channel"], r["sender"], r["text"])
        h = r["expected_handling"]
        if h == "refuse_unknown_sender":
            assert out["outcome"] == "unknown_sender_refused"
        elif h == "block":
            assert out["outcome"] == "blocked_sensitive"
        elif h == "clarify":
            assert out["outcome"] == "needs_info"
        for rid in out["request_ids"]:
            assert e.req(rid)["status"] in ("PENDING_APPROVAL", "NEEDS_INFO", "FULFILLING")  # never approved or closed
    blob = json.dumps(e.s.all("SELECT * FROM events") + e.s.all("SELECT * FROM outbox") + e.s.all("SELECT * FROM requests"))
    assert "Hunter2" not in blob
    assert not e.hr.leaves  # nothing was applied anywhere


def test_all_chaos_scenarios_hold(e):
    for name, fn in chaos.SCENARIOS.items():
        res = fn(e)
        assert res["passed"], (name, res["checks"])
    assert len(e.hr.leaves) == 3  # exactly one effect per HR scenario


def test_api_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKHAND_DB", str(tmp_path / "t.db"))
    import importlib
    from app import main
    importlib.reload(main)
    c = TestClient(main.app)
    assert "Deskhand" in c.get("/").text
    assert len(c.get("/api/state").json()["employees"]) == 10
    r = c.post("/api/ingest", json={"channel": "chat", "sender": "e01", "text": "Need a Claude seat for my work on the research project."}).json()
    rid = r["request_ids"][0]
    assert c.post(f"/api/requests/{rid}/approve", json={"actor_id": "e01"}).status_code == 403
    assert c.post(f"/api/requests/{rid}/reject", json={"actor_id": "e06", "reason": ""}).status_code == 422
    assert c.get("/api/inbox/e06").json()[0]["inbox_kind"] == "approval"
    assert c.post(f"/api/requests/{rid}/approve", json={"actor_id": "e06"}).status_code == 200
    assert c.post(f"/api/requests/{rid}/approve", json={"actor_id": "e06"}).status_code == 409
    assert c.post(f"/api/requests/{rid}/done", json={"actor_id": "e06"}).json()["status"] == "CLOSED"
    d = c.get("/api/dashboard").json()
    assert d["by_status"]["CLOSED"] == 1 and d["guard"]["unauthorized_action_refused"] == 1
    assert c.post("/api/chaos/duplicate_x4").json()["passed"]
    assert c.post("/api/clock/advance", json={"hours": 30}).status_code == 200
    assert c.get("/api/digest/e06").json()["person"] == "Imran Khan"
    assert c.post("/api/reset").json()["ok"] and c.get("/api/dashboard").json()["total"] == 0
