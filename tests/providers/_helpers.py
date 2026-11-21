"""Shared helpers for the provider auto-instrumentation tests.

No test here touches the network: each SDK's own client is driven through a
mock transport (openai, anthropic, google-genai) or a botocore Stubber.
"""

from __future__ import annotations

import inspect
import json
import sys
from typing import Any, Dict, Iterable, List, Tuple

PROMPT = "PROMPT-SECRET-7f3a"
COMPLETION = "COMPLETION-SECRET-9c1d"

SCHEMA_FIELDS = {
    "schema_version", "decision_id", "decision_type", "function_name",
    "inputs", "outputs", "started_at", "ended_at", "execution_time_ms",
}


def httpx_for(client: Any) -> Any:
    """The httpx flavour (``httpx`` or ``httpx2``) an SDK client is built
    on, found from the base classes of its transport client."""
    for cls in type(client._client).__mro__:
        root = cls.__module__.split(".")[0]
        if root in ("httpx", "httpx2"):
            return sys.modules[root]
    raise AssertionError("SDK client is not built on httpx")


def sse(events: Iterable[Tuple[Any, Dict[str, Any]]], done: bool = False) -> bytes:
    """Encode ``(event_name_or_None, data)`` pairs as a Server-Sent Events body."""
    out: List[str] = []
    for name, data in events:
        if name:
            out.append(f"event: {name}\n")
        out.append(f"data: {json.dumps(data)}\n\n")
    if done:
        out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def static(owner: Any, attr: str) -> Any:
    return inspect.getattr_static(owner, attr)


def assert_base_record(record: Dict[str, Any], decision_type: str) -> None:
    """The record keeps the @capture layout and carries the provider fields."""
    from briefcase.decorators import SCHEMA_VERSION

    assert SCHEMA_FIELDS <= set(record), set(record)
    assert record["schema_version"] == SCHEMA_VERSION
    assert record["decision_type"] == decision_type
    assert len(record["decision_id"]) == 36
    assert record["execution_time_ms"] >= 0
    assert record["started_at"] <= record["ended_at"]
    assert record["model_parameters"]["provider"]


def assert_no_content(record: Dict[str, Any]) -> None:
    blob = json.dumps(record, default=str)
    assert PROMPT not in blob
    assert COMPLETION not in blob
    assert record["inputs"] == {}
