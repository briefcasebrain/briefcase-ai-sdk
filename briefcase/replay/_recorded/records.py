"""Load provider decision records and decide whether each one can be replayed.

A record written by provider auto-instrumentation
(:mod:`briefcase.integrations.providers`) carries the request only when it
was captured with ``capture_content=True``. This module turns a record into a
:class:`RecordedCall`: the rebuilt request (model, recorded parameters, the
request content) plus the recorded output to compare against, and a list of
precise reasons when it cannot be replayed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple, Union

# Mirrors briefcase.integrations.providers._base.DEFAULT_MAX_CONTENT_CHARS
# and _MAX_ITEMS: the bounds ``jsonable`` applies to captured content.
CAPTURE_MAX_CHARS = 8000
CAPTURE_MAX_ITEMS = 200

# decision_type -> (surface, provider)
SURFACES: Dict[str, Tuple[str, str]] = {
    "llm.openai.chat": ("openai.chat", "openai"),
    "llm.openai.responses": ("openai.responses", "openai"),
    "llm.openai.completions": ("openai.completions", "openai"),
    "llm.openai.embeddings": ("openai.embeddings", "openai"),
    "llm.anthropic.messages": ("anthropic.messages", "anthropic"),
    "llm.google_genai.generate_content": ("google_genai.generate_content", "google-genai"),
    "llm.bedrock.converse": ("bedrock.converse", "bedrock"),
    "llm.bedrock.invoke_model": ("bedrock.invoke_model", "bedrock"),
}

# Request keys the provider instrumentation records (when content is on).
REQUEST_KEYS: Dict[str, Tuple[str, ...]] = {
    "openai.chat": ("messages", "tools"),
    "openai.responses": ("instructions", "input", "tools"),
    "openai.completions": ("prompt",),
    "anthropic.messages": ("system", "messages", "tools"),
    "google_genai.generate_content": ("contents", "system_instruction"),
    "bedrock.converse": ("system", "messages"),
}
# The key without which there is no request at all.
REQUIRED_KEY: Dict[str, str] = {
    "openai.chat": "messages",
    "openai.responses": "input",
    "openai.completions": "prompt",
    "anthropic.messages": "messages",
    "google_genai.generate_content": "contents",
    "bedrock.converse": "messages",
}

# Request settings the instrumentation does NOT record. If the original call
# set them, the replay sends the API default instead; they can be supplied
# with --param.
UNRECORDED_SETTINGS: Dict[str, Tuple[str, ...]] = {
    "openai.chat": ("response_format", "tool_choice", "stop", "parallel_tool_calls",
                    "logit_bias", "prediction", "reasoning_effort (non-string)"),
    "openai.responses": ("text (structured output format)", "tool_choice", "reasoning",
                         "previous_response_id", "conversation", "include"),
    "openai.completions": ("stop", "suffix", "logit_bias", "best_of"),
    "anthropic.messages": ("tool_choice", "stop_sequences", "thinking",
                           "output_config (other than effort)"),
    "google_genai.generate_content": ("config.tools", "config.tool_config",
                                      "config.response_mime_type", "config.response_schema",
                                      "config.safety_settings", "config.stop_sequences",
                                      "config.thinking_config"),
    "bedrock.converse": ("toolConfig", "inferenceConfig.stopSequences",
                         "additionalModelRequestFields", "guardrailConfig"),
}

# Keys in model_parameters that are not request parameters.
_META_PARAMS = ("provider", "model_name", "requested_model")

_PLACEHOLDER = re.compile(r"^<(\d+ bytes|[A-Za-z_][\w.]*)>$")
_REDACTION_FAILED = "<redaction-failed>"


@dataclass
class RecordedCall:
    """One recorded provider call, as replay sees it."""

    index: int
    record: Dict[str, Any]
    decision_id: Optional[str] = None
    decision_type: Optional[str] = None
    surface: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    served_model: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    request: Dict[str, Any] = field(default_factory=dict)
    output: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None
    latency_ms: Optional[float] = None
    finish_reason: Optional[str] = None
    cost_usd: Optional[float] = None
    stream: bool = False
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def replayable(self) -> bool:
        return not self.reasons

    def summary(self) -> Dict[str, Any]:
        """Content-free description (used by ``replay explain --json``)."""
        return {
            "index": self.index,
            "decision_id": self.decision_id,
            "decision_type": self.decision_type,
            "surface": self.surface,
            "model": self.model,
            "replayable": self.replayable,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


# -- loading ----------------------------------------------------------------

def iter_jsonl(path: Union[str, Path]) -> Iterator[Dict[str, Any]]:
    """Yield the JSON objects of a JSONL file. A line that is not a JSON
    object is yielded as ``{"__unparsable__": line_number}`` so it is
    reported instead of silently dropped."""
    with open(path, "r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                yield {"__unparsable__": number}
                continue
            yield obj if isinstance(obj, dict) else {"__unparsable__": number}


def load_records(source: Union[str, Path, Iterable[Dict[str, Any]]]) -> List[RecordedCall]:
    """Analyse every record in a JSONL file (path) or an iterable of dicts."""
    if isinstance(source, (str, Path)):
        records: Iterable[Dict[str, Any]] = iter_jsonl(source)
    else:
        records = source
    return [analyze(record, index) for index, record in enumerate(records)]


# -- analysis ---------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def record_cost(record: Dict[str, Any]) -> Optional[float]:
    """Per-call cost in USD when the record carries one (``cost_usd``, or a
    ``cost`` number / ``{"total_usd"|"usd"}`` mapping). Replay ships no price
    table of its own."""
    value = _num(record.get("cost_usd"))
    if value is not None:
        return value
    cost = record.get("cost")
    if isinstance(cost, dict):
        for key in ("total_usd", "usd", "total"):
            value = _num(cost.get(key))
            if value is not None:
                return value
        return None
    return _num(cost)


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _walk_lists(value: Any) -> Iterator[list]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_lists(item)
    elif isinstance(value, list):
        yield value
        for item in value:
            yield from _walk_lists(item)


def content_problems(value: Any) -> List[str]:
    """Reasons the captured request content is not a faithful copy of what
    was sent: placeholders for binary / non-JSON values, failed redaction,
    and strings or lists cut at the capture bounds."""
    problems: List[str] = []
    placeholders = sorted({
        s for s in _walk_strings(value) if s == _REDACTION_FAILED or _PLACEHOLDER.match(s)
    })
    if _REDACTION_FAILED in placeholders:
        placeholders.remove(_REDACTION_FAILED)
        problems.append(
            "the redact hook failed on part of the request, so that text was replaced by "
            "'<redaction-failed>' when it was captured"
        )
    if placeholders:
        shown = ", ".join(placeholders[:3])
        problems.append(
            "part of the request was not JSON (an image, file, audio or SDK object) and was "
            f"captured only as a placeholder ({shown}); the original bytes are not in the record"
        )
    if any(len(s) == CAPTURE_MAX_CHARS + 3 and s.endswith("...") for s in _walk_strings(value)):
        problems.append(
            f"a request string was cut at the capture limit ({CAPTURE_MAX_CHARS} characters, "
            "max_content_chars); raise max_content_chars when capturing to replay long prompts"
        )
    if any(len(items) == CAPTURE_MAX_ITEMS for items in _walk_lists(value)):
        problems.append(
            f"a request list holds exactly {CAPTURE_MAX_ITEMS} items, the capture bound, so "
            "later items (e.g. older messages) may have been dropped"
        )
    return problems


def normalize_tool_calls(calls: Any) -> List[Dict[str, Any]]:
    """Tool calls as ``[{"name", "arguments", "raw"}]`` from any of the
    recorded / SDK shapes: OpenAI chat (``function.name`` / ``arguments``
    JSON string), Responses ``function_call`` items, Anthropic ``tool_use``
    (``name`` / ``input``), Gemini ``function_call`` (``name`` / ``args``)
    and Bedrock ``toolUse`` (``name`` / ``input``). ``arguments`` is parsed
    JSON when the raw value is a JSON string; ``raw`` keeps the original."""
    out: List[Dict[str, Any]] = []
    for call in calls if isinstance(calls, (list, tuple)) else ():
        if not isinstance(call, dict):
            continue
        source: Dict[str, Any] = _as_dict(call.get("function")) or call
        for nested in ("toolUse", "function_call", "functionCall"):
            if isinstance(call.get(nested), dict):
                source = call[nested]
        name = source.get("name")
        raw = None
        for key in ("arguments", "input", "args"):
            if key in source:
                raw = source[key]
                break
        arguments = raw
        if isinstance(raw, str):
            try:
                arguments = json.loads(raw)
            except ValueError:
                arguments = raw
        out.append({"name": name, "arguments": arguments, "raw": raw})
    return out


def _dangling_tool_calls(surface: str, request: Dict[str, Any]) -> List[str]:
    """Tool calls in the conversation that have no tool result after them.
    The API would reject such a request, and replay never runs tools to
    produce the missing results."""
    messages = request.get("messages")
    if not isinstance(messages, list):
        return []
    pending: Dict[str, str] = {}
    if surface == "openai.chat":
        for message in messages:
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    pending[call["id"]] = str((call.get("function") or {}).get("name"))
            if message.get("role") == "tool":
                pending.pop(str(message.get("tool_call_id")), None)
    elif surface in ("anthropic.messages", "bedrock.converse"):
        use_key, result_key = (
            ("tool_use", "tool_result") if surface == "anthropic.messages" else ("toolUse", "toolResult")
        )
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else ():
                if not isinstance(block, dict):
                    continue
                if surface == "anthropic.messages":
                    if block.get("type") == use_key and block.get("id"):
                        pending[block["id"]] = str(block.get("name"))
                    elif block.get("type") == result_key:
                        pending.pop(str(block.get("tool_use_id")), None)
                else:
                    use = block.get(use_key)
                    if isinstance(use, dict) and use.get("toolUseId"):
                        pending[use["toolUseId"]] = str(use.get("name"))
                    result = block.get(result_key)
                    if isinstance(result, dict):
                        pending.pop(str(result.get("toolUseId")), None)
    return sorted(set(pending.values()))


def _recorded_output(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    outputs = record.get("outputs")
    if not isinstance(outputs, dict):
        return None
    text = outputs.get("content")
    tool_calls = normalize_tool_calls(outputs.get("tool_calls"))
    if not isinstance(text, str):
        text = None
    if not text and not tool_calls:
        return None
    out: Dict[str, Any] = {"text": text, "tool_calls": tool_calls}
    if isinstance(outputs.get("refusal"), str):
        out["refusal"] = outputs["refusal"]
    return out


def analyze(record: Dict[str, Any], index: int = 0) -> RecordedCall:
    """Turn one record into a :class:`RecordedCall`, filling ``reasons``
    when it cannot be replayed and ``warnings`` for caveats that do not
    block a replay."""
    call = RecordedCall(index=index, record=record)
    if "__unparsable__" in record:
        call.reasons.append(f"line {record['__unparsable__']} is not a JSON object")
        return call
    call.decision_id = record.get("decision_id")
    call.decision_type = record.get("decision_type")
    surface = SURFACES.get(str(call.decision_type))
    if surface is None:
        call.reasons.append(
            f"decision_type {call.decision_type!r} is not a provider call recorded by "
            "briefcase.auto (openai, anthropic, google-genai or bedrock), so there is no "
            "request to rebuild"
        )
        return call
    call.surface, call.provider = surface
    reasons, warnings = call.reasons, call.warnings

    parameters = _as_dict(record.get("model_parameters"))
    call.served_model = parameters.get("model_name")
    call.model = parameters.get("requested_model") or parameters.get("model_name")
    call.params = {k: v for k, v in parameters.items() if k not in _META_PARAMS}
    usage = record.get("token_usage")
    call.usage = usage if isinstance(usage, dict) else None
    call.latency_ms = _num(record.get("execution_time_ms"))
    call.finish_reason = record.get("finish_reason")
    call.cost_usd = record_cost(record)
    call.stream = bool(record.get("stream"))

    if call.surface == "openai.embeddings":
        reasons.append("embeddings calls record no output vectors, so there is nothing to compare")
        return call
    if call.surface == "bedrock.invoke_model":
        reasons.append(
            "InvokeModel responses are provider-specific bytes that capture never reads, so no "
            "output was recorded to compare against; use Converse to get replayable records"
        )
        return call
    if not call.model:
        reasons.append("the record has no model name (model_parameters.model_name)")

    status = record.get("status")
    if status not in (None, "ok"):
        detail = record.get("error_type") or status
        reasons.append(
            f"the original call did not complete (status={status}, {detail}), so there is no "
            "recorded output to compare against"
        )

    inputs = record.get("inputs")
    inputs = inputs if isinstance(inputs, dict) else {}
    required = REQUIRED_KEY[call.surface]
    if "sha256" in inputs:
        reasons.append(
            "content was captured as a hash (capture_content='hash'): only a digest of the "
            "request is stored; re-capture with capture_content=True to replay"
        )
    elif required not in inputs:
        reasons.append(
            "content capture was off when this call was recorded (the default), so the request "
            f"({required}) is not in the record; re-capture with "
            "briefcase.auto(capture_content=True)"
        )
    else:
        call.request = {k: inputs[k] for k in REQUEST_KEYS[call.surface] if k in inputs}
        reasons.extend(content_problems(call.request))
        dangling = _dangling_tool_calls(call.surface, call.request)
        if dangling:
            reasons.append(
                "the conversation ends in tool calls with no tool results ("
                + ", ".join(dangling)
                + "); replay never runs tools, so the request cannot be completed"
            )

    outputs = _as_dict(record.get("outputs"))
    if outputs.get("stream_completed") is False:
        reasons.append(
            "the stream was not read to the end when it was recorded, so the recorded output is "
            "partial"
        )
    call.output = _recorded_output(record)
    if not any("sha256" in s or "content capture was off" in s for s in reasons):
        if "sha256" in outputs:
            reasons.append("the output was captured as a hash, so there is no text to compare")
        elif call.output is None and status in (None, "ok"):
            if call.stream:
                reasons.append(
                    "this was a streamed call and its recorded output is empty; streamed tool "
                    "calls are not captured (only text deltas are), so there is nothing to compare"
                )
            elif call.surface in ("openai.responses", "google_genai.generate_content",
                                  "bedrock.converse"):
                reasons.append(
                    "the recorded output is empty; for this API capture keeps only the text, so "
                    "a tool/function-call answer was not recorded and cannot be compared"
                )
            else:
                reasons.append("no output was recorded for this call")
        elif call.output is not None and call.output.get("text") is not None:
            if any(len(s) == CAPTURE_MAX_CHARS + 3 and s.endswith("...")
                   for s in _walk_strings(call.output)):
                warnings.append(
                    f"the recorded output was cut at {CAPTURE_MAX_CHARS} characters, so an "
                    "exact comparison will report a change; the diff shows where"
                )

    if call.surface == "anthropic.messages" and "max_tokens" not in call.params and call.request:
        reasons.append(
            "max_tokens is required by the Anthropic Messages API and was not recorded"
        )
    if call.stream and call.usage is None:
        warnings.append("streamed without usage reporting: token deltas will be unavailable")
    if isinstance(call.params.get("n"), int) and call.params["n"] > 1:
        warnings.append("n > 1 was requested; only the first choice was recorded and is compared")
    unrecorded = UNRECORDED_SETTINGS.get(call.surface or "")
    if unrecorded and not reasons:
        warnings.append(
            "capture does not record " + ", ".join(unrecorded)
            + "; if the original call set any of them, pass it with --param"
        )
    return call


def fingerprint(surface: Optional[str], model: Optional[str], params: Dict[str, Any],
                request: Dict[str, Any]) -> str:
    """Stable SHA-256 of a request (used as the cassette key)."""
    blob = json.dumps(
        {"surface": surface, "model": model, "params": params, "request": request},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = [
    "CAPTURE_MAX_CHARS",
    "REQUEST_KEYS",
    "RecordedCall",
    "SURFACES",
    "UNRECORDED_SETTINGS",
    "analyze",
    "content_problems",
    "fingerprint",
    "iter_jsonl",
    "load_records",
    "normalize_tool_calls",
    "record_cost",
]
