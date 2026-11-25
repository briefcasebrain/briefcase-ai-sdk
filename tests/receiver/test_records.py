"""Normalise + map: synthetic payloads per source -> decision records."""

import json
from pathlib import Path

import pytest

from briefcase.receiver import normalize, otlp
from briefcase.receiver.records import REDACTED, SCHEMA_VERSION, RecordMapper, Redactor

from tests.receiver import payloads as p

FIXTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "decision_record.json").read_text())


def run(kind, payload, **mapper_kwargs):
    redactor = mapper_kwargs.pop("redactor", None) or Redactor(sanitize=None)
    mapper = RecordMapper(redactor, **mapper_kwargs)
    normaliser = {"logs": normalize.normalize_log, "traces": normalize.normalize_span,
                  "metrics": normalize.normalize_metric}[kind]
    records = []
    for signal in otlp.parse(kind, payload):
        event = normaliser(signal)
        if event is not None:
            records.extend(mapper.ingest(event))
    records.extend(mapper.flush())
    return records


def cursor_records(hostile=False, **mapper_kwargs):
    mapper = RecordMapper(mapper_kwargs.pop("redactor", None) or Redactor(sanitize=None), **mapper_kwargs)
    records = []
    for payload in p.cursor_hooks(hostile=hostile):
        records.extend(mapper.ingest(normalize.normalize_cursor(payload)))
    return records + mapper.flush()


def by_function(records):
    return {record["function_name"]: record for record in records}


def all_sources():
    return {
        "claude_code": run("logs", p.claude_code_logs()),
        "cowork": run("logs", p.cowork_logs()),
        "codex": run("logs", p.codex_logs()),
        "copilot": run("traces", p.copilot_traces()),
        "metrics": run("metrics", p.claude_code_metrics(), emit_metrics=True),
        "cursor": cursor_records(),
    }


def test_schema_version_matches_capture():
    from briefcase.decorators import SCHEMA_VERSION as capture_version

    assert SCHEMA_VERSION == capture_version


def test_records_use_only_the_capture_record_fields():
    allowed = set(FIXTURE["required_fields"]) | set(FIXTURE["optional_fields"])
    required = set(FIXTURE["required_fields"])
    for source, records in all_sources().items():
        assert records, source
        for record in records:
            assert required <= set(record) <= allowed, (source, sorted(set(record) - allowed))
            assert record["inputs"]["source"]
            json.dumps(record)  # every record is plain JSON


def test_claude_code_api_request_becomes_a_model_call():
    record = by_function(run("logs", p.claude_code_logs()))["claude_code.api_request"]
    assert record["decision_type"] == "model_call"
    assert record["inputs"] == {
        "source": "claude_code", "session_id": p.SESSION, "organization_id": "org-1", "user_id": "anon-device-id",
        "user_account_uuid": "acct-uuid-1", "terminal_type": "iTerm.app", "service_version": "1.2.3",
        "user_email_hmac_sha256": record["inputs"]["user_email_hmac_sha256"], "prompt_id": p.PROMPT_ID,
        "event_sequence": 1, "model": "claude-sonnet-4-5", "request_id": "req_1", "speed": "normal",
    }
    assert record["outputs"] == {"input_tokens": 1200, "output_tokens": 340, "cache_read_tokens": 1000,
                                 "cache_creation_tokens": 50, "cost_usd": 0.0123}
    assert record["execution_time_ms"] == 1500.0 and record["workflow_id"] == p.PROMPT_ID
    assert record["started_at"] < record["ended_at"] and record["ended_at"].endswith("+00:00")
    assert len(record["inputs"]["user_email_hmac_sha256"]) == 64


def test_claude_code_tool_decision_and_result_join_on_tool_use_id():
    records = run("logs", p.claude_code_logs())
    bash = by_function(records)["claude_code.tool.Bash"]
    assert bash["decision_type"] == "tool_call"
    assert bash["inputs"]["tool_use_id"] == "toolu_1" and bash["inputs"]["decision"] == "accept"
    assert bash["inputs"]["decision_source"] == "config"
    assert bash["outputs"] == {"success": True, "tool_result_size_bytes": 2048}
    assert bash["execution_time_ms"] == 250.0 and "status" not in bash
    assert sum(r["function_name"] == "claude_code.tool.Bash" for r in records) == 1

    rejected = by_function(records)["claude_code.tool.Write"]
    assert rejected["status"] == "rejected" and rejected["inputs"]["decision"] == "reject"

    failed = by_function(records)["claude_code.tool.Read"]  # a result with no decision still records
    assert failed["status"] == "error" and failed["error"] == REDACTED
    assert failed["outputs"] == {"success": False, "error_type": "PermissionError"}


def test_claude_code_prompt_group_and_other_events():
    records = run("logs", p.claude_code_logs())
    assert [r["decision_type"] for r in records] == [
        "user_prompt", "model_call", "tool_call", "tool_call", "tool_call", "assistant_response",
        "model_call", "model_call"]
    assert {r["workflow_id"] for r in records} == {p.PROMPT_ID}
    functions = by_function(records)
    assert functions["claude_code.user_prompt"]["inputs"]["prompt_length"] == len(p.PROMPT)
    assert functions["claude_code.assistant_response"]["outputs"] == {"response_length": len(p.RESPONSE)}
    error = functions["claude_code.api_error"]
    assert error["status"] == "error" and error["error"] == REDACTED and error["outputs"]["status_code"] == 529
    assert error["inputs"]["attempt"] == 2
    assert functions["claude_code.api_refusal"]["status"] == "refused"


def test_cowork_bare_event_names_map_the_same_way():
    records = run("logs", p.cowork_logs())
    assert {r["inputs"]["source"] for r in records} == {"cowork"}
    assert "cowork.api_request" in by_function(records) and "cowork.tool.Bash" in by_function(records)


def test_codex_merges_tokens_joins_on_call_id_and_groups_by_prompt():
    records = run("logs", p.codex_logs())
    functions = by_function(records)
    assert [r["decision_type"] for r in records] == [
        "event", "user_prompt", "model_call", "tool_call", "assistant_response"]
    model = functions["codex.api_request"]
    assert model["inputs"]["model"] == "gpt-5-codex" and model["inputs"]["session_id"] == "conv-77"
    assert model["outputs"] == {"input_tokens": 900, "output_tokens": 120, "cache_read_tokens": 500,
                                "reasoning_tokens": 64, "status_code": 200}
    assert model["execution_time_ms"] == 800.0
    tool = functions["codex.tool.shell"]
    assert tool["inputs"]["tool_use_id"] == "call_1" and tool["inputs"]["decision"] == "approved"
    assert tool["outputs"] == {"success": True}
    # Codex sends no prompt.id: the user_prompt opens a group the later events join.
    group = functions["codex.user_prompt"]["workflow_id"]
    assert group and {r.get("workflow_id") for r in records[1:]} == {group}
    assert "workflow_id" not in records[0]
    assert functions["codex.session_start"]["inputs"]["model"] == "gpt-5-codex"


def test_a_codex_request_with_no_completion_is_still_emitted_on_flush():
    payload = p.codex_logs()
    records_in = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"] = records_in[2:3]
    (record,) = run("logs", payload)
    assert record["function_name"] == "codex.api_request" and record["outputs"] == {"status_code": 200}


def test_copilot_gen_ai_spans():
    records = run("traces", p.copilot_traces())
    assert [r["function_name"] for r in records] == [
        "copilot.invoke_agent", "copilot.api_request", "copilot.tool.readFile"]
    chat = records[1]
    assert chat["decision_type"] == "model_call" and chat["inputs"]["model"] == "gpt-4o-2024-08-06"
    assert chat["outputs"] == {"input_tokens": 420, "output_tokens": 77, "cache_read_tokens": 100}
    assert chat["execution_time_ms"] == 1200.0
    assert chat["trace_id"] == "0af7651916cd43dd8448eb211c80319c" and chat["parent_span_id"] == "00f067aa0ba902b7"
    assert chat["inputs"]["session_id"] == "conv-copilot-1"
    tool = records[2]
    assert tool["decision_type"] == "tool_call" and tool["inputs"]["tool_use_id"] == "call_abc"
    assert tool["outputs"] == {"success": True} and tool["execution_time_ms"] == 50.0


def test_metrics_are_dropped_unless_asked_for():
    assert run("metrics", p.claude_code_metrics()) == []
    records = run("metrics", p.claude_code_metrics(), emit_metrics=True)
    assert [(r["function_name"], r["outputs"]["value"]) for r in records] == [
        ("claude_code.token.usage", 1200), ("claude_code.token.usage", 340),
        ("claude_code.cost.usage", 0.0123), ("claude_code.session.count", 1)]
    assert records[0]["decision_type"] == "metric" and records[0]["inputs"]["type"] == "input"


def test_cursor_hook_events():
    records = cursor_records()
    assert [r["function_name"] for r in records] == [
        "cursor.session_start", "cursor.user_prompt", "cursor.tool.Shell", "cursor.assistant_response", "cursor.stop"]
    tool = records[2]
    assert tool["inputs"]["tool_use_id"] == "tu_1" and tool["execution_time_ms"] == 73.0
    assert tool["inputs"]["session_id"] == "cur-conv-1" and tool["workflow_id"] == "cur-gen-1"
    assert records[1]["inputs"]["prompt_length"] == len(p.PROMPT)
    assert records[3]["outputs"] == {"response_length": len(p.RESPONSE)}
    assert records[4]["inputs"]["status"] == "completed"


def test_redaction_is_on_by_default_for_every_source():
    for source, records in all_sources().items():
        blob = json.dumps(records)
        for sentinel in p.SENTINELS:
            assert sentinel not in blob, (source, sentinel)
        assert "tool_parameters" not in blob and "host_paths" not in blob, source


def test_a_sanitizer_never_lets_commands_or_paths_through():
    """The compiled Sanitizer rewrites PII patterns, not commands or paths:
    with it present, redacted records used to keep ``tool_parameters``
    (``cat /Users/.../.env``). Free text is dropped whatever it returns."""
    def pii_only_sanitizer(text):  # what the real one does to a command line
        return text.replace(p.EMAIL, "[EMAIL]")

    redactor = Redactor(sanitize=pii_only_sanitizer)
    sources = {
        "claude_code": run("logs", p.claude_code_logs(), redactor=redactor),
        "cowork": run("logs", p.cowork_logs(), redactor=redactor),
        "codex": run("logs", p.codex_logs(hostile=True), redactor=redactor),
        "copilot": run("traces", p.copilot_traces(hostile=True), redactor=redactor),
        "cursor": cursor_records(hostile=True, redactor=redactor),
    }
    assert any(r["function_name"] == "copilot.tool.runInTerminal" for r in sources["copilot"])
    assert sum(r["function_name"] == "cursor.tool.Shell" for r in sources["cursor"]) == 2
    assert sum(r["function_name"] == "codex.tool.shell" for r in sources["codex"]) == 2
    for source, records in sources.items():
        assert records, source
        blob = json.dumps(records)
        for sentinel in p.SENTINELS:
            assert sentinel not in blob, (source, sentinel)
        for record in records:
            assert "tool_parameters" not in record["inputs"], source
            assert "tool_input" not in record["inputs"] and "tool_output" not in record["outputs"], source
    functions = by_function(sources["claude_code"])
    assert functions["claude_code.tool.Read"]["error"] == REDACTED
    assert functions["claude_code.tool.Read"]["outputs"]["error_type"] == "PermissionError"
    assert functions["claude_code.api_error"]["error"] == REDACTED


def test_free_text_is_dropped_by_default_with_or_without_the_sanitizer():
    assert Redactor().text("anything at all") is None
    assert Redactor(sanitize=lambda text: text).text("cat /etc/passwd") is None
    assert Redactor(keep_content=True, sanitize=None).text("cat /etc/passwd") == "cat /etc/passwd"


@pytest.mark.parametrize("value", [
    p.TOOL_INPUT, p.COMMAND, p.HOST_PATH, p.WINDOWS_PATH, "~/secret/.env", "./run.sh",
    "https://internal.corp.example/x", "dev.person@example.com", "a/b/c", "two words", "x" * 201,
])
def test_label_fields_reject_commands_paths_urls_and_emails(value):
    from briefcase.receiver.records import _label
    assert _label(value) is None


@pytest.mark.parametrize("value", [
    "claude-sonnet-4-5", "gpt-4o-2024-08-06", "openai/gpt-4o", "anthropic.claude-v2:0",
    "mcp__github__create_issue", "toolu_01AbC", "5b1c2f9e-0000-4000-8000-000000000001", "user_reject",
])
def test_label_fields_keep_identifiers(value):
    from briefcase.receiver.records import _label
    assert _label(value) == value


def test_email_hash_is_keyed_and_stable():
    a, b = Redactor(salt=b"k" * 32), Redactor(salt=b"z" * 32)
    assert a.email_hash("Dev@Example.com ") == a.email_hash("dev@example.com")
    assert a.email_hash("dev@example.com") != b.email_hash("dev@example.com")


def test_keep_content_passes_content_through():
    records = run("logs", p.claude_code_logs(), redactor=Redactor(keep_content=True, sanitize=None))
    functions = by_function(records)
    assert functions["claude_code.user_prompt"]["inputs"]["prompt"] == p.PROMPT
    assert functions["claude_code.assistant_response"]["outputs"]["response"] == p.RESPONSE
    assert functions["claude_code.tool.Bash"]["inputs"]["tool_input"] == p.TOOL_INPUT
    assert functions["claude_code.tool.Read"]["error"].startswith(p.TOOL_ERROR)
    assert p.COMMAND in functions["claude_code.tool.Bash"]["inputs"]["tool_parameters"] or \
        p.TOOL_INPUT in functions["claude_code.tool.Bash"]["inputs"]["tool_parameters"]
    assert functions["claude_code.api_request"]["inputs"]["user_email"] == p.EMAIL
    assert functions["claude_code.api_request"]["inputs"]["attributes"]["workspace.host_paths"] == [p.HOST_PATH]


def test_pending_tool_decisions_expire_into_records():
    now = [0.0]
    mapper = RecordMapper(Redactor(sanitize=None), clock=lambda: now[0], ttl=10)
    signals = otlp.parse("logs", p.claude_code_logs())
    assert mapper.ingest(normalize.normalize_log(signals[2])) == []  # accept decision waits for its result
    now[0] = 11
    (expired, prompt) = mapper.ingest(normalize.normalize_log(signals[0]))
    assert expired["function_name"] == "claude_code.tool.Bash" and expired["status"] == "no_result"


@pytest.mark.parametrize("payload", [
    {}, {"resourceLogs": "x"}, {"resourceLogs": [None, {"scopeLogs": [{"logRecords": [None, {}, {"attributes": 3}]}]}]},
    {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"body": {"stringValue": "has spaces so not an event"}}]}]}]},
])
def test_junk_payloads_produce_nothing_and_do_not_raise(payload):
    assert run("logs", payload) == []
    assert run("traces", payload) == []
    assert run("metrics", payload) == []


def test_any_value_shapes():
    assert otlp.attributes(p.attrs({"a": 1, "b": 1.5, "c": True, "d": "x", "e": [1, "y"]})) == {
        "a": 1, "b": 1.5, "c": True, "d": "x", "e": [1, "y"]}
    assert otlp.any_value({"kvlistValue": {"values": p.attrs({"k": "v"})}}) == {"k": "v"}
    assert otlp.any_value({"intValue": "nope"}) is None and otlp.any_value("x") is None
