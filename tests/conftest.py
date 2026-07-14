"""Test bootstrap: make the repo root importable and provide a fixture loader.

No environment changes are required -- these tests only exercise pure
parsing/analysis functions and never create a database engine or hit the
network.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_html():
    """Load a saved-HTML fixture from tests/fixtures by filename."""
    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")
    return _load
