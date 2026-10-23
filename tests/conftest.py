import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.pop("ANTHROPIC_API_KEY", None)

from app.engine import Engine  # noqa: E402


@pytest.fixture
def e():
    return Engine(api_key="")  # in-memory DB, rules classifier, mock HR


@pytest.fixture(autouse=True)
def _pinned_today(monkeypatch):
    """Dates in a leave request are worked out from "today". Pin it to a Thursday (2026-10-22) so no test depends on the weekday it runs on; the demo clock still moves it.
    A test that needs another day sets its own Engine.today. DESKHAND_TEST_TODAY=YYYY-MM-DD pins a different day to prove the suite does not depend on the weekday."""
    base = datetime.fromisoformat(os.environ.get("DESKHAND_TEST_TODAY", "2026-10-22")).replace(hour=12)

    def today(self):
        return (base + timedelta(seconds=float(self.s.get_setting("clock_offset", 0)))).date()
    monkeypatch.setattr(Engine, "today", today)


@pytest.fixture(autouse=True)
def _no_live_anything(monkeypatch):
    """Tests never deliver live: delivery and the integration are forced to their safe defaults, and no worker thread runs."""
    for k in ("DESKHAND_LIVE_DELIVERY", "DESKHAND_INTEGRATION", "DESKHAND_LIVE_EMAIL_OVERRIDE", "DESKHAND_DEMO_PASSWORD", "APP_NAME", "DESKHAND_REQUIRE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DESKHAND_DELIVERY_WORKER", "off")
