"""viaSocket's public app catalog: which apps exist, which actions each has, and the form (input_schema) each action needs.

The catalog is public: no token, no connection, nothing is written anywhere. It is read through the same transport as every other viaSocket call, so tests inject a fake
transport (rule 7) and a FakeCatalog fed by data/steps_catalog_fixture.json. App slugs are checked against SLUG before they go into any URL and are never built from
message text. Facts: docs/VIASOCKET_FACTS.md, "PHASE 5 SPIKE 5A.0"."""
import json
import os
import re
import time
from urllib.parse import urlencode

from . import viasocket_adapter as vs

SLUG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PLUGS = "https://plug-service.viasocket.com"
EVENTS_URL = "https://flow.sokt.io/func/scriK4LFg2kc"  # viaSocket's own public catalog function (named in its documentation); not ours and not secret
RESERVED = {"ai", "slack_flow"}  # rows in the connections table that are not apps
SEARCH_MAX, TTL = 30, 600
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "steps_catalog_fixture.json")
# Words that make an action destructive. It then can only run when a person presses Run (rule 21).
DESTRUCTIVE = re.compile(r"\b(delete|remove|clear|purge|destroy|wipe|terminate|suspend|deactivate|revoke|disable|permanently|drop|erase)\b", re.I)


class CatalogError(Exception):
    """The catalog could not answer. The message is safe to show."""

    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def valid_slug(slug):
    return isinstance(slug, str) and bool(SLUG.match(slug)) and slug not in RESERVED


def is_destructive(name, description=""):
    return bool(DESTRUCTIVE.search(f"{name or ''} {description or ''}"))


class Catalog:
    """What the rest of Deskhand may ask of the catalog. `app` and `search` return plain dicts; an unknown app is None."""

    def search(self, text):
        raise NotImplementedError

    def app(self, slug):
        """{slug, name, service_id, auth_type, requires_auth, actions: [raw action dicts]} or None."""
        raise NotImplementedError


class HttpCatalog(Catalog):
    def __init__(self, transport=None, clock=time.time, ttl=TTL):
        self.t, self.clock, self.ttl, self._cache = transport or vs.HttpxTransport(), clock, ttl, {}

    def _get(self, key, make):
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < self.ttl:
            return hit[1]
        value = make()
        self._cache[key] = (self.clock(), value)
        return value

    def _json(self, method, url, body=None, timeout=25):
        try:
            status, text = self.t.request(method, url, None, body, timeout=timeout)
        except vs.TransportError:
            raise CatalogError("The app catalog could not be reached. Try again in a minute.")
        if status >= 400:
            raise CatalogError("The app catalog is not available right now.")
        try:
            return json.loads(text)
        except ValueError:
            raise CatalogError("The app catalog sent an answer Deskhand cannot read.")

    def search(self, text):
        text = (text or "").strip()[:60]
        if not text:
            return []

        def make():
            body = self._json("GET", f"{PLUGS}/plugins/search?{urlencode({'prefix': text})}")
            rows = ((body.get("data") or {}).get("rows") if isinstance(body, dict) else None) or []
            return [{"slug": r["appslugname"], "name": str(r.get("name") or r["appslugname"])[:80]} for r in rows if isinstance(r, dict) and valid_slug(r.get("appslugname"))][:SEARCH_MAX]
        return self._get(("search", text.lower()), make)

    def _service_id(self, slug):
        # an unknown slug can take about 30 s to answer with nothing, so this call is allowed to wait
        body = self._json("GET", f"{PLUGS}/api/v1/plugins/recommend/services?{urlencode({'service': slug})}", timeout=35)
        plugins = ((body.get("data") or {}).get("plugins") if isinstance(body, dict) else None) or {}
        row = plugins.get(slug) if isinstance(plugins, dict) else None
        return row.get("rowid") if isinstance(row, dict) else None

    def app(self, slug):
        if not valid_slug(slug):
            return None

        def make():
            sid = self._service_id(slug)
            if not sid:
                return None
            body = self._json("POST", EVENTS_URL, {"service_id": sid})
            data = body if isinstance(body, dict) and "service" in body else (body.get("data") if isinstance(body, dict) else None)
            svc = (data or {}).get("service") if isinstance(data, dict) else None
            actions = [a for a in (data or {}).get("actions") or [] if isinstance(a, dict) and a.get("action_id") and a.get("action_version_id")]
            if not isinstance(svc, dict) or not svc.get("name") or not actions:
                return None
            return {"slug": slug, "name": str(svc["name"])[:80], "service_id": sid, "auth_type": str(svc.get("auth_type") or ""), "requires_auth": svc.get("requires_auth") is not False,
                    "actions": actions}
        return self._get(("app", slug), make)


class FakeCatalog(Catalog):
    """Serves data/steps_catalog_fixture.json (or a dict passed in). No network."""

    def __init__(self, data=None):
        self.data = data if data is not None else json.load(open(FIXTURE, encoding="utf-8"))
        self.calls = []

    def search(self, text):
        self.calls.append(("search", text))
        t = (text or "").strip().lower()
        return [{"slug": s, "name": a["name"]} for s, a in self.data["apps"].items() if t and (t in a["name"].lower() or t in s)][:SEARCH_MAX]

    def app(self, slug):
        self.calls.append(("app", slug))
        a = self.data["apps"].get(slug) if valid_slug(slug) else None
        if not a:
            return None
        return {"slug": slug, "name": a["name"], "service_id": a["service_id"], "auth_type": a["auth_type"], "requires_auth": a["requires_auth"], "actions": a["actions"]}


_override = None  # chosen on purpose (tests, offline development)
_default = None  # the real catalog, made on first use so the whole process shares one cache


def set_catalog(catalog):
    """Tests and offline development choose the catalog here. None goes back to the real one."""
    global _override
    _override = catalog


def get_catalog():
    global _default
    if _override is not None:
        return _override
    if _default is None:
        _default = FakeCatalog() if os.environ.get("DESKHAND_STEPS_CATALOG") == "fixture" else HttpCatalog()  # the fixture never reaches viaSocket
    return _default
