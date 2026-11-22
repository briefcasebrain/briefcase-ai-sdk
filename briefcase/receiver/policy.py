"""Policy decisions posted by ``briefcase hook <tool> --enforce`` -> decision records.

``POST /v1/policy/decisions`` takes the JSON the hook writes to its local log
and returns a record with ``decision_type = "policy.tool_call"``.

The record is built from an allow-list. What the decision was *about* (the
command, the path, the URL) is content: by default only its keyed hash
(``target_hmac_sha256``, computed in the hook) and the rule that matched are
kept. The text is kept only when the hook was installed with
``--capture-content`` *and* the receiver runs with ``--keep-content``, the
same two switches every other record needs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from briefcase.receiver.records import SCHEMA_VERSION, Redactor

DECISION_TYPE = "policy.tool_call"
PATH = "/v1/policy/decisions"
_EFFECTS = ("allow", "ask", "deny")
_TOOLS = ("claude-code", "cursor", "codex")
_KINDS = ("shell", "read", "write", "mcp", "web", "error")
_SOURCES = ("preset", "built-in", "user", "project", "system", "setting")


def _label(value: Any, limit: int = 200) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > limit or "\n" in value:
        return None
    return value


def _choice(value: Any, choices: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in choices else None


def _hex(value: Any) -> Optional[str]:
    if isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value):
        return value
    return None


def record_from_decision(payload: Dict[str, Any], redactor: Redactor,
                         now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """One record, or ``None`` when ``payload`` is not a policy decision."""
    if not isinstance(payload, dict):
        return None
    tool = _choice(payload.get("tool"), _TOOLS)
    effect = _choice(payload.get("effect"), _EFFECTS)
    kind = _choice(payload.get("kind"), _KINDS)
    if tool is None or effect is None or kind is None:
        return None
    ended = now or datetime.now(timezone.utc)
    duration = payload.get("ms")
    duration = float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) \
        and 0 <= duration < 600000 else 0.0
    inputs: Dict[str, Any] = {"source": tool, "tool_kind": kind}
    for key, name in (("event", "hook_event_name"), ("tool_name", "tool_name"), ("session", "session_id"),
                      ("tool_use_id", "tool_use_id")):
        label = _label(payload.get(key))
        if label is not None:
            inputs[name] = label
    digest = _hex(payload.get("target_hmac_sha256"))
    if digest is not None:
        inputs["target_hmac_sha256"] = digest
    if redactor.keep_content:
        for key in ("target", "unit"):
            if isinstance(payload.get(key), str) and payload[key]:
                inputs[key] = payload[key][:4000]
    outputs: Dict[str, Any] = {"effect": effect, "enforced": effect != "allow"}
    for key, value in (
        ("would", _choice(payload.get("would"), _EFFECTS)),
        ("mode", _choice(payload.get("mode"), ("observe", "enforce"))),
        ("rule_id", _label(payload.get("rule"), 120)),
        ("rule_source", _choice(payload.get("source"), _SOURCES)),
        # The rule's reason is text from the policy file, not from the tool call.
        ("reason", _label(payload.get("reason"), 500)),
        ("ask_fallback", _choice(payload.get("ask_fallback"), ("allow", "deny"))),
    ):
        if value is not None:
            outputs[key] = value
    if isinstance(payload.get("unparseable"), int) and not isinstance(payload.get("unparseable"), bool):
        outputs["unparseable"] = payload["unparseable"]
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": str(uuid.uuid4()),
        "decision_type": DECISION_TYPE,
        "function_name": f"{tool}.{kind}",
        "inputs": inputs,
        "outputs": outputs,
        "started_at": (ended - timedelta(milliseconds=duration)).astimezone(timezone.utc).isoformat(),
        "ended_at": ended.astimezone(timezone.utc).isoformat(),
        "execution_time_ms": duration,
    }
    if isinstance(payload.get("error"), str) and payload["error"]:
        # A hook problem (bad policy file, crash). The message can quote a
        # path, so it goes through the same rule as every other error string.
        record["error"] = redactor.text(payload["error"], limit=1000) or "[redacted]"
        record["status"] = "error"
    return record
