"""SYNTHETIC OTLP payloads for the receiver tests.

These are hand-built from the event and attribute names in each tool's public
documentation. They are not captures from real tools: a real export carries
more attributes, and Codex's attribute names in particular are an assumption.
The sentinel strings below are what the redaction tests search for.
"""

PROMPT = "please refactor the SECRET-PROMPT-TEXT module"
RESPONSE = "here is SECRET-RESPONSE-TEXT"
EMAIL = "dev.person@example.com"
HOST_PATH = "/Users/dev.person/secret-project"
TOOL_INPUT = "cat /Users/dev.person/secret-project/.env"
TOOL_ERROR = "permission denied for dev.person@example.com"
# What real tool calls carry: a command line with a URL and a credential, a
# Windows path, and a path in a field that normally holds a short label.
COMMAND = "curl -H 'Authorization: Bearer sk-live-TOKEN123' https://internal.corp.example/api/v1/keys"
WINDOWS_PATH = "C:\\Users\\dev.person\\secret-project\\notes.txt"
SENTINELS = (PROMPT, "SECRET-PROMPT-TEXT", RESPONSE, "SECRET-RESPONSE-TEXT", EMAIL, "dev.person",
             HOST_PATH, "secret-project", "RAW-API-BODY", ".env", "cat ", "curl", "sk-live-TOKEN123",
             "internal.corp.example", "https://", "Authorization")

SESSION = "sess-0001"
PROMPT_ID = "5b1c2f9e-0000-4000-8000-000000000001"
T0 = 1_790_000_000_000_000_000  # ns


def _value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_value(v) for v in value]}}
    return {"stringValue": str(value)}


def attrs(mapping):
    return [{"key": key, "value": _value(value)} for key, value in mapping.items()]


def logs(service, records, resource=None):
    resource_attrs = {"service.name": service, "service.version": "1.2.3"}
    resource_attrs.update(resource or {})
    return {"resourceLogs": [{
        "resource": {"attributes": attrs(resource_attrs)},
        "scopeLogs": [{"scope": {"name": "synthetic"}, "logRecords": records}],
    }]}


def log(body, attributes, offset_ms=0):
    return {"timeUnixNano": str(T0 + offset_ms * 1_000_000), "body": {"stringValue": body},
            "attributes": attrs(attributes)}


def _claude_common(extra):
    base = {
        "session.id": SESSION, "prompt.id": PROMPT_ID, "organization.id": "org-1", "user.id": "anon-device-id",
        "user.account_uuid": "acct-uuid-1", "user.email": EMAIL, "terminal.type": "iTerm.app",
        "workspace.host_paths": [HOST_PATH],
    }
    base.update(extra)
    return base


def claude_code_logs(prefixed=True):
    """One prompt: user_prompt, api_request, tool_decision + tool_result, a
    failing tool, assistant_response, api_error and api_refusal. All content
    gates are shown as if the user had enabled them, so redaction is tested
    against the worst case."""
    prefix = "claude_code." if prefixed else ""
    service = "claude-code" if prefixed else "cowork"

    def event(name, extra, offset):
        return log(prefix + name, _claude_common(dict(extra, **{"event.name": name, "event.sequence": offset})), offset)

    return logs(service, [
        event("user_prompt", {"prompt": PROMPT, "prompt_text": PROMPT, "prompt_length": len(PROMPT)}, 0),
        event("api_request", {
            "model": "claude-sonnet-4-5", "cost_usd": 0.0123, "duration_ms": 1500, "input_tokens": 1200,
            "output_tokens": 340, "cache_read_tokens": 1000, "cache_creation_tokens": 50,
            "request_id": "req_1", "speed": "normal", "api_request_body": "RAW-API-BODY",
        }, 1),
        event("tool_decision", {"tool_name": "Bash", "tool_use_id": "toolu_1", "decision": "accept",
                                "source": "config"}, 2),
        event("tool_result", {
            "tool_name": "Bash", "tool_use_id": "toolu_1", "success": "true", "duration_ms": 250,
            "tool_result_size_bytes": 2048, "tool_parameters": '{"bash_command": "' + TOOL_INPUT + '"}',
            "tool_input": TOOL_INPUT, "decision_type": "accept", "decision_source": "config",
            "full_command": COMMAND, "file_path": WINDOWS_PATH, "url": "https://internal.corp.example/x",
            "mcp_server_scope": HOST_PATH,
        }, 3),
        event("tool_decision", {"tool_name": "Write", "tool_use_id": "toolu_2", "decision": "reject",
                                "source": "user_reject"}, 4),
        event("tool_result", {"tool_name": "Read", "tool_use_id": "toolu_3", "success": "false",
                              "duration_ms": 12, "error": TOOL_ERROR + " while running " + COMMAND,
                              "error_type": "PermissionError"}, 5),
        event("assistant_response", {"response": RESPONSE, "response_length": len(RESPONSE),
                                     "model": "claude-sonnet-4-5"}, 6),
        event("api_error", {"model": "claude-sonnet-4-5", "error": "overloaded: " + EMAIL, "status_code": 529,
                            "duration_ms": 90, "attempt": 2}, 7),
        event("api_refusal", {"model": "claude-sonnet-4-5", "request_id": "req_9"}, 8),
    ])


def cowork_logs():
    return claude_code_logs(prefixed=False)


def codex_logs(hostile=False):
    """Codex: token counts arrive on a later sse_event; no prompt.id."""
    common = {"conversation.id": "conv-77", "model": "gpt-5-codex", "user.email": EMAIL, "app.version": "0.50.0"}

    def event(name, extra, offset):
        return log("codex." + name, dict(common, **dict(extra, **{"event.name": "codex." + name})), offset)

    return logs("codex_cli_rs", [
        event("conversation_starts", {"approval_policy": "on-request", "sandbox_policy": "workspace-write"}, 0),
        event("user_prompt", {"prompt_length": len(PROMPT), "prompt": PROMPT}, 1),
        event("api_request", {"attempt": 1, "duration_ms": 800, "http.response.status_code": 200}, 2),
        event("sse_event", {"event.kind": "response.output_text.delta", "duration_ms": 3}, 3),
        event("sse_event", {"event.kind": "response.completed", "input_token_count": 900,
                            "output_token_count": 120, "cached_token_count": 500,
                            "reasoning_token_count": 64}, 4),
        event("tool_decision", {"tool_name": "shell", "call_id": "call_1", "decision": "approved",
                                "source": "user"}, 5),
        event("tool_result", {"tool_name": "shell", "call_id": "call_1", "arguments": TOOL_INPUT,
                              "duration_ms": 40, "success": "true", "output": HOST_PATH}, 6),
        event("agent_response", {"response": RESPONSE}, 7),
    ] + ([
        event("tool_result", {"tool_name": "shell", "call_id": "call_2", "duration_ms": 5, "success": "false",
                              "arguments": '{"command": ["bash", "-lc", "' + COMMAND + '"], "workdir": "'
                                           + HOST_PATH + '"}',
                              "error.message": "exit 1: " + COMMAND, "cwd": HOST_PATH}, 8),
    ] if hostile else []))


def copilot_traces(hostile=False):
    trace = "0af7651916cd43dd8448eb211c80319c"
    common = {"gen_ai.conversation.id": "conv-copilot-1"}

    def span(span_id, parent, name, extra, start_ms, end_ms, status=None):
        out = {
            "traceId": trace, "spanId": span_id, "name": name,
            "startTimeUnixNano": str(T0 + start_ms * 1_000_000), "endTimeUnixNano": str(T0 + end_ms * 1_000_000),
            "attributes": attrs(dict(common, **extra)),
        }
        if parent:
            out["parentSpanId"] = parent
        if status:
            out["status"] = status
        return out

    return {"resourceSpans": [{
        "resource": {"attributes": attrs({"service.name": "copilot-chat"})},
        "scopeSpans": [{"spans": [
            span("00f067aa0ba902b7", "", "invoke_agent copilot", {
                "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "copilot",
                "gen_ai.input.messages": PROMPT, "gen_ai.output.messages": RESPONSE}, 0, 5000),
            span("00f067aa0ba902b8", "00f067aa0ba902b7", "chat gpt-4o", {
                "gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o",
                "gen_ai.response.model": "gpt-4o-2024-08-06", "gen_ai.usage.input_tokens": 420,
                "gen_ai.usage.output_tokens": 77, "gen_ai.usage.cache_read.input_tokens": 100}, 100, 1300),
            span("00f067aa0ba902b9", "00f067aa0ba902b7", "execute_tool readFile", {
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "readFile",
                "gen_ai.tool.call.id": "call_abc", "gen_ai.tool.call.arguments": TOOL_INPUT,
                "gen_ai.tool.call.result": HOST_PATH,
                "github.copilot.tool.parameters.file_path": HOST_PATH}, 1400, 1450),
            span("00f067aa0ba902ba", "", "GET /unrelated", {"http.request.method": "GET"}, 0, 5),
        ] + ([
            span("00f067aa0ba902bb", "00f067aa0ba902b7", "execute_tool runInTerminal", {
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "runInTerminal",
                "gen_ai.tool.call.id": "call_cmd", "gen_ai.tool.type": HOST_PATH,
                "gen_ai.tool.call.arguments": '{"command": "' + COMMAND + '"}',
                "url.full": "https://internal.corp.example/api", "error.type": "ExitCode1",
                "error.message": "failed: " + COMMAND}, 1500, 1600, {"code": 2, "message": COMMAND}),
        ] if hostile else [])}],
    }]}


def claude_code_metrics():
    def metric(name, unit, points):
        return {"name": name, "unit": unit, "sum": {"dataPoints": [
            {"timeUnixNano": str(T0), "attributes": attrs(dict({"session.id": SESSION, "user.email": EMAIL}, **a)), **v}
            for a, v in points]}}

    return {"resourceMetrics": [{
        "resource": {"attributes": attrs({"service.name": "claude-code"})},
        "scopeMetrics": [{"metrics": [
            metric("claude_code.token.usage", "tokens", [
                ({"type": "input", "model": "claude-sonnet-4-5"}, {"asInt": "1200"}),
                ({"type": "output", "model": "claude-sonnet-4-5"}, {"asInt": "340"})]),
            metric("claude_code.cost.usage", "USD", [({"model": "claude-sonnet-4-5"}, {"asDouble": 0.0123})]),
            metric("claude_code.session.count", "count", [({}, {"asInt": "1"})]),
        ]}],
    }]}


def cursor_hooks(hostile=False):
    common = {"conversation_id": "cur-conv-1", "generation_id": "cur-gen-1", "model": "claude-4.5-sonnet",
              "cursor_version": "2.4.0", "workspace_roots": [HOST_PATH], "user_email": EMAIL,
              "transcript_path": HOST_PATH + "/transcript.jsonl"}
    return [
        dict(common, hook_event_name="sessionStart", session_id="cur-conv-1", composer_mode="agent"),
        dict(common, hook_event_name="beforeSubmitPrompt", prompt=PROMPT, attachments=[{"file": HOST_PATH}]),
        dict(common, hook_event_name="postToolUse", tool_name="Shell", tool_use_id="tu_1",
             tool_input={"command": TOOL_INPUT}, tool_output=HOST_PATH, cwd=HOST_PATH, duration=73),
        dict(common, hook_event_name="afterAgentResponse", text=RESPONSE),
        dict(common, hook_event_name="stop", status="completed", loop_count=0),
    ] + ([
        dict(common, hook_event_name="postToolUseFailure", tool_name="Shell", tool_use_id="tu_2",
             tool_input={"command": COMMAND, "working_directory": WINDOWS_PATH}, cwd=HOST_PATH,
             error_message="command failed: " + COMMAND, failure_type="error", duration=5),
        dict(common, hook_event_name="stop", status="completed", loop_count=1, reason=HOST_PATH + "/x.py"),
    ] if hostile else [])
