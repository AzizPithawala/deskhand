"""The app catalog adapter: reads viaSocket's public catalog through an injected transport, checks every slug before it reaches a URL, caches, and never leaks a failure
as anything but a readable message. No network (rule 7)."""
import json

import pytest

from app import viasocket_adapter as vs
from app import viasocket_catalog as vcat

FIX = json.load(open(vcat.FIXTURE))
SHEETS = FIX["apps"]["google-sheets"]


class Transport:
    """Answers the three public catalog calls from the recorded fixture and counts them."""

    def __init__(self, fail=None, status=200, body=None):
        self.calls, self.fail, self.status, self.body = [], fail, status, body

    def request(self, method, url, headers=None, body=None, timeout=25):
        self.calls.append((method, url, body))
        if self.fail:
            raise self.fail
        if self.body is not None:
            return self.status, self.body
        if "/plugins/search" in url:
            return 200, json.dumps({"data": {"rows": [{"appslugname": "google-sheets", "name": "Google Sheets"}, {"appslugname": "../etc/passwd", "name": "bad"},
                                                         {"appslugname": "has space", "name": "bad"}, {"appslugname": "slack", "name": "Slack"}]}})
        if "recommend/services" in url:
            return 200, json.dumps({"data": {"plugins": {"google-sheets": {"rowid": "svc-sheets"}}}})
        if url == vcat.EVENTS_URL:
            assert body == {"service_id": "svc-sheets"}
            return 200, json.dumps({"service": {"name": SHEETS["name"], "auth_type": SHEETS["auth_type"], "requires_auth": True}, "actions": SHEETS["actions"], "triggers": []})
        raise AssertionError(url)


def test_search_keeps_only_valid_slugs_and_encodes_the_text():
    t = Transport()
    got = vcat.HttpCatalog(t).search("google sheets & more")
    assert [a["slug"] for a in got] == ["google-sheets", "slack"]  # the two with unusable slugs are dropped
    assert "prefix=google+sheets+%26+more" in t.calls[0][1]
    assert vcat.HttpCatalog(Transport()).search("   ") == []


def test_an_app_comes_back_with_its_actions_and_is_cached_until_the_ttl():
    now = [1000.0]
    t = Transport()
    c = vcat.HttpCatalog(t, clock=lambda: now[0], ttl=600)
    a = c.app("google-sheets")
    assert a["name"] == "Google Sheets" and a["auth_type"] == "Auth2.0" and a["requires_auth"] and len(a["actions"]) == 8
    n = len(t.calls)
    c.app("google-sheets")
    assert len(t.calls) == n  # served from the cache
    now[0] += 601
    c.app("google-sheets")
    assert len(t.calls) > n  # the cache expired


@pytest.mark.parametrize("slug", ["", "a b", "../x", "x/y", "a" * 65, "ai", "slack_flow", None, 5, "x;y", "x?y=1"])
def test_a_slug_that_is_not_an_app_name_never_reaches_a_url(slug):
    t = Transport()
    assert vcat.HttpCatalog(t).app(slug) is None
    assert t.calls == []


def test_an_unknown_app_is_none_and_not_an_error():
    class Unknown(Transport):
        def request(self, method, url, headers=None, body=None, timeout=25):
            return 200, json.dumps({"data": {"plugins": {}}})
    assert vcat.HttpCatalog(Unknown()).app("nope") is None


@pytest.mark.parametrize("t", [Transport(fail=vs.TransportError("ConnectError")), Transport(status=500, body="{}"), Transport(body="<html>not json</html>")])
def test_a_catalog_that_cannot_answer_is_one_readable_error(t):
    with pytest.raises(vcat.CatalogError) as e:
        vcat.HttpCatalog(t).app("google-sheets")
    assert "catalog" in str(e.value).lower() and "http" not in str(e.value).lower()


def test_destructive_names_are_recognised_on_the_real_sheets_actions():
    names = {a["name"]: vcat.is_destructive(a["name"]) for a in SHEETS["actions"]}
    assert names["Delete Rows"] and names["Clear Spreadsheet Row"] and names["Permanently Delete Spreadsheet"]
    assert not any(v for k, v in names.items() if k in ("Add New Row to Sheet", "List Spreadsheets", "Create a Spreadsheet", "Lookup Spreadsheet Rows", "Update Spreadsheet Row"))


def test_the_fake_catalog_serves_the_fixture_and_checks_slugs_too():
    c = vcat.FakeCatalog()
    assert [a["slug"] for a in c.search("sheet")] == ["google-sheets"] and c.search("") == []
    assert c.app("demo-tickets")["name"] == "Demo Tickets" and c.app("nope") is None and c.app("../x") is None


def test_the_fixture_marks_what_is_real_and_what_is_synthetic():
    assert FIX["apps"]["google-sheets"]["real"] is True and FIX["apps"]["demo-tickets"]["real"] is False
    assert "SYNTHETIC" in FIX["apps"]["demo-tickets"]["note"]
    assert not vs._ID_LIKE.search(json.dumps(FIX))  # nothing shaped like a script id, token or secret (rule 8)
