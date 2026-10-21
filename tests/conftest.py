import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.pop("ANTHROPIC_API_KEY", None)

from app.engine import Engine  # noqa: E402


@pytest.fixture
def e():
    return Engine(api_key="")  # in-memory DB, rules classifier, mock HR


@pytest.fixture(autouse=True)
def _no_live_anything(monkeypatch):
    """Tests never deliver live: delivery and the integration are forced to their safe defaults, and no worker thread runs."""
    for k in ("DESKHAND_LIVE_DELIVERY", "DESKHAND_INTEGRATION", "DESKHAND_LIVE_EMAIL_OVERRIDE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DESKHAND_DELIVERY_WORKER", "off")
