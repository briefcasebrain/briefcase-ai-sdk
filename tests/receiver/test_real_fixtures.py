"""Payloads captured from REAL tools (see fixtures/real/README.md) must map to records.

When a tool release changes its telemetry, re-capturing with `python -m scripts.conformance` and
adding the new version directory makes these tests cover it.
"""

import json
from pathlib import Path

import pytest

from briefcase.receiver import normalize, otlp
from briefcase.receiver.records import RecordMapper, Redactor

REAL = Path(__file__).parent / "fixtures" / "real"
LOGS = sorted(REAL.glob("*/*/*-logs.json"))


def records_for(path):
    mapper = RecordMapper(Redactor(sanitize=None))
    out = []
    for signal in otlp.parse("logs", json.loads(path.read_text(encoding="utf-8"))):
        event = normalize.normalize_log(signal)
        if event is not None:
            out.extend(mapper.ingest(event))
    return out + mapper.flush()


@pytest.mark.skipif(not LOGS, reason="no captured payloads in the corpus")
@pytest.mark.parametrize("path", LOGS, ids=lambda p: f"{p.parent.parent.name}/{p.parent.name}")
def test_real_logs_map_to_records(path):
    records = records_for(path)
    names = {r["function_name"] for r in records}
    assert any("user_prompt" in n for n in names), names
    assert any("api_request" in n for n in names), names
    for record in records:
        assert record["decision_id"] and record["started_at"] and record["ended_at"]


@pytest.mark.skipif(not LOGS, reason="no captured payloads in the corpus")
def test_real_logs_never_leak_content_or_identity_through_records():
    blob = json.dumps([r for path in LOGS for r in records_for(path)])
    assert "conformance probe" not in blob           # the prompt text
    assert "@" not in blob.replace("example.invalid", "")


def test_every_corpus_directory_follows_tool_version_layout():
    for tool in (p for p in REAL.iterdir() if p.is_dir()):
        for version in (v for v in tool.iterdir() if v.is_dir()):
            assert version.name[0].isdigit(), f"{version}: directory must be named by tool version"
            assert any(version.glob("*.json")), f"{version}: empty"
