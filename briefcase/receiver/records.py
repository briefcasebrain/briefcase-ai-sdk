"""Turn normalised events into decision records shaped like ``@capture`` output.

Redaction is on by default and works as an allow-list: only attributes named
here (ids, model, token counts, cost, durations, tool names, outcomes) reach a
record. Prompt text, response text, tool input, raw API bodies and host paths
are never copied; tool parameters and error strings are kept only after the
SDK's ``Sanitizer`` has rewritten them, and dropped when the native extension
is not available. ``user.email`` becomes a keyed hash. ``keep_content=True``
turns all of that off.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from briefcase.semantic_conventions import cowork as cw

SCHEMA_VERSION = "1.1"  # kept equal to briefcase.decorators.SCHEMA_VERSION (tested)
REDACTED = "[redacted]"
PENDING_TTL_SECONDS = 600.0
MAX_PENDING = 10_000

# identity attribute -> record key
_IDENTITY = (
    (cw.SESSION_ID, "session_id"),
    (cw.ORGANIZATION_ID, "organization_id"),
    (cw.USER_ID, "user_id"),
    (cw.USER_ACCOUNT_UUID, "user_account_uuid"),
    ("user.account_id", "user_account_id"),
    (cw.TERMINAL_TYPE, "terminal_type"),
    ("app.version", "app_version"),
    (cw.SERVICE_VERSION, "service_version"),
)

_INT_KEYS = (
    cw.API_INPUT_TOKENS, cw.API_OUTPUT_TOKENS, cw.API_CACHE_READ_TOKENS, cw.API_CACHE_CREATION_TOKENS,
    "cache_creation_5m_tokens", "cache_creation_1h_tokens", "reasoning_tokens", "tool_tokens",
)
_MODEL_INPUT_STRINGS = (
    "request_id", "client_request_id", cw.API_SPEED, "effort", "query_source", "agent.name",
    "skill.name", "plugin.name", "marketplace.name", "mcp_server.name", "mcp_tool.name",
    "gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.system",
)
_TOOL_INPUT_STRINGS = (
    cw.TOOL_USE_ID, "request_id", cw.TOOL_DECISION_TYPE, cw.TOOL_DECISION_SOURCE,
    cw.TOOL_MCP_SERVER_SCOPE, "tool_source", "gen_ai.tool.type",
)
_GENERIC_STRINGS = (
    cw.API_MODEL, cw.TOOL_NAME, "hook_event_name", "event.kind", "reason", "final_status", "status",
    "composer_mode", "command_name", "command_source", "gen_ai.operation.name",
    "gen_ai.agent.name", "request_id",
)
_GENERIC_NUMBERS = (
    cw.API_DURATION_MS, "loop_count", cw.API_ERROR_ATTEMPT, cw.API_ERROR_STATUS_CODE,
    cw.PROMPT_LENGTH, cw.RESPONSE_LENGTH,
) + _INT_KEYS
_REJECT_WORDS = ("reject", "deny", "denied", "block", "abort")


def number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def boolean(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _label(value: Any, limit: int = 200) -> Optional[str]:
    """A short identifier-like string (model, tool name, id); never free text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str) or not value or len(value) > limit or "\n" in value:
        return None
    return value


def _default_sanitize() -> Optional[Callable[[str], Optional[str]]]:
    """``Sanitizer().sanitize(text).sanitized`` when the compiled core is there."""
    try:
        from briefcase.sanitize import Sanitizer
        sanitizer = Sanitizer()
    except Exception:
        return None

    def run(text: str) -> Optional[str]:
        try:
            result = sanitizer.sanitize(text).sanitized
        except Exception:
            return None
        return result if isinstance(result, str) else None

    return run if isinstance(run("probe"), str) else None


def load_salt(path: Optional[Path] = None) -> bytes:
    """A per-install random key for hashing ``user.email``. Stable across
    restarts so the same user maps to the same hash, and never leaves the
    machine, so the hash cannot be reversed by hashing guessed addresses."""
    if path is None:
        return secrets.token_bytes(32)
    try:
        data = path.read_bytes()
        if len(data) >= 16:
            return data
    except OSError:
        pass
    data = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except OSError:
        pass
    return data


class Redactor:
    def __init__(self, keep_content: bool = False,
                 sanitize: Any = "auto", salt: Optional[bytes] = None) -> None:
        self.keep_content = keep_content
        self._sanitize = _default_sanitize() if sanitize == "auto" else sanitize
        self._salt = salt if salt is not None else secrets.token_bytes(32)

    @property
    def sanitizer_available(self) -> bool:
        return self._sanitize is not None

    def email_hash(self, email: str) -> str:
        return hmac.new(self._salt, email.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()

    def text(self, value: Any, limit: int = 4000) -> Optional[str]:
        """Free text that may be kept once sanitised (tool parameters, errors).
        ``None`` means "drop it"."""
        if value is None:
            return None
        if not isinstance(value, str):
            try:
                value = json.dumps(value, default=str)
            except (TypeError, ValueError):
                return None
        if self.keep_content:
            return value[:limit]
        if self._sanitize is None:
            return None
        return (self._sanitize(value[:limit]) or None)

    def identity(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, name in _IDENTITY:
            label = _label(attrs.get(key))
            if label is not None:
                out[name] = label
        email = attrs.get(cw.USER_EMAIL)
        if isinstance(email, str) and email:
            if self.keep_content:
                out["user_email"] = email
            else:
                out["user_email_hmac_sha256"] = self.email_hash(email)
        return out


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class RecordMapper:
    """Stateful event -> record mapping. Not thread-safe; the receiver locks."""

    def __init__(self, redactor: Optional[Redactor] = None, emit_metrics: bool = False,
                 clock: Callable[[], float] = time.monotonic, ttl: float = PENDING_TTL_SECONDS) -> None:
        self.redactor = redactor or Redactor()
        self.emit_metrics = emit_metrics
        self._clock = clock
        self._ttl = ttl
        self._groups: Dict[Tuple[str, str], str] = {}
        self._decisions: Dict[Tuple[str, str, str], Tuple[float, Dict[str, Any]]] = {}
        self._requests: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
        self.dropped = 0

    # -- public ----------------------------------------------------------------
    def ingest(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        records = self._expire()
        kind = event["type"]
        handler = {
            cw.EVENT_USER_PROMPT: self._user_prompt,
            cw.EVENT_API_REQUEST: self._api_request,
            cw.EVENT_API_ERROR: self._api_error,
            cw.EVENT_API_REFUSAL: self._api_refusal,
            cw.EVENT_TOOL_DECISION: self._tool_decision,
            cw.EVENT_TOOL_RESULT: self._tool_result,
            cw.EVENT_ASSISTANT_RESPONSE: self._assistant_response,
            "sse_event": self._sse_event,
            "metric": self._metric,
        }.get(kind, self._generic)
        records.extend(handler(event))
        return records

    def flush(self) -> List[Dict[str, Any]]:
        """Emit everything still waiting for its other half (on shutdown)."""
        return self._expire(everything=True)

    # -- shared pieces ---------------------------------------------------------
    def _session(self, event: Dict[str, Any]) -> str:
        return _label(event["attrs"].get(cw.SESSION_ID)) or ""

    def _group(self, event: Dict[str, Any]) -> Optional[str]:
        prompt_id = _label(event["attrs"].get(cw.PROMPT_ID))
        if prompt_id:
            return prompt_id
        return self._groups.get((event["source"], self._session(event)))

    def _base(self, event: Dict[str, Any], decision_type: str, function_name: str,
              duration_ms: Optional[float] = None) -> Dict[str, Any]:
        attrs = event["attrs"]
        inputs: Dict[str, Any] = {"source": event["source"]}
        inputs.update(self.redactor.identity(attrs))
        group = self._group(event)
        if group:
            inputs["prompt_id"] = group
        sequence = number(attrs.get(cw.EVENT_SEQUENCE))
        if sequence is not None:
            inputs["event_sequence"] = sequence
        if self.redactor.keep_content:
            inputs["attributes"] = attrs
        ended = event["time"]
        duration = duration_ms if duration_ms is not None and duration_ms >= 0 else 0.0
        record: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "decision_id": str(uuid.uuid4()),
            "decision_type": decision_type,
            "function_name": function_name,
            "inputs": inputs,
            "outputs": {},
            "started_at": _iso(ended - timedelta(milliseconds=duration)),
            "ended_at": _iso(ended),
            "execution_time_ms": float(duration),
        }
        if group:
            record["workflow_id"] = group
        if event.get("trace_id"):
            record["trace_id"] = event["trace_id"]
            parent = event.get("parent_span_id") or event.get("span_id")
            if parent:
                record["parent_span_id"] = parent
        return record

    def _copy(self, attrs: Dict[str, Any], target: Dict[str, Any], strings: Any = (), numbers: Any = ()) -> None:
        for key in strings:
            label = _label(attrs.get(key))
            if label is not None:
                target.setdefault(key.replace(".", "_"), label)
        for key in numbers:
            value = number(attrs.get(key))
            if value is not None:
                target.setdefault(key.replace(".", "_"), value)

    def _error(self, record: Dict[str, Any], attrs: Dict[str, Any]) -> None:
        raw = attrs.get(cw.TOOL_ERROR)
        if raw is None or raw == "":
            return
        record["error"] = self.redactor.text(raw, limit=1000) or REDACTED
        record["status"] = "error"

    # -- handlers --------------------------------------------------------------
    def _user_prompt(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        attrs = event["attrs"]
        group = _label(attrs.get(cw.PROMPT_ID)) or str(uuid.uuid4())
        self._groups[(event["source"], self._session(event))] = group
        if len(self._groups) > MAX_PENDING:
            self._groups.pop(next(iter(self._groups)))
        attrs = dict(attrs, **{cw.PROMPT_ID: group})
        event = dict(event, attrs=attrs)
        record = self._base(event, cw.EVENT_USER_PROMPT, f"{event['source']}.{cw.EVENT_USER_PROMPT}")
        self._copy(attrs, record["inputs"], ("command_name", "command_source"), (cw.PROMPT_LENGTH,))
        if self.redactor.keep_content:
            prompt = attrs.get(cw.PROMPT_TEXT, attrs.get(cw.PROMPT_TEXT_ALT))
            if prompt is not None:
                record["inputs"]["prompt"] = prompt
        return [record]

    def _model_record(self, event: Dict[str, Any]) -> Dict[str, Any]:
        attrs = event["attrs"]
        record = self._base(event, "model_call", f"{event['source']}.{cw.EVENT_API_REQUEST}",
                            number(attrs.get(cw.API_DURATION_MS)))
        self._copy(attrs, record["inputs"], (cw.API_MODEL,) + _MODEL_INPUT_STRINGS, (cw.API_ERROR_ATTEMPT,))
        self._copy(attrs, record["outputs"], (), _INT_KEYS + (cw.API_COST_USD, cw.API_ERROR_STATUS_CODE))
        return record

    def _api_request(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        if event["source"] != "codex":
            return [self._model_record(event)]
        # Codex reports token counts on a later sse_event; hold the request so
        # the two become one record.
        out = []
        key = (event["source"], self._session(event))
        previous = self._requests.pop(key, None)
        if previous:
            out.append(self._finish_request(previous[1]))
        self._requests[key] = (self._clock(), event)
        return out

    def _finish_request(self, event: Dict[str, Any]) -> Dict[str, Any]:
        record = self._model_record(event)
        self._error(record, event["attrs"])
        return record

    def _sse_event(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        attrs = event["attrs"]
        has_tokens = any(number(attrs.get(key)) is not None for key in _INT_KEYS)
        if not has_tokens and attrs.get("event.kind") != "response.completed":
            self.dropped += 1
            return []
        pending = self._requests.pop((event["source"], self._session(event)), None)
        if pending:
            merged = dict(pending[1]["attrs"])
            merged.update({k: v for k, v in attrs.items() if k in _INT_KEYS or k not in merged})
            event = dict(pending[1], attrs=merged, time=event["time"])
        return [self._finish_request(event)]

    def _api_error(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        record = self._model_record(event)
        record["function_name"] = f"{event['source']}.{cw.EVENT_API_ERROR}"
        self._error(record, event["attrs"])
        record.setdefault("error", REDACTED)
        record["status"] = "error"
        return [record]

    def _api_refusal(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        record = self._model_record(event)
        record["function_name"] = f"{event['source']}.{cw.EVENT_API_REFUSAL}"
        record["status"] = "refused"
        return [record]

    def _tool_key(self, event: Dict[str, Any]) -> Tuple[str, str, str]:
        attrs = event["attrs"]
        tool_id = _label(attrs.get(cw.TOOL_USE_ID))
        if not tool_id:
            tool_id = "name:" + (_label(attrs.get(cw.TOOL_NAME)) or "") + ":" + (self._group(event) or "")
        return (event["source"], self._session(event), tool_id)

    def _tool_record(self, decision: Optional[Dict[str, Any]], result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        event = result or decision
        assert event is not None
        attrs: Dict[str, Any] = {}
        if decision:
            attrs.update(decision["attrs"])
        if result:
            attrs.update(result["attrs"])
        tool = _label(attrs.get(cw.TOOL_NAME)) or "unknown"
        merged = dict(event, attrs=attrs)
        record = self._base(merged, "tool_call", f"{event['source']}.tool.{tool}",
                            number(attrs.get(cw.TOOL_DURATION_MS)) if result else None)
        inputs, outputs = record["inputs"], record["outputs"]
        inputs["tool_name"] = tool
        self._copy(attrs, inputs, _TOOL_INPUT_STRINGS)
        if decision:
            self._copy(decision["attrs"], inputs, (cw.TOOL_DECISION_DECISION,))
            source = _label(decision["attrs"].get(cw.TOOL_DECISION_SOURCE_ATTR))
            if source:
                inputs["decision_source"] = source
        parameters = self.redactor.text(attrs.get(cw.TOOL_PARAMETERS))
        if parameters:
            inputs["tool_parameters"] = parameters
        if self.redactor.keep_content:
            for key in (cw.TOOL_INPUT, "tool_output"):
                if attrs.get(key) is not None:
                    (inputs if key == cw.TOOL_INPUT else outputs)[key] = attrs[key]
        if result:
            success = boolean(result["attrs"].get(cw.TOOL_SUCCESS))
            if success is not None:
                outputs["success"] = success
            self._copy(result["attrs"], outputs, (cw.TOOL_ERROR_TYPE,),
                       (cw.TOOL_RESULT_SIZE_BYTES, "tool_input_size_bytes"))
            self._error(record, result["attrs"])
            if success is False:
                record["status"] = "error"
                record.setdefault("error", REDACTED)
        else:
            decided = (_label(decision["attrs"].get(cw.TOOL_DECISION_DECISION)) or "").lower() if decision else ""
            record["status"] = "rejected" if any(w in decided for w in _REJECT_WORDS) else "no_result"
        return record

    def _tool_decision(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        decided = (_label(event["attrs"].get(cw.TOOL_DECISION_DECISION)) or "").lower()
        if any(word in decided for word in _REJECT_WORDS):
            return [self._tool_record(event, None)]
        if len(self._decisions) >= MAX_PENDING:
            self._decisions.pop(next(iter(self._decisions)))
            self.dropped += 1
        self._decisions[self._tool_key(event)] = (self._clock(), event)
        return []

    def _tool_result(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        pending = self._decisions.pop(self._tool_key(event), None)
        return [self._tool_record(pending[1] if pending else None, event)]

    def _assistant_response(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        attrs = event["attrs"]
        record = self._base(event, cw.EVENT_ASSISTANT_RESPONSE,
                            f"{event['source']}.{cw.EVENT_ASSISTANT_RESPONSE}")
        self._copy(attrs, record["inputs"], (cw.API_MODEL, "request_id", "query_source"))
        self._copy(attrs, record["outputs"], (), (cw.RESPONSE_LENGTH,))
        if self.redactor.keep_content and attrs.get(cw.RESPONSE_TEXT) is not None:
            record["outputs"]["response"] = attrs[cw.RESPONSE_TEXT]
        return [record]

    def _metric(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.emit_metrics:
            self.dropped += 1
            return []
        record = self._base(event, "metric", str(event.get("metric")))
        self._copy(event["attrs"], record["inputs"], (cw.API_MODEL, "type", cw.TOOL_NAME, "decision"))
        record["outputs"] = {"value": number(event.get("value")), "unit": _label(event.get("unit"))}
        return [record]

    def _generic(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        attrs = event["attrs"]
        kind = _label(event["type"], 80) or "event"
        record = self._base(event, "event", f"{event['source']}.{kind}", number(attrs.get(cw.API_DURATION_MS)))
        self._copy(attrs, record["inputs"], _GENERIC_STRINGS)
        self._copy(attrs, record["outputs"], (), _GENERIC_NUMBERS)
        return [record]

    def _expire(self, everything: bool = False) -> List[Dict[str, Any]]:
        now = self._clock()
        out = []
        for key in [k for k, (at, _) in self._decisions.items() if everything or now - at > self._ttl]:
            out.append(self._tool_record(self._decisions.pop(key)[1], None))
        for pair in [k for k, (at, _) in self._requests.items() if everything or now - at > self._ttl]:
            out.append(self._finish_request(self._requests.pop(pair)[1]))
        return out
