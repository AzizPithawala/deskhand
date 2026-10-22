"""Who a message goes to and from whom: rehearsal versus live, a Slack DM per person, the right mailbox, directory people only. Fake transport; no network."""
import json

import pytest

from app import delivery as dl, viasocket_adapter as vs
from app.engine import Engine
from tests.test_delivery import GMAIL_OK, SCRIPT_G, SCRIPT_P, SCRIPT_S, Fake, env, live, make_delivery, rows, setup_apps  # noqa: F401 - fixtures are used by name

SCRIPT_P2 = "scriPERSONALRUN2"


def connect_personal(e, person, script):
    vs.ConnectionStore(e.s, vs.personal_tenant("acme-demo", person)).save("gmail", "auth-" + person, script)


def slack_calls(fake):
    return [c for c in fake.calls if c[0] == "slack"]


def mail_calls(fake):
    return [c for c in fake.calls if c[0] == "gmail"]


def test_rehearsal_sends_everything_only_to_the_rehearsing_admin_and_says_who_it_is_for(live):
    e, d, fake = live
    assert d.mode() == "rehearsal" and d.rehearsal_person()["id"] == "e10"
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    e.approve(rid, "e06")
    e.done(rid, "e06")
    d.deliver_pending()
    assert slack_calls(fake) and mail_calls(fake)
    for _, body, _ in slack_calls(fake):
        assert body["inputData"]["userId"] == "U010" and body["inputData"]["content"].startswith("[Rehearsal] To ")  # Arjun, whoever the message is for
    for _, body, _ in mail_calls(fake):
        assert body["inputData"]["to"] == "arjun.patel@acme-demo.example" and body["inputData"]["subject"].startswith("[Rehearsal] To ")
        assert "Rehearsal: this is the message" in body["inputData"]["messageBody"]
    blob = json.dumps(fake.calls)
    for other in ("U006", "U001", "imran.khan@", "priya.sharma@"):
        assert other not in blob  # nobody else's Slack id or address was ever used
    assert "[Rehearsal] To Imran Khan: " in blob and "[Rehearsal] To Priya Sharma: " in blob


def test_live_sends_each_message_to_its_own_person_by_slack_id_and_directory_email(live):
    e, d, fake = live
    d.set_mode("live", "e10")
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    e.approve(rid, "e06")
    e.done(rid, "e06")
    d.deliver_pending()
    ids = {c[1]["inputData"]["userId"] for c in slack_calls(fake)}
    assert ids == {"U006", "U001"}  # the approver and the requester, nobody else
    assert {c[1]["inputData"]["to"] for c in mail_calls(fake)} == {"imran.khan@acme-demo.example", "priya.sharma@acme-demo.example"}
    text = [c[1]["inputData"]["content"] for c in slack_calls(fake)]
    assert not any("[Rehearsal]" in t for t in text) and not any("[Rehearsal]" in c[1]["inputData"]["subject"] for c in mail_calls(fake))
    approval = [t for t in text if "Approval needed" in t][0]
    assert f"http://deskhand.test/#needs/{rid}" in approval  # the approver is sent to the decision page
    done = [t for t in text if "ready" in t][0]
    assert f"http://deskhand.test/#request/{rid}" in done and "#needs" not in done  # the requester to the request


def test_message_text_cannot_ping_a_channel_or_inject_a_link(live):
    e, d, fake = live
    d.set_mode("live", "e10")
    e.ingest("chat", "e01", "Need a Claude seat. <!channel> <@U123> & <http://evil.test|click> please")
    d.deliver_pending()
    text = [c[1]["inputData"]["content"] for c in slack_calls(fake) if "Approval needed" in c[1]["inputData"]["content"]][0]
    assert "<!channel>" not in text and "<@U123>" not in text and "<http" not in text and "&lt;!channel&gt;" in text
    mail = [c[1]["inputData"]["messageBody"] for c in mail_calls(fake)][0]
    assert "<!channel>" not in mail and "&lt;!channel&gt;" in mail


def test_chat_replies_policy_answers_and_received_notices_stay_in_the_app(live):
    e, d, fake = live
    d.set_mode("live", "e10")
    e.ingest("chat", "e01", "What is the work from home policy?")
    e.ingest("chat", "e02", "Need a Claude seat for my research project")
    d.deliver_pending()
    kinds = {r["kind"]: r["delivery_status"] for r in rows(e) if r["channel"] == "chat"}
    assert kinds["answer"] == "skipped" and kinds["submitted"] == "skipped" and kinds["approval_request"] == "delivered"
    assert not [c for c in slack_calls(fake) if "work from home" in json.dumps(c).lower()]


def test_the_request_email_comes_from_the_requester_and_decisions_from_the_approver(live):
    e, d, fake = live
    d.set_mode("live", "e10")
    connect_personal(e, "e01", SCRIPT_P)
    connect_personal(e, "e06", SCRIPT_P2)
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    e.approve(rid, "e06")
    e.done(rid, "e06")
    d.deliver_pending()
    by_kind = {r["kind"]: r for r in rows(e, channel="email")}
    assert by_kind["approval_request"]["from_person"] == "e01" and by_kind["approved"]["from_person"] == "e06" and by_kind["fulfilled"]["from_person"] == "e06" and by_kind["task"]["from_person"] is None
    urls = {c[1]["inputData"]["subject"].split(":")[0]: c[2] for c in mail_calls(fake)}
    to_approver = [c for c in mail_calls(fake) if c[1]["inputData"]["to"] == "imran.khan@acme-demo.example"]
    to_requester = [c for c in mail_calls(fake) if c[1]["inputData"]["to"] == "priya.sharma@acme-demo.example"]
    assert any(SCRIPT_P in c[2] and "Approval needed" in c[1]["inputData"]["subject"] for c in to_approver)  # Priya's own mailbox asks Imran
    assert all(SCRIPT_P2 in c[2] for c in to_requester) and to_requester  # Imran's own mailbox tells Priya
    assert any(SCRIPT_G in c[2] and c[1]["inputData"]["subject"].startswith("Task:") for c in to_approver)  # system messages use the company mailbox
    assert urls


def test_without_a_personal_mailbox_the_company_mailbox_sends(live):
    e, d, fake = live
    d.set_mode("live", "e10")
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    e.approve(rid, "e06")
    d.deliver_pending()
    assert mail_calls(fake) and all(SCRIPT_G in c[2] for c in mail_calls(fake)) and all(r["delivery_status"] != "failed" for r in rows(e))


class ScriptFake(Fake):
    """Can make one specific runner answer differently, to test a personal mailbox that cannot send."""
    def __init__(self):
        super().__init__()
        self.by_script = {}

    def request(self, method, url, headers=None, body=None, timeout=25):
        for sc, plan in self.by_script.items():
            if sc in url and plan:
                self.calls.append(("gmail", body, url))
                return plan.pop(0)
        return super().request(method, url, headers, body, timeout)


def test_a_personal_mailbox_that_cannot_send_falls_back_to_the_company_mailbox_once(env):
    e = Engine(api_key="")
    setup_apps(e)
    fake = ScriptFake()
    d = e.live = make_delivery(e, fake)
    d.set_mode("live", "e10")
    connect_personal(e, "e01", SCRIPT_P)
    fake.by_script[SCRIPT_P] = [(200, json.dumps({"success": True, "data": {"labelIds": []}}))]  # the mailbox answers but sends nothing: a certain failure
    rid = e.ingest("chat", "e01", "Need a Claude seat for my research project")["request_ids"][0]
    e.s.q("DELETE FROM outbox WHERE channel='chat' OR kind != 'approval_request'")
    d.deliver_pending()
    row = rows(e, kind="approval_request")[0]
    mails = [c for c in fake.calls if c[0] == "gmail"]
    assert row["delivery_status"] == "delivered" and len(mails) == 2 and SCRIPT_P in mails[0][2] and SCRIPT_G in mails[1][2] and rid


def test_a_person_without_a_slack_account_still_gets_the_email_and_the_admin_is_told(live):
    e, d, fake = live
    d.set_mode("live", "e10")
    e.d.emp("e06")["slack_id"] = ""  # Imran has no Slack account matched
    e.ingest("chat", "e01", "Need a Claude seat for my research project")
    to_imran = {r["channel"]: r for r in rows(e, kind="approval_request")}
    assert to_imran["chat"]["delivery_status"] == "skipped" and "no Slack account matched for Imran Khan" in to_imran["chat"]["delivery_note"]
    assert to_imran["email"]["delivery_status"] == "pending"
    d.deliver_pending()
    assert [c[1]["inputData"]["to"] for c in mail_calls(fake)] == ["imran.khan@acme-demo.example"] and "Imran Khan" in d.notes()[0] and "1 people" in d.notes()[0]


def test_recipients_are_always_people_in_the_directory(live):
    e, d, fake = live
    d.set_mode("live", "e10")
    assert d.recipient("stranger@evil.test") is None and d.recipient("e06")["email"] == "imran.khan@acme-demo.example"
    e._notify(None, "stranger@evil.test", "hello", "approval_request", subject="x")  # an id that is not a person
    e._notify(None, "e01", "hello", "approval_request", subject="x")
    d.deliver_pending()
    assert "stranger" not in json.dumps(fake.calls) and {r["to_person"]: r["delivery_status"] for r in rows(e) if r["channel"] == "email"}["stranger@evil.test"] == "skipped"
    d.e.d.emp("e01")["active"] = False
    assert d.recipient("e01") is None  # a deactivated person is not messaged
    d.set_mode("rehearsal", "e10")
    assert d.recipient("stranger@evil.test")["id"] == "e10"  # in rehearsal only the admin, whoever the row names


def test_one_message_makes_one_chat_row_and_one_email_row_and_repeating_it_adds_nothing(live):
    e, d, fake = live
    e._notify(7, "e06", "Please decide", "approval_request", subject="Approval needed")
    e._notify(7, "e06", "Please decide", "approval_request", subject="Approval needed")
    got = sorted((r["channel"], r["idempotency_key"]) for r in rows(e, kind="approval_request"))
    assert got == [("chat", "7:approval_request:e06:0"), ("email", "7:approval_request:e06:0:email")]


def test_rehearsal_person_must_be_an_active_admin_and_every_mode_change_is_audited(live):
    e, d, fake = live
    for bad in ("e01", "nobody", "e06"):
        with pytest.raises(ValueError):
            d.set_mode("rehearsal", "e10", bad)
    with pytest.raises(ValueError):
        d.set_mode("sometimes", "e10")
    d.set_mode("live", "e10")
    ev = json.loads(e.s.all("SELECT data_json FROM events WHERE type='delivery_mode_changed'")[-1]["data_json"])
    assert ev["mode"] == "live" and ev["rehearsal_person"] == "e10" and ev["was"] == ["rehearsal", "e10"]
    assert e.s.all("SELECT actor FROM events WHERE type='delivery_mode_changed'")[-1]["actor"] == "e10"


def test_going_live_sends_nothing_that_was_written_before_it(live):
    e, d, fake = live
    e.ingest("chat", "e01", "Need a Claude seat for my research project")  # written in rehearsal, not sent yet
    assert rows(e, delivery_status="pending")
    d.set_mode("live", "e10")
    assert not rows(e, delivery_status="pending") and {r["delivery_note"] for r in rows(e, delivery_status="skipped")} >= {"written before the mode changed, not sent"}
    d.deliver_pending()
    assert not fake.calls  # no rehearsal message reaches a real person
    ev = json.loads(e.s.all("SELECT data_json FROM events WHERE type='delivery_mode_changed'")[-1]["data_json"])
    assert ev["unsent_messages_dropped"] >= 2
    e.ingest("chat", "e02", "Need a Figma seat for design")
    d.deliver_pending()
    assert fake.calls  # new messages flow


def test_the_rehearsal_note_and_summary_tell_an_admin_what_is_on(live):
    e, d, fake = live
    s = d.summary()
    assert s["mode"] == "rehearsal" and s["rehearsal_person"] == {"id": "e10", "name": "Arjun Patel"} and "Rehearsal" in s["notes"][0] and s["slack_matched"] == 10 and s["people"] == 10
    d.set_mode("live", "e10")
    assert d.summary()["mode"] == "live" and d.summary()["notes"] == []
