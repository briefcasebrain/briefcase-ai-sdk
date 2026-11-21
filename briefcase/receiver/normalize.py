"""Per-source normalisers: OTLP signals and Cursor hook payloads -> events.

An event is a dict with ``source``, ``type`` and ``attrs``, where ``type`` and
the attribute names follow ``briefcase.semantic_conventions.cowork``. Claude
Code and Codex share that vocabulary behind a prefix; Copilot's ``gen_ai.*``
spans and Cursor's hook JSON are translated onto it here.

Codex attribute names are not pinned down by its documentation, so several
aliases are accepted (``_ALIASES``); unknown attributes are harmless because
the record mapper only passes an allow-list through.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from briefcase.semantic_conventions import cowork as cw

SERVICE_SOURCES = {
    "claude-code": "claude_code",
    "cowork": "cowork",
    "copilot-chat": "copilot",
    "github-copilot": "copilot",
    "gemini-cli": "gemini",
    "cline": "cline",
}

_TYPE_ALIASES = {
    "agent_response": cw.EVENT_ASSISTANT_RESPONSE,  # Codex
    "conversation_starts": "session_start",  # Codex
}

# alias -> canonical attribute name
_ALIASES = {
    "conversation.id": cw.SESSION_ID,
    "conversation_id": cw.SESSION_ID,
    "call_id": cw.TOOL_USE_ID,
    "input_token_count": cw.API_INPUT_TOKENS,
    "output_token_count": cw.API_OUTPUT_TOKENS,
    "cached_token_count": cw.API_CACHE_READ_TOKENS,
    "reasoning_token_count": "reasoning_tokens",
    "tool_token_count": "tool_tokens",
    "arguments": cw.TOOL_PARAMETERS,
    "output": "tool_output",
    "http.response.status_code": cw.API_ERROR_STATUS_CODE,
    "error.message": cw.TOOL_ERROR,
}

_IDENTIFIER = re.compile(r"^[A-Za-z_][\w.]{1,80}$")

GEN_AI_MODEL_OPS = frozenset({"chat", "text_completion", "generate_content", "embeddings"})


def _time(signal: Dict[str, Any], attrs: Dict[str, Any]) -> datetime:
    stamp = attrs.get(cw.EVENT_TIMESTAMP)
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    nanos = signal.get("time_ns")
    if nanos:
        try:
            return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(timezone.utc)


def source_of(service: Any, name: str = "") -> str:
    for source, prefix in cw.SOURCE_EVENT_PREFIXES.items():
        if name.startswith(prefix):
            return source
    if isinstance(service, str) and service:
        if service in SERVICE_SOURCES:
            return SERVICE_SOURCES[service]
        if service.lower().startswith("codex"):
            return "codex"
        return re.sub(r"[^a-z0-9_]+", "_", service.lower())[:40] or "unknown"
    return "unknown"


def _merged(signal: Dict[str, Any]) -> Dict[str, Any]:
    attrs = dict(signal.get("resource") or {})
    attrs.update(signal.get("attrs") or {})
    for alias, canonical in _ALIASES.items():
        if alias in attrs and canonical not in attrs:
            attrs[canonical] = attrs[alias]
    return attrs


def normalize_log(signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attrs = _merged(signal)
    name = attrs.get("event.name") or signal.get("name")
    if not isinstance(name, str) or not name:
        body = signal.get("body")
        name = body if isinstance(body, str) and _IDENTIFIER.match(body) else ""
    if not name:
        return None
    source = source_of(attrs.get(cw.SERVICE_NAME), name)
    prefix = cw.SOURCE_EVENT_PREFIXES.get(source, "")
    kind = name[len(prefix):] if prefix and name.startswith(prefix) else name
    kind = _TYPE_ALIASES.get(kind, kind)
    return {
        "source": source, "type": kind, "attrs": attrs, "time": _time(signal, attrs),
        "trace_id": signal.get("trace_id"), "span_id": signal.get("span_id"),
    }


def normalize_span(signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``gen_ai.*`` spans (Copilot Chat and any other GenAI-semconv emitter)."""
    attrs = _merged(signal)
    operation = attrs.get("gen_ai.operation.name")
    if not isinstance(operation, str):
        return None  # not a GenAI span; nothing to map
    out = dict(attrs)
    for src, dst in (
        ("gen_ai.conversation.id", cw.SESSION_ID),
        ("gen_ai.usage.input_tokens", cw.API_INPUT_TOKENS),
        ("gen_ai.usage.output_tokens", cw.API_OUTPUT_TOKENS),
        ("gen_ai.usage.cache_read.input_tokens", cw.API_CACHE_READ_TOKENS),
        ("gen_ai.usage.cache_creation.input_tokens", cw.API_CACHE_CREATION_TOKENS),
        ("gen_ai.usage.reasoning.output_tokens", "reasoning_tokens"),
        ("gen_ai.tool.name", cw.TOOL_NAME),
        ("gen_ai.tool.call.id", cw.TOOL_USE_ID),
        ("error.type", cw.TOOL_ERROR_TYPE),
    ):
        if src in attrs and dst not in out:
            out[dst] = attrs[src]
    model = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model")
    if model is not None:
        out.setdefault(cw.API_MODEL, model)
    start, end = signal.get("start_ns"), signal.get("time_ns")
    if start and end and end >= start:
        out[cw.API_DURATION_MS] = (end - start) / 1e6
    failed = signal.get("status_code") in (2, "STATUS_CODE_ERROR") or "error.type" in attrs
    if operation in GEN_AI_MODEL_OPS:
        kind = cw.EVENT_API_ERROR if failed else cw.EVENT_API_REQUEST
    elif operation == "execute_tool":
        kind = cw.EVENT_TOOL_RESULT
        out.setdefault(cw.TOOL_SUCCESS, not failed)
    else:
        kind = operation
    return {
        "source": source_of(attrs.get(cw.SERVICE_NAME)), "type": kind, "attrs": out,
        "time": _time(signal, {}), "trace_id": signal.get("trace_id"),
        "span_id": signal.get("span_id"), "parent_span_id": signal.get("parent_span_id"),
    }


def normalize_metric(signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = signal.get("name")
    if not name:
        return None
    attrs = _merged(signal)
    return {
        "source": source_of(attrs.get(cw.SERVICE_NAME), name), "type": "metric", "attrs": attrs,
        "time": _time(signal, {}), "metric": name, "value": signal.get("value"), "unit": signal.get("unit"),
    }


_CURSOR_TYPES = {
    "beforeSubmitPrompt": cw.EVENT_USER_PROMPT,
    "postToolUse": cw.EVENT_TOOL_RESULT,
    "postToolUseFailure": cw.EVENT_TOOL_RESULT,
    "afterAgentResponse": cw.EVENT_ASSISTANT_RESPONSE,
    "sessionStart": "session_start",
    "sessionEnd": "session_end",
}


def normalize_cursor(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A Cursor hook's stdin JSON (as forwarded by ``briefcase hook cursor``)."""
    hook = payload.get("hook_event_name")
    if not isinstance(hook, str) or not _IDENTIFIER.match(hook):
        return None
    attrs: Dict[str, Any] = {"hook_event_name": hook}
    for src, dst in (
        ("conversation_id", cw.SESSION_ID), ("generation_id", cw.PROMPT_ID), ("model", cw.API_MODEL),
        ("cursor_version", "app.version"), ("user_email", cw.USER_EMAIL),
        ("workspace_roots", cw.WORKSPACE_HOST_PATHS), ("tool_name", cw.TOOL_NAME),
        ("tool_use_id", cw.TOOL_USE_ID), ("tool_input", cw.TOOL_INPUT), ("tool_output", "tool_output"),
        ("duration", cw.TOOL_DURATION_MS), ("duration_ms", cw.TOOL_DURATION_MS),
        ("error_message", cw.TOOL_ERROR), ("failure_type", cw.TOOL_ERROR_TYPE),
        ("prompt", cw.PROMPT_TEXT), ("prompt_length", cw.PROMPT_LENGTH),
        ("text", cw.RESPONSE_TEXT), ("text_length", cw.RESPONSE_LENGTH),
        ("reason", "reason"), ("final_status", "final_status"), ("status", "status"),
        ("loop_count", "loop_count"), ("composer_mode", "composer_mode"),
        ("is_background_agent", "is_background_agent"), ("transcript_path", "transcript_path"),
    ):
        if payload.get(src) is not None and dst not in attrs:
            attrs[dst] = payload[src]
    kind = _CURSOR_TYPES.get(hook, hook)
    if kind == cw.EVENT_USER_PROMPT and isinstance(attrs.get(cw.PROMPT_TEXT), str):
        attrs.setdefault(cw.PROMPT_LENGTH, len(attrs[cw.PROMPT_TEXT]))
    if kind == cw.EVENT_ASSISTANT_RESPONSE and isinstance(attrs.get(cw.RESPONSE_TEXT), str):
        attrs.setdefault(cw.RESPONSE_LENGTH, len(attrs[cw.RESPONSE_TEXT]))
    if kind == cw.EVENT_TOOL_RESULT:
        attrs[cw.TOOL_SUCCESS] = hook != "postToolUseFailure"
    return {"source": "cursor", "type": kind, "attrs": attrs, "time": datetime.now(timezone.utc)}
