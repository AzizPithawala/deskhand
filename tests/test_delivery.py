"""Outbound delivery machinery: separation from approvals, exactly-once, retries, the daily cap, concurrency. Who a message goes to is in test_delivery_people.py.
Fake transport only; nothing touches the network."""
import json
import threading
import time

import pytest

from app import chaos, delivery as dl, viasocket_adapter as vs
from app.engine import Engine

SCRIPT_S, SCRIPT_G, SCRIPT_P = "scriSLACKRUNNER1", "scriGMAILRUNNER1", "scriPERSONALRUN1"
SLACK_OK = (200, json.dumps({"ok": True, "ts": "1.2", "channel": "D1", "message": {"metadata": {"event_payload": {"script_id": SCRIPT_S}}}}))
GMAIL_OK = (200, json.dumps({"success": True, "data": {"id": "m1", "threadId": "t1"}}))


class Fake:
    """Answers by which runner is called; records every call as (app, body, url). `plan` can inject failures per app."""
    def __init__(self):
        self.calls, self.plan = [], {"slack": [], "gmail": []}

    def request(self, method, url, headers=None, body=None, timeout=25):
        app = "slack" if SCRIPT_S in url else "gmail"
        self.calls.append((app, body, url))
        if self.plan[app]:
            a = self.plan[app].pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        return SLACK_OK if app == "slack" else GMAIL_OK


@pytest.fixture
def env(monkeypatch):
    for k, v in {"VIASOCKET_ORG_ID": "o", "VIASOCKET_PROJECT_ID": "p", "VIASOCKET_EMBED_SECRET": "secret-value-for-tests-0123456789",
                 "VIASOCKET_API_URL": "https://api.test.invalid", "VIASOCKET_RUN_URL": "https://run.test.invalid"}.items():
        monkeypatch.setenv(k, v)


def setup_apps(e, slack=True, gmail=True):
    cs = vs.ConnectionStore(e.s, getattr(e, "org_id", "acme-demo"))
    if slack:
        cs.save("slack", "auth-slack-000001", SCRIPT_S)
    if gmail:
        cs.save("gmail", "auth-gmail-000001", SCRIPT_G)


def make_delivery(e, fake, tenant="acme-demo"):
    return dl.Delivery(e, lambda: vs.ViaSocketIntegration(e.s, tenant, vs.VsClient(tenant, fake)), base_url="http://deskhand.test", tenant=tenant,
                       personal_factory=lambda t: vs.ViaSocketIntegration(e.s, t, vs.VsClient(t, fake)))


@pytest.fixture
def live(env):
    """A company with Slack and Gmail connected, in rehearsal (the default): everything goes to the one rehearsing admin."""
    e = Engine(api_key="")
    setup_apps(e)
    fake = Fake()
    d = e.live = make_delivery(e, fake)
    return e, d, fake


def rows(e, **where):
    sql = "SELECT * FROM outbox" + ("".join(f" WHERE {k}=?" if i == 0 else f" AND {k}=?" for i, k in enumerate(where)))
    return e.s.all(sql + " ORDER BY id", tuple(where.values()))


def test_cap_is_the_backstop_value_and_only_failure_notices_are_withheld():
    assert dl.DAILY_CAP == 150 and dl.NOT_DELIVERED_KINDS == {"delivery_failed"} and not hasattr(dl, "LIVE_KINDS")


def test_the_only_switch_is_rehearsal_or_live_and_there_is_no_mock_fallback():
    assert not hasattr(dl.Delivery, "toggle") and not hasattr(dl.Delivery, "set_toggle") and not hasattr(vs, "MockIntegration")
    assert not hasattr(vs.get_integration(Engine(api_key="").s), "mode")


def test_every_attention_kind_is_delivered_once_the_apps_are_set_up_and_chat_replies_stay_in_the_app(live):
    e, d, fake = live
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    e.approve(rid, "e06")
    e.done(rid, "e06")
    by_kind = {}
    for r in rows(e):
        by_kind.setdefault(r["kind"], set()).add((r["channel"], r["delivery_status"]))
    assert by_kind["submitted"] == {("chat", "skipped")} and by_kind["approval_request"] == {("chat", "pending"), ("email", "pending")}
    assert by_kind["task"] == {("chat", "pending"), ("email", "pending")} and by_kind["fulfilled"] == {("chat", "pending"), ("email", "pending")}
    assert by_kind["approved"] <= {("chat", "pending"), ("email", "pending")}
    pending = len(rows(e, delivery_status="pending"))
    d.deliver_pending()
    assert len(fake.calls) == pending and not rows(e, delivery_status="pending") and rows(e, kind="submitted")[0]["delivery_note"] == "in the Deskhand chat only"


def test_not_configured_rows_are_skipped_and_the_banner_says_why(env):
    e = Engine(api_key="")
    d = e.live = make_delivery(e, Fake())
    assert d.problems() == ["Slack not connected: notifications are not being delivered", "Gmail not connected: email notifications are not being delivered"]
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    assert {r["delivery_status"] for r in rows(e)} == {"skipped"} and any("not connected" in (r["delivery_note"] or "") for r in rows(e))
    setup_apps(e, gmail=False)
    assert d.problems() == ["Gmail not connected: email notifications are not being delivered"]
    setup_apps(e)
    assert d.problems() == []


def test_missing_server_values_block_delivery_with_a_banner(env, monkeypatch):
    e = Engine(api_key="")
    setup_apps(e)
    d = e.live = make_delivery(e, Fake())
    monkeypatch.delenv("VIASOCKET_EMBED_SECRET")
    assert d.problems() == ["viaSocket is not configured on the server: notifications are not being delivered"] and not d.ready("chat")


def test_delivery_starts_by_itself_once_configured_and_old_rows_are_not_replayed(env):
    e = Engine(api_key="")
    fake = Fake()
    d = e.live = make_delivery(e, fake)
    e.ingest("chat", "e01", "Need a Claude seat for my research project")  # before setup
    setup_apps(e)
    d.deliver_pending()
    assert not fake.calls  # no flood of stale messages
    e.ingest("chat", "e02", "Need a Figma seat for design")
    d.deliver_pending()
    assert fake.calls


def test_approval_succeeds_even_if_slack_is_down_and_delivery_is_parked_not_lost(live):
    e, d, fake = live
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    fake.plan["slack"] = [vs.TransportError("ReadTimeout")] * 50
    e.approve(rid, "e06")  # no delivery happens in the approval path
    assert e.req(rid)["status"] == "FULFILLING" and not fake.calls
    d.deliver_pending()
    assert e.req(rid)["status"] == "FULFILLING"  # a delivery failure never touches the request
    failed = rows(e, delivery_status="failed")
    assert failed and "outcome unknown" in failed[0]["delivery_error"]
    fake.plan["slack"] = []
    e.live.retry_failed(failed[0]["id"], "e10")
    d.deliver_pending()
    assert rows(e, id=failed[0]["id"])[0]["delivery_status"] == "delivered"


def test_a_failed_delivery_notice_is_not_itself_delivered_so_a_dead_channel_cannot_loop(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    fake.plan["slack"] = [(200, json.dumps({"ok": False, "error": "user_not_found"}))] * 100
    d.deliver_pending()
    d.deliver_pending()
    notices = rows(e, kind="delivery_failed")
    assert notices and {r["delivery_status"] for r in notices} == {"skipped"}
    attempted = [r for r in rows(e) if r["delivery_status"] in ("failed", "delivered")]
    assert len(fake.calls) == len(attempted)  # one call per row, never one per notice


def test_exactly_once_per_idempotency_key(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    d.deliver_pending()
    n = len(fake.calls)
    e.s.q("UPDATE outbox SET delivery_status='pending' WHERE delivery_status='delivered'")  # even if rows were wrongly reset, a key that succeeded is never resent
    d.deliver_pending()
    assert len(fake.calls) == n and not rows(e, delivery_status="pending")


def _one_slack_row(e):
    e.s.q("DELETE FROM outbox WHERE kind != 'approval_request' OR channel='email'")


def test_safe_failures_retry_with_backoff_unsafe_ones_do_not(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    _one_slack_row(e)
    fake.plan["slack"] = [vs.TransportError("ConnectError"), (200, json.dumps({"ok": False, "error": "ratelimited"}))]
    assert d.deliver_pending()["retry"] == 1
    assert d.deliver_pending()["delivered"] == 0  # backoff not elapsed yet
    e.advance_hours(1)
    assert d.deliver_pending()["retry"] == 1
    e.advance_hours(1)
    assert d.deliver_pending()["delivered"] == 1 and len(fake.calls) == 3
    e.ingest("chat", "e02", "Need a Figma seat for design")
    e.s.q("DELETE FROM outbox WHERE (kind != 'approval_request' OR channel='email') AND delivery_status='pending'")
    fake.plan["slack"] = [(502, "{}")]  # a 5xx may have been delivered already: never auto-retry
    out = d.deliver_pending()
    assert out["failed"] == 1 and out["retry"] == 0


def test_permanent_failure_is_parked_with_audit(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    fake.plan["slack"] = [(200, json.dumps({"ok": False, "error": "user_not_found"}))] * 20
    assert d.deliver_pending()["failed"] >= 1
    types = [x["type"] for x in e.s.all("SELECT type FROM events")]
    assert "delivery_failed" in types and e.dashboard()["guard"]["delivery_failed"] >= 1


def test_daily_cap_150_shows_a_banner_and_nothing_silently_stops(live):
    e, d, fake = live
    for i in range(dl.DAILY_CAP):
        e.s.q("INSERT INTO delivery_ledger(day,ikey,ts) VALUES(?,?,?)", (dl._today(), f"x{i}", 0))
    assert d.capped() and d.summary()["capped"] and d.summary()["cap"] == 150
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    assert {r["delivery_status"] for r in rows(e)} == {"skipped"} and any("cap" in (r["delivery_note"] or "") for r in rows(e))
    d.deliver_pending()
    assert not fake.calls and e.dashboard()["delivery"]["capped"] and e.dashboard()["delivery"]["cap"] == 150
    e.reset()  # a reset cannot reopen the cap, and it keeps the connections and their settings
    assert d.used_today() == dl.DAILY_CAP and d.ready("chat") and d.ready("email")
    with pytest.raises(ValueError):
        e.s.q("INSERT INTO outbox(request_id,channel,to_person,subject,body,kind,idempotency_key,created_at,delivery_status) VALUES(1,'chat','e01','s','b','rejected','k',0,'failed')")
        d.retry_failed(1, "e10")


def test_reaching_the_cap_midway_never_exceeds_it(live):
    e, d, fake = live
    for i in range(dl.DAILY_CAP - 1):
        e.s.q("INSERT INTO delivery_ledger(day,ikey,ts) VALUES(?,?,?)", (dl._today(), f"x{i}", 0))
    for emp in ("e01", "e02"):
        e.ingest("chat", emp, "Need a Claude seat for my research project")
    d.deliver_pending()
    assert len(fake.calls) == 1 and d.used_today() == dl.DAILY_CAP and not rows(e, delivery_status="pending")


def test_chaos_and_reset_never_send_live_and_reset_keeps_configuration(live):
    e, d, fake = live
    with dl.forced_mock(e):
        e.ingest("chat", "e01", "Need a Claude seat for my research project")
        assert {r["delivery_status"] for r in rows(e)} == {"skipped"}
        assert d.deliver_pending() == {"delivered": 0, "failed": 0, "retry": 0, "skipped": 0}
        for name, fn in chaos.SCENARIOS.items():
            assert fn(e)["passed"], name
        e.reset()
    assert not fake.calls and not rows(e, delivery_status="pending")
    assert d.ready("chat") and d.ready("email")  # configuration survives Reset demo


def test_nothing_secret_leaks_into_outbox_audit_or_dashboard(live):
    e, d, fake = live
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    fake.plan["slack"] = [(200, json.dumps({"ok": False, "error": f"boom {SCRIPT_S}"}))]
    d.deliver_pending()
    e.approve(rid, "e06")
    d.deliver_pending()
    blob = json.dumps([rows(e), e.s.all("SELECT * FROM events"), e.dashboard(), d.summary(), d.problems()])
    for secret in (SCRIPT_S, SCRIPT_G, "auth-slack-000001", "secret-value-for-tests"):
        assert secret not in blob


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
    _one_slack_row(e)
    stale = rows(e, delivery_status="pending")[0]
    fake.plan["slack"] = [vs.TransportError("ConnectError")]
    d.deliver_pending()  # attempt 1 fails safely and goes back to pending
    e.advance_hours(1)
    assert d._deliver_one(stale) == "delivered"  # attempt 2, counted as attempt 2, no uniqueness error
    assert d.used_today() == 2


def test_manual_endpoint_and_worker_share_the_same_single_flight(live):
    e, d, slow = _slow(live)
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    n = len(rows(e, delivery_status="pending"))
    first = threading.Thread(target=d.deliver_pending)
    first.start()
    time.sleep(0.05)
    assert d.deliver_pending().get("busy") is True  # a second caller while one runs is told to skip, not queued
    first.join()
    assert len(slow.calls) == n


def test_preflight_reports_yes_no_and_leaks_nothing(live, tmp_path, monkeypatch):
    from app import preflight
    from app.db import Store
    path = str(tmp_path / "pf.db")
    st = Store(path)
    cs = vs.ConnectionStore(st, "acme-demo")
    cs.save("slack", "auth-slack-000001", SCRIPT_S)
    out = dict(preflight.report(path))
    assert out["Slack connected"] == "yes" and out["Gmail connected"] == "NO" and out["delivery is working (all of the above)"] == "NO" and out["messaging mode"] == "rehearsal"
    cs.save("gmail", "auth-gmail-000001", SCRIPT_G)
    st.set_setting("delivery_mode", "live")
    out = dict(preflight.report(path))
    assert out["delivery is working (all of the above)"] == "yes" and out["cap used today"] == "0/150" and out["commit checked out"] and out["messaging mode"] == "live"
    blob = json.dumps(out)
    for secret in (SCRIPT_S, SCRIPT_G, "auth-slack-000001", "secret-value-for-tests"):
        assert secret not in blob


def test_the_demo_company_never_delivers_even_with_real_connections_stored(live):
    e, d, fake = live
    assert d.ready("chat") and d.ready("email")
    e.is_demo = True  # a demo company that inherited Slack and Gmail rows from an older single-company database
    assert not d.ready("chat") and not d.ready("email")
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    assert {r["delivery_status"] for r in rows(e)} == {"skipped"}
    assert d.deliver_pending()["delivered"] == 0 and not fake.calls
