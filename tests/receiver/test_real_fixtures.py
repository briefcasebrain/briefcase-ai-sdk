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


# -- redaction on the real envelope --------------------------------------------
#
# The captured Claude Code run made no tool calls, so its corpus has no
# tool_decision / tool_result events. These tests take a REAL log record from
# the corpus (its resource, its identity attributes, its exact OTLP shape) and
# change only the event name and the tool attributes Claude Code documents for
# those events, filled with what real tool calls carry: shell commands, host
# paths, URLs and credentials. Default redaction must drop all of it.

CLAUDE_LOGS = sorted(REAL.glob("claude-code/*/*-logs.json"))
SECRET_COMMAND = "cat /Users/dev.person/secret-project/.env"
SECRET_SENTINELS = ("/Users/dev.person", "secret-project", ".env", "cat ", "curl", "sk-live-SECRET",
                    "internal.corp.example", "https://", "Authorization", "C:\\\\Users", "rm -rf")


def _otlp_value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    return {"stringValue": str(value)}


def _with_tool_events(payload):
    """The real payload plus tool_decision / tool_result events cloned from
    its own api_request record."""
    import copy

    payload = copy.deepcopy(payload)
    records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    template = next(r for r in records
                    if any(a["key"] == "event.name" and a["value"].get("stringValue") == "api_request"
                           for a in r["attributes"]))
    keep = {"user.id", "session.id", "anthropic.provider", "terminal.type", "prompt.id", "event.timestamp"}
    common = [a for a in template["attributes"] if a["key"] in keep]
    parameters = json.dumps({"bash_command": "curl", "full_command": SECRET_COMMAND + " && curl -H "
                             "'Authorization: Bearer sk-live-SECRET' https://internal.corp.example/k",
                             "description": "read C:\\Users\\dev.person\\secret-project", "timeout": 1000})
    events = [
        ("tool_decision", {"tool_name": "Bash", "tool_use_id": "toolu_real_1", "decision": "accept",
                           "source": "config"}),
        ("tool_result", {"tool_name": "Bash", "tool_use_id": "toolu_real_1", "success": "true",
                         "duration_ms": 31, "tool_result_size_bytes": 120, "tool_parameters": parameters,
                         "tool_input": SECRET_COMMAND, "decision_type": "accept", "decision_source": "config"}),
        ("tool_result", {"tool_name": "Read", "tool_use_id": "toolu_real_2", "success": "false",
                         "duration_ms": 2, "error": "EACCES: /Users/dev.person/secret-project/.env",
                         "error_type": "PermissionError", "mcp_server_scope": "/Users/dev.person/secret-project",
                         "tool_parameters": json.dumps({"file_path": "/Users/dev.person/secret-project/.env"})}),
        ("tool_result", {"tool_name": "mcp__fs__write", "tool_use_id": "toolu_real_3", "success": "false",
                         "duration_ms": 5, "error": "rm -rf ~/secret-project failed",
                         "tool_parameters": json.dumps({"mcp_server_name": "fs", "path": "~/secret-project"})}),
    ]
    for sequence, (name, attrs) in enumerate(events, start=100):
        record = copy.deepcopy(template)
        record["body"] = {"stringValue": "claude_code." + name}
        record["attributes"] = common + [
            {"key": "event.name", "value": {"stringValue": name}},
            {"key": "event.sequence", "value": {"intValue": str(sequence)}},
        ] + [{"key": key, "value": _otlp_value(value)} for key, value in attrs.items()]
        records.append(record)
    return payload


def _records_from(payload, redactor):
    mapper = RecordMapper(redactor)
    out = []
    for signal in otlp.parse("logs", payload):
        event = normalize.normalize_log(signal)
        if event is not None:
            out.extend(mapper.ingest(event))
    return out + mapper.flush()


@pytest.mark.skipif(not CLAUDE_LOGS, reason="no captured Claude Code payloads in the corpus")
@pytest.mark.parametrize("path", CLAUDE_LOGS, ids=lambda p: p.parent.name)
@pytest.mark.parametrize("sanitizer", [None, "pii-only"])
def test_real_claude_code_tool_events_keep_no_commands_paths_or_urls_by_default(path, sanitizer):
    """Regression: with the sanitiser available the receiver used to keep
    ``tool_parameters`` (``cat /Users/.../.env``) after a PII pass that does
    not touch commands or paths."""
    def pii_only(text):  # what a PII sanitiser does to a command line: nothing
        return text.replace("dev.person@example.com", "[EMAIL]")

    payload = _with_tool_events(json.loads(path.read_text(encoding="utf-8")))
    records = _records_from(payload, Redactor(sanitize=pii_only if sanitizer else None))
    tools = {r["function_name"]: r for r in records if r["decision_type"] == "tool_call"}
    assert set(tools) == {"claude_code.tool.Bash", "claude_code.tool.Read", "claude_code.tool.mcp__fs__write"}
    blob = json.dumps(records)
    for sentinel in SECRET_SENTINELS:
        assert sentinel not in blob, sentinel
    for record in tools.values():
        assert "tool_parameters" not in record["inputs"] and "tool_input" not in record["inputs"]
        assert "attributes" not in record["inputs"]
    bash = tools["claude_code.tool.Bash"]
    assert bash["inputs"]["tool_use_id"] == "toolu_real_1" and bash["outputs"]["success"] is True
    assert bash["inputs"]["decision_source"] == "config"
    read = tools["claude_code.tool.Read"]
    assert read["status"] == "error" and read["error"] == "[redacted]"
    assert read["outputs"]["error_type"] == "PermissionError"


@pytest.mark.skipif(not CLAUDE_LOGS, reason="no captured Claude Code payloads in the corpus")
def test_real_claude_code_tool_events_keep_content_only_when_asked():
    payload = _with_tool_events(json.loads(CLAUDE_LOGS[0].read_text(encoding="utf-8")))
    records = _records_from(payload, Redactor(keep_content=True, sanitize=None))
    tools = {r["function_name"]: r for r in records if r["decision_type"] == "tool_call"}
    assert SECRET_COMMAND in tools["claude_code.tool.Bash"]["inputs"]["tool_parameters"]
    assert tools["claude_code.tool.Bash"]["inputs"]["tool_input"] == SECRET_COMMAND
    assert tools["claude_code.tool.Read"]["error"].startswith("EACCES: /Users/")
