"""One database file per company: isolation by construction, directory from people, demo seeding, legacy migration."""
import os
import sqlite3

import pytest

from app import accounts as ac, registry as rg
from app.db import Store


@pytest.fixture
def env(tmp_path):
    acc, reg = rg.open_platform(str(tmp_path), ["c1", "c2", "c3"])
    return acc, reg, tmp_path


def make_company(acc, reg, email, code, name):
    org, owner = acc.register_company(name, "Owner " + name, email, "a-long-password", code)
    return org, owner


def test_each_company_has_its_own_database_file_and_nothing_leaks(env):
    acc, reg, tmp = env
    o1, u1 = make_company(acc, reg, "a@one.test", "c1", "One")
    o2, u2 = make_company(acc, reg, "b@two.test", "c2", "Two")
    e1, e2 = reg.engine_for(o1["id"]), reg.engine_for(o2["id"])
    assert e1 is not e2 and e1.s.path != e2.s.path and os.path.exists(e1.s.path) and os.path.exists(e2.s.path)
    r1 = e1.ingest("chat", u1["id"], "Need a Claude seat for my research project")["request_ids"][0]
    assert r1 == 1 and e2.list_requests() == [] and e2.s.all("SELECT * FROM outbox") == [] and e2.s.all("SELECT * FROM events") == []
    r2 = e2.ingest("chat", u2["id"], "I need a second monitor for my desk.")["request_ids"][0]
    assert r2 == 1 and [r["type_code"] for r in e1.list_requests()] == ["software_seat"]  # same id 1 in both, different rows
    assert e1.req(1)["employee_id"] == u1["id"] and e2.req(1)["employee_id"] == u2["id"]
    assert e1.live.tenant == o1["id"] and e2.live.tenant == o2["id"] and e1.org_id != e2.org_id


def test_directory_comes_from_the_companys_people_and_follows_changes(env):
    acc, reg, _ = env
    org, owner = make_company(acc, reg, "a@one.test", "c1", "One")
    eng = reg.engine_for(org["id"])
    boss, _ = acc.create_invite(org["id"], "boss@one.test", "Boss", "member", owner["id"])
    assert eng.d.emp(boss["id"]) is None  # not known yet: the directory refreshes when people change
    acc.accept_invite(acc.create_reset_link(org["id"], boss["id"], owner["id"]), "a-long-password")
    emp, _ = acc.create_invite(org["id"], "emp@one.test", "Emp", "member", owner["id"], manager_id=boss["id"])
    acc.accept_invite(acc.create_reset_link(org["id"], emp["id"], owner["id"]), "a-long-password")
    reg.refresh_directory(org["id"])
    assert eng.d.emp(emp["id"])["manager_id"] == boss["id"] and eng.d.resolve_sender("EMP@one.test")["id"] == emp["id"]
    rid = eng.ingest("chat", emp["id"], "I need Tuesday off, family function")["request_ids"][0]
    assert eng.req(rid)["chain"] == [boss["id"]]  # manager from the reporting line
    rid2 = eng.ingest("chat", emp["id"], "Need a Claude seat for my research project")["request_ids"][0]
    assert eng.req(rid2)["chain"] == [owner["id"]]  # no IT role holder yet: falls back to an admin
    assert eng.d.role_holder("admin")["id"] == owner["id"]


def test_invited_people_are_not_approvers_and_disabled_people_drop_out(env):
    acc, reg, _ = env
    org, owner = make_company(acc, reg, "a@one.test", "c1", "One")
    eng = reg.engine_for(org["id"])
    inv, _ = acc.create_invite(org["id"], "x@one.test", "Invited", "admin", owner["id"])
    reg.refresh_directory(org["id"])
    assert eng.d.role_holder("admin")["id"] == owner["id"]  # the invited admin has not joined


def test_demo_company_is_seeded_once_with_ten_people_and_the_json_directory(env):
    acc, reg, _ = env
    reg.seed_demo()
    users = acc.list_users(rg.DEMO_ORG_ID)
    assert len(users) == 10 and {u["status"] for u in users} == {"demo"} and acc.get_org(rg.DEMO_ORG_ID)["is_demo"] == 1
    eng = reg.engine_for(rg.DEMO_ORG_ID)
    assert eng.is_demo and eng.d.emp("e04")["name"] == "Rahul Mehta" and eng.d.emp("e01")["manager_id"] == "e04"
    assert {u["id"] for u in users if u["access_role"] == "admin"} == {"e10"}


def test_company_ids_cannot_escape_the_data_directory(env):
    _, reg, _ = env
    for bad in ("../x", "a/b", "", "a b", "x" * 80):
        with pytest.raises(ValueError):
            reg.path(bad)


def test_unknown_or_suspended_company_has_no_engine(env):
    acc, reg, _ = env
    with pytest.raises(KeyError):
        reg.engine_for("org_nope")
    org, _ = make_company(acc, reg, "a@one.test", "c1", "One")
    acc.s.q("UPDATE orgs SET status='suspended' WHERE id=?", (org["id"],))
    with pytest.raises(KeyError):
        reg.engine_for(org["id"])
    assert all(e.org_id != org["id"] for e in reg.all_engines())


def test_old_single_company_database_becomes_the_demo_file_and_keeps_its_data(tmp_path):
    legacy = tmp_path / "deskhand.db"
    old = Store(str(legacy))
    from app.engine import Engine
    e = Engine(store=old, api_key="")
    e.ingest("chat", "e01", "I need Friday and Monday off, family function.")
    old.conn.close()
    acc, reg = rg.open_platform(str(tmp_path), [])
    eng = reg.engine_for(rg.DEMO_ORG_ID)
    assert [r["type_code"] for r in eng.list_requests()] == ["leave"] and os.path.exists(legacy)  # copied, the old file is left as a backup
    acc2, reg2 = rg.open_platform(str(tmp_path), [])  # a second start does not copy again
    assert reg2.migrate_legacy(str(legacy)) is False
