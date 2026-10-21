import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.pop("ANTHROPIC_API_KEY", None)

from app.engine import Engine  # noqa: E402


@pytest.fixture
def e():
    return Engine(api_key="")  # in-memory DB, rules classifier, mock HR
