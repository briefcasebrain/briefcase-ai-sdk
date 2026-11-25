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


# -- botocore over canned HTTP responses -------------------------------------

def encode_event(event_type: str, payload: bytes, message_type: str = "event") -> bytes:
    """One ``application/vnd.amazon.eventstream`` frame, as Bedrock sends
    them (prelude, CRC, headers, payload, CRC); botocore's own
    ``EventStream`` decodes it in the tests."""
    import binascii
    import struct

    def header(name: str, value: str) -> bytes:
        n, v = name.encode(), value.encode()
        return bytes([len(n)]) + n + b"\x07" + struct.pack("!H", len(v)) + v

    headers = (header(":event-type", event_type) + header(":content-type", "application/json")
               + header(":message-type", message_type))
    prelude = struct.pack("!II", 12 + len(headers) + len(payload) + 4, len(headers))
    message = prelude + struct.pack("!I", binascii.crc32(prelude) & 0xFFFFFFFF) + headers + payload
    return message + struct.pack("!I", binascii.crc32(message) & 0xFFFFFFFF)


def invoke_chunk(obj: Any) -> bytes:
    """An ``InvokeModelWithResponseStream`` ``chunk`` event carrying ``obj``."""
    import base64

    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return encode_event("chunk", json.dumps({"bytes": base64.b64encode(raw).decode()}).encode())


def bedrock_client(body: bytes, headers: Any = None, status: int = 200) -> Any:
    """A real ``bedrock-runtime`` client whose HTTP session returns ``body``.
    Everything above the socket (serialisation, response parsing, event
    stream decoding, ``StreamingBody``) is botocore's own code."""
    import io

    import boto3
    from botocore.awsrequest import AWSResponse

    class Raw(io.BytesIO):
        def stream(self, amt: int = 1024, decode_content: Any = None) -> Any:
            while True:
                data = self.read(amt)
                if not data:
                    return
                yield data

        def release_conn(self) -> None:
            pass

    client = boto3.client("bedrock-runtime", region_name="us-east-1",
                          aws_access_key_id="test", aws_secret_access_key="test")
    all_headers = {"x-amzn-requestid": "req-1"}
    all_headers.update(headers or {})
    client._endpoint.http_session.send = lambda request: AWSResponse(
        request.url, status, all_headers, Raw(body))
    return client


def drain_gc() -> None:
    """Collect garbage and export the records of streams it finalized."""
    import gc

    from briefcase.integrations.providers._base import flush_abandoned

    gc.collect()
    flush_abandoned()
