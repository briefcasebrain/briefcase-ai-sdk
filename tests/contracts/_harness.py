"""SDK clients whose HTTP layer answers from spec-generated bodies and
keeps every request it was sent, so a test can validate the wire request
against the spec and the record against the response.

Everything above the socket (serialisation, response parsing, streaming)
is the installed SDK's own code; nothing reaches a provider.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from tests.providers._helpers import httpx_for, sse

Responder = Callable[[str, Dict[str, Any]], Tuple[Any, Dict[str, str], bool]]


class Wire:
    """Requests seen by a mock transport: ``(path, headers, json body)``."""

    def __init__(self) -> None:
        self.requests: List[Tuple[str, Dict[str, str], Any]] = []

    @property
    def last(self) -> Tuple[str, Dict[str, str], Any]:
        return self.requests[-1]


def _transport(httpx: Any, wire: Wire, respond: Responder) -> Any:
    def handler(request: Any) -> Any:
        body = json.loads(request.content) if request.content else None
        wire.requests.append((request.url.path + ("?" + request.url.query.decode()
                                                  if request.url.query else ""),
                              dict(request.headers), body))
        payload, headers, stream = respond(request.url.path, body or {})
        if stream:
            return httpx.Response(200, content=payload,
                                  headers=dict({"content-type": "text/event-stream"}, **headers))
        return httpx.Response(200, json=payload, headers=headers)

    return httpx.MockTransport(handler)


def openai_client(respond: Responder, wire: Optional[Wire] = None) -> Tuple[Any, Wire]:
    import openai

    wire = wire or Wire()
    httpx = httpx_for(openai.OpenAI(api_key="k"))
    client = openai.OpenAI(api_key="k", max_retries=0,
                           http_client=httpx.Client(transport=_transport(httpx, wire, respond)))
    return client, wire


def anthropic_client(respond: Responder, wire: Optional[Wire] = None) -> Tuple[Any, Wire]:
    import anthropic

    wire = wire or Wire()
    httpx = httpx_for(anthropic.Anthropic(api_key="k"))
    client = anthropic.Anthropic(api_key="k", max_retries=0,
                                 http_client=httpx.Client(transport=_transport(httpx, wire, respond)))
    return client, wire


def google_client(respond: Responder, wire: Optional[Wire] = None) -> Tuple[Any, Wire]:
    import httpx
    from google import genai
    from google.genai import types

    wire = wire or Wire()
    client = genai.Client(api_key="k", http_options=types.HttpOptions(
        httpx_client=httpx.Client(transport=_transport(httpx, wire, respond)),
        retry_options=types.HttpRetryOptions(attempts=1),
    ))
    return client, wire


def json_body(body: Any, request_id: str = "req-contract") -> Responder:
    def respond(path: str, request: Dict[str, Any]) -> Tuple[Any, Dict[str, str], bool]:
        return body, {"x-request-id": request_id, "request-id": request_id}, False
    return respond


def sse_body(events: List[Tuple[Optional[str], Any]], done: bool = False,
             request_id: str = "req-contract") -> Responder:
    def respond(path: str, request: Dict[str, Any]) -> Tuple[Any, Dict[str, str], bool]:
        return sse(events, done=done), {"x-request-id": request_id, "request-id": request_id}, True
    return respond


def bedrock_wire_client(body: bytes, headers: Optional[Dict[str, str]] = None) -> Tuple[Any, Wire]:
    """A real ``bedrock-runtime`` client answering ``body`` and keeping each
    request's operation path and JSON body."""
    from tests.providers._helpers import bedrock_client

    client = bedrock_client(body, headers)
    wire = Wire()
    session = client._endpoint.http_session
    send = session.send

    def recording(request: Any) -> Any:
        raw = request.body
        if hasattr(raw, "read"):
            raw = raw.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw
        wire.requests.append((request.url, dict(request.headers), parsed))
        return send(request)

    session.send = recording
    return client, wire


def jsonify(value: Any) -> Any:
    """A botocore-shaped example as the JSON Bedrock sends (bytes as
    base64, timestamps as epoch seconds)."""
    import base64
    import datetime

    if isinstance(value, dict):
        return {k: jsonify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonify(v) for v in value]
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    return value
