"""Outbound live delivery: safeguards, separation from approvals, exactly-once. Fake transport only; nothing touches the network."""
import json
import threading
import time

import pytest

from app import chaos, delivery as dl, viasocket_adapter as vs
from app.engine import Engine

SCRIPT_S, SCRIPT_G = "scriSLACKRUNNER1", "scriGMAILRUNNER1"
OVERRIDE = "demo-inbox@example.test"
SLACK_OK = (200, json.dumps({"ok": True, "ts": "1.2", "channel": "C1", "message": {"metadata": {"event_payload": {"script_id": SCRIPT_S}}}}))
GMAIL_OK = (200, json.dumps({"success": True, "data": {"id": "m1", "threadId": "t1"}}))


class Fake:
    """Answers by which runner is called; records every call. `plan` can inject failures per app."""
    def __init__(self):
        self.calls, self.plan = [], {"slack": [], "gmail": []}

    def request(self, method, url, headers=None, body=None, timeout=25):
        app = "slack" if SCRIPT_S in url else "gmail"
        self.calls.append((app, body))
        if self.plan[app]:
            a = self.plan[app].pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        return SLACK_OK if app == "slack" else GMAIL_OK


@pytest.fixture
def env(monkeypatch):
    for k, v in {"VIASOCKET_ORG_ID": "o", "VIASOCKET_PROJECT_ID": "p", "VIASOCKET_EMBED_SECRET": "secret-value-for-tests-0123456789",
                 "VIASOCKET_API_URL": "https://api.test.invalid", "VIASOCKET_RUN_URL": "https://run.test.invalid",
                 "DESKHAND_LIVE_EMAIL_OVERRIDE": OVERRIDE}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def live(env):
    e = Engine(api_key="")
    cs = vs.ConnectionStore(e.s, "acme-demo")
    cs.save("slack", "auth-slack-000001", SCRIPT_S)
    cs.set_setting("slack", "channel_id", "C123")
    cs.save("gmail", "auth-gmail-000001", SCRIPT_G)
    fake = Fake()
    d = dl.Delivery(e, lambda: vs.ViaSocketIntegration(e.s, "acme-demo", vs.VsClient("acme-demo", fake)), base_url="http://deskhand.test")
    e.live = d
    d.set_toggle(True, "e10")
    return e, d, fake


def rows(e, **where):
    sql = "SELECT * FROM outbox" + ("".join(f" WHERE {k}=?" if i == 0 else f" AND {k}=?" for i, k in enumerate(where)))
    return e.s.all(sql + " ORDER BY id", tuple(where.values()))


def test_allow_list_is_exactly_the_agreed_kinds():
    assert dl.LIVE_KINDS == {"approval_request", "approved", "rejected", "fulfilled", "escalation_1", "escalation_2", "parked"} and dl.DAILY_CAP == 40


def test_off_by_default_and_without_a_delivery_object_nothing_goes_live(env):
    e = Engine(api_key="")
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    assert {r["delivery_status"] for r in rows(e)} == {"skipped"}
    e.live = dl.Delivery(e, lambda: vs.MockIntegration())
    assert e.live.toggle() is False and e.live.status_for("approval_request", "chat") == "skipped"


def test_only_allowed_kinds_wait_for_delivery_and_the_rest_stay_in_the_outbox(live):
    e, d, fake = live
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    e.approve(rid, "e06")
    e.done(rid, "e06")
    by_kind = {}
    for r in rows(e):
        by_kind.setdefault(r["kind"], set()).add(r["delivery_status"])
    assert by_kind["approval_request"] == {"pending"} and by_kind["approved"] == {"pending"} and by_kind["fulfilled"] == {"pending"}
    for k in ("submitted", "task"):
        assert by_kind[k] == {"skipped"}
    d.deliver_pending()
    assert len(fake.calls) == 3 and all(r["delivery_status"] in ("delivered", "skipped") for r in rows(e))


def test_slack_message_has_prefix_link_and_neutralised_text(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat. <!channel> <@U123> & <http://evil.test|click> please")
    d.deliver_pending()
    app, body = fake.calls[0]
    text = body["inputData"]["markdown_content"]
    assert app == "slack" and text.startswith("[Deskhand demo to Imran Khan]")
    assert "<!channel>" not in text and "<@U123>" not in text and "<http" not in text and "&lt;!channel&gt;" in text
    assert "http://deskhand.test/#inbox/e06" in text
    assert body["inputData"]["destination"]["channel_id"] == ["C123"]


def test_employee_messages_have_no_inbox_link(live):
    e, d, fake = live
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    e.reject(rid, "e06", "No budget")
    d.deliver_pending()
    texts = [c[1]["inputData"]["markdown_content"] for c in fake.calls]
    emp = [t for t in texts if t.startswith("[Deskhand demo to Priya Sharma]")][0]
    assert "rejected" in emp and "#inbox" not in emp


def test_approval_succeeds_even_if_slack_is_down_and_delivery_is_parked_not_lost(live):
    e, d, fake = live
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    fake.plan["slack"] = [vs.TransportError("ReadTimeout")] * 10
    e.approve(rid, "e06")  # no delivery happens in the approval path
    assert e.req(rid)["status"] == "FULFILLING" and not fake.calls
    d.deliver_pending()
    assert e.req(rid)["status"] == "FULFILLING"  # a delivery failure never touches the request
    failed = rows(e, delivery_status="failed")
    assert failed and "outcome unknown" in failed[0]["delivery_error"]
    assert any(r["kind"] == "delivery_failed" and r["delivery_status"] == "skipped" for r in rows(e))  # admin told, not live
    e.live.retry_failed(failed[0]["id"], "e10")
    fake.plan["slack"] = []
    d.deliver_pending()
    assert rows(e, id=failed[0]["id"])[0]["delivery_status"] == "delivered"


def test_exactly_once_per_idempotency_key(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    d.deliver_pending()
    n = len(fake.calls)
    e.s.q("UPDATE outbox SET delivery_status='pending' WHERE kind='approval_request'")  # even if a row were wrongly reset, a key that succeeded is never resent
    e.s.q("UPDATE outbox SET delivery_status='pending' WHERE kind='submitted'")  # and a kind that is not allowed live is never sent, even if marked pending
    d.deliver_pending()
    assert len(fake.calls) == n
    assert [r["delivery_status"] for r in rows(e) if r["kind"] == "approval_request"] == ["delivered"]


def test_safe_failures_retry_with_backoff_unsafe_ones_do_not(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    fake.plan["slack"] = [vs.TransportError("ConnectError"), (200, json.dumps({"ok": False, "error": "ratelimited"}))]
    assert d.deliver_pending()["retry"] == 1
    assert d.deliver_pending()["delivered"] == 0  # backoff not elapsed yet
    e.advance_hours(1)
    assert d.deliver_pending()["retry"] == 1
    e.advance_hours(1)
    assert d.deliver_pending()["delivered"] == 1 and len(fake.calls) == 3
    e.ingest("chat", "e02", "Need a Figma seat for design")
    fake.plan["slack"] = [(502, "{}")]  # a 5xx may have been delivered already: never auto-retry
    out = d.deliver_pending()
    assert out["failed"] == 1 and out["retry"] == 0


def test_permanent_failure_is_parked_with_audit(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    fake.plan["slack"] = [(200, json.dumps({"ok": False, "error": "channel_not_found"}))]
    assert d.deliver_pending()["failed"] == 1
    types = [x["type"] for x in e.s.all("SELECT type FROM events")]
    assert "delivery_failed" in types and e.dashboard()["guard"]["delivery_failed"] == 1


def test_daily_cap_stops_live_delivery_and_a_reset_cannot_clear_it(live):
    e, d, fake = live
    for i in range(dl.DAILY_CAP):
        e.s.q("INSERT INTO delivery_ledger(day,ikey,ts) VALUES(?,?,?)", (dl._today(), f"x{i}", 0))
    assert d.capped() and d.summary()["capped"]
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    assert {r["delivery_status"] for r in rows(e, kind="approval_request")} == {"skipped"}  # written after the cap: never pending
    e.s.q("UPDATE outbox SET delivery_status='pending'")
    d.deliver_pending()
    assert not fake.calls and d.toggle() is False  # cap hit: delivery stopped and switched off
    assert e.dashboard()["delivery"]["capped"]
    e.reset()
    assert d.used_today() == dl.DAILY_CAP and d.toggle() is False
    with pytest.raises(ValueError):
        e.s.q("INSERT INTO outbox(request_id,channel,to_person,subject,body,kind,idempotency_key,created_at,delivery_status) VALUES(1,'chat','e01','s','b','rejected','k',0,'failed')")
        d.retry_failed(1, "e10")


def test_reaching_the_cap_midway_never_exceeds_it(live):
    e, d, fake = live
    for i in range(dl.DAILY_CAP - 1):
        e.s.q("INSERT INTO delivery_ledger(day,ikey,ts) VALUES(?,?,?)", (dl._today(), f"x{i}", 0))
    for emp in ("e01", "e02", "e03"):
        e.ingest("chat", emp, "Need a Claude seat for my research project")
    d.deliver_pending()
    assert len(fake.calls) == 1 and d.used_today() == dl.DAILY_CAP and d.toggle() is False


def test_email_goes_only_to_the_single_override_address(live):
    e, d, fake = live
    e.ingest("email", "e01", "Subject: Equipment\nNeed a second monitor")
    d.deliver_pending()
    gm = [c for c in fake.calls if c[0] == "gmail"]
    assert gm and all(c[1]["inputData"]["to"] == OVERRIDE for c in gm)
    assert not any("acme-demo.example" in json.dumps(c[1]) for c in gm)
    assert gm[0][1]["inputData"]["subject"].startswith("[Deskhand demo to ")


def test_email_is_never_sent_without_a_valid_single_override(live, monkeypatch):
    e, d, fake = live
    for bad in ("", "a@b.co, c@d.co", "not-an-address"):
        monkeypatch.setenv("DESKHAND_LIVE_EMAIL_OVERRIDE", bad)
        e.s.q("DELETE FROM outbox")
        e.s.q("DELETE FROM action_runs")
        e.ingest("email", "e01", "Subject: Equipment\nNeed a second monitor", source_event_id="ev-" + bad)
        d.deliver_pending()
        assert not [c for c in fake.calls if c[0] == "gmail"]
        assert rows(e, delivery_status="failed")[0]["delivery_error"].startswith("no single live email address")


def test_forced_mock_blocks_everything_live_including_chaos(live):
    e, d, fake = live
    with dl.forced_mock(e):
        e.ingest("chat", "e01", "Need a Claude seat for my research project")
        assert {r["delivery_status"] for r in rows(e)} == {"skipped"} and d.active() is False
        for name, fn in chaos.SCENARIOS.items():
            assert fn(e)["passed"], name
    assert not fake.calls and not rows(e, delivery_status="pending")


def test_switching_off_clears_what_was_waiting(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    d.set_toggle(False, "e10")
    d.deliver_pending()
    assert not fake.calls and not rows(e, delivery_status="pending")


def test_nothing_secret_leaks_into_outbox_audit_or_dashboard(live):
    e, d, fake = live
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    fake.plan["slack"] = [(200, json.dumps({"ok": False, "error": f"boom {SCRIPT_S}"}))]
    d.deliver_pending()
    e.approve(rid, "e06")
    d.deliver_pending()
    blob = json.dumps([rows(e), e.s.all("SELECT * FROM events"), e.dashboard(), d.summary()])
    for secret in (SCRIPT_S, SCRIPT_G, "auth-slack-000001", "secret-value-for-tests"):
        assert secret not in blob


def test_web_api_toggle_status_and_reset(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import importlib
    monkeypatch.setenv("DESKHAND_DB", str(tmp_path / "t.db"))
    from app import main
    importlib.reload(main)
    c = TestClient(main.app)
    assert c.get("/api/delivery", params={"actor_id": "e01"}).status_code == 403
    assert c.post("/api/delivery/toggle", json={"actor_id": "e10", "on": True}).status_code == 409  # mock mode
    d = c.get("/api/delivery", params={"actor_id": "e10"}).json()
    assert d["toggle"] is False and d["cap"] == 40 and d["email_to"] == "d***@example.test" and OVERRIDE not in json.dumps(d)
    assert c.get("/api/clock").json()["live"]["on"] is False
    monkeypatch.setenv("DESKHAND_INTEGRATION", "viasocket")
    assert c.post("/api/delivery/toggle", json={"actor_id": "e10", "on": True}).status_code == 409  # Slack not connected yet
    main.engine.s.set_setting("live_delivery", "on")
    c.post("/api/reset")
    assert main.engine.live.toggle() is False  # Reset demo puts delivery back to mock


class SlowFake(Fake):
    """Thread-safe, and slow enough that two sweeps really overlap."""
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()

    def request(self, method, url, headers=None, body=None, timeout=25):
        time.sleep(0.15)
        with self.lock:
            return super().request(method, url, headers, body, timeout)


def _slow(live):
    e, d, _ = live
    slow = SlowFake()
    d.factory = lambda: vs.ViaSocketIntegration(e.s, "acme-demo", vs.VsClient("acme-demo", slow))
    return e, d, slow


def _race(n, fn):
    barrier, out = threading.Barrier(n), []

    def run():
        barrier.wait()
        out.append(fn())

    ts = [threading.Thread(target=run) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def test_two_concurrent_sweeps_send_each_row_once_and_one_of_them_skips(live):
    e, d, slow = _slow(live)
    for emp in ("e01", "e02", "e03"):
        e.ingest("chat", emp, "Need a Claude seat for my research project")
    live_rows = len(rows(e, delivery_status="pending"))
    results = _race(2, d.deliver_pending)
    assert sum(r["delivered"] for r in results) == live_rows == len(slow.calls)
    assert sorted(bool(r.get("busy")) for r in results) == [False, True]  # single-flight: the second sweep skipped itself
    assert d.used_today() == live_rows and not rows(e, delivery_status="sending")


def test_atomic_claim_alone_prevents_double_send_even_without_single_flight(live):
    e, d, slow = _slow(live)
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    row = rows(e, delivery_status="pending")[0]  # every racer holds the same pending snapshot
    results = _race(8, lambda: d._deliver_one(row))
    assert results.count("delivered") == 1 and results.count("skipped") == 7 and len(slow.calls) == 1


def test_a_stale_snapshot_after_a_retry_cannot_collide_in_the_ledger(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    stale = rows(e, delivery_status="pending")[0]
    fake.plan["slack"] = [vs.TransportError("ConnectError")]
    d.deliver_pending()  # attempt 1 fails safely and goes back to pending
    e.advance_hours(1)
    assert d._deliver_one(stale) == "delivered"  # attempt 2, counted as attempt 2, no uniqueness error
    assert d.used_today() == 2


def test_manual_endpoint_and_worker_share_the_same_single_flight(live):
    e, d, slow = _slow(live)
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    first = threading.Thread(target=d.deliver_pending)
    first.start()
    time.sleep(0.05)
    assert d.deliver_pending().get("busy") is True  # a second caller while one runs is told to skip, not queued
    first.join()
    assert len(slow.calls) == 1


def test_preflight_reports_state_and_leaks_nothing(live, tmp_path, monkeypatch):
    from app import preflight
    e, d, _ = live
    path = str(tmp_path / "pf.db")
    from app.db import Store
    s = Store(path)
    cs = vs.ConnectionStore(s, "acme-demo")
    cs.save("slack", "auth-slack-000001", SCRIPT_S)
    cs.set_setting("slack", "channel_id", "C123")
    s.set_setting("live_delivery", "on")
    monkeypatch.setenv("DESKHAND_INTEGRATION", "viasocket")
    out = dict(preflight.report(path))
    assert out["integration mode"] == "viasocket" and out["live delivery"] == "ON" and out["cap used today"] == "0/40"
    assert out["email override is one valid address"] == "yes" and out["Slack"] == "connected" and out["Gmail"] == "not connected"
    assert out["Slack channel chosen"] == "yes" and out["commit checked out"]
    blob = json.dumps(out)
    for secret in (SCRIPT_S, "auth-slack-000001", "secret-value-for-tests", OVERRIDE, "C123"):
        assert secret not in blob
    monkeypatch.setenv("DESKHAND_LIVE_EMAIL_OVERRIDE", "a@b.co, c@d.co")
    assert dict(preflight.report(path))["email override is one valid address"].startswith("NO")
