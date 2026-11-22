"""`POST /v1/policy/decisions` -> a `policy.tool_call` decision record built from an allow-list."""

import json
from datetime import datetime, timezone

from briefcase.exporters import MemoryExporter
from briefcase.receiver import Receiver
from briefcase.receiver.policy import DECISION_TYPE, record_from_decision
from briefcase.receiver.records import Redactor

HASH = "a" * 64
DECISION = {"ts": "2026-11-22T10:00:00+0000", "tool": "claude-code", "event": "PreToolUse", "kind": "shell",
            "tool_name": "Bash", "session": "s1", "tool_use_id": "toolu_1", "target_hmac_sha256": HASH,
            "effect": "deny", "would": "deny", "mode": "enforce", "rule": "shell.rm-root", "source": "preset",
            "reason": "Recursive delete of a root, home or system directory.", "ms": 12.5}
NOW = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)


def test_record_shape():
    record = record_from_decision(dict(DECISION), Redactor(sanitize=None), now=NOW)
    assert record["decision_type"] == DECISION_TYPE == "policy.tool_call"
    assert record["function_name"] == "claude-code.shell"
    assert record["inputs"] == {"source": "claude-code", "tool_kind": "shell", "hook_event_name": "PreToolUse",
                                "tool_name": "Bash", "session_id": "s1", "tool_use_id": "toolu_1",
                                "target_hmac_sha256": HASH}
    assert record["outputs"] == {"effect": "deny", "enforced": True, "would": "deny", "mode": "enforce",
                                 "rule_id": "shell.rm-root", "rule_source": "preset",
                                 "reason": "Recursive delete of a root, home or system directory."}
    assert record["execution_time_ms"] == 12.5 and record["ended_at"].startswith("2026-11-22T10:00:00")


def test_content_needs_both_switches():
    payload = dict(DECISION, target="rm -rf /", unit="command: rm -rf /")
    assert "target" not in record_from_decision(payload, Redactor(sanitize=None))["inputs"]
    kept = record_from_decision(payload, Redactor(keep_content=True, sanitize=None))["inputs"]
    assert kept["target"] == "rm -rf /" and kept["unit"] == "command: rm -rf /"


def test_unknown_fields_and_bad_values_are_dropped():
    payload = dict(DECISION, extra="x", rule="bad\nrule", source="elsewhere", target_hmac_sha256="nothex",
                   ms=-5, effect="deny")
    record = record_from_decision(payload, Redactor(sanitize=None))
    assert "extra" not in json.dumps(record) and "rule_id" not in record["outputs"]
    assert "rule_source" not in record["outputs"] and "target_hmac_sha256" not in record["inputs"]
    assert record["execution_time_ms"] == 0.0
    for broken in ({}, [], dict(DECISION, tool="vim"), dict(DECISION, effect="maybe"), dict(DECISION, kind="x")):
        assert record_from_decision(broken, Redactor(sanitize=None)) is None


def test_hook_error_becomes_an_error_record():
    payload = {"tool": "cursor", "kind": "error", "effect": "allow", "rule": "fail_open", "source": "setting",
               "error": "the hook failed (RuntimeError: x)"}
    record = record_from_decision(payload, Redactor(sanitize=None))
    # The message can quote a host path, so without a sanitizer it is redacted like any error text.
    assert record["status"] == "error" and record["error"] == "[redacted]"
    assert record["outputs"]["enforced"] is False
    kept = record_from_decision(payload, Redactor(keep_content=True, sanitize=None))
    assert "RuntimeError" in kept["error"]


def test_endpoint():
    memory = MemoryExporter()
    receiver = Receiver(memory, async_export=False)
    status, _, _ = receiver.handle_post("/v1/policy/decisions", "application/json", json.dumps(DECISION).encode())
    assert status == 200 and memory.records[-1]["decision_type"] == "policy.tool_call"
    status, _, _ = receiver.handle_post("/v1/policy/decisions", "application/x-protobuf", b"\x00")
    assert status == 415
    status, _, _ = receiver.handle_post("/v1/policy/decisions", "application/json", b'{"tool": "nope"}')
    assert status == 200 and len(memory.records) == 1 and receiver.stats["unmapped"] == 1
