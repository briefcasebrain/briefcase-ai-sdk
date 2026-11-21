import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "pricing"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BRIEFCASE_HOME", raising=False)
    monkeypatch.delenv("BRIEFCASE_PRICING_FILE", raising=False)
    return tmp_path
