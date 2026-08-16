"""Tests for briefcase.routing.opa OPARouter.

A stub httpx module is injected into sys.modules via monkeypatch, so the
tests run without httpx installed and never touch the network. OPARouter
imports httpx lazily in its constructor, which resolves to the stub.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Dict, Optional

import pytest

from briefcase.routing.base import RoutingDecision
from briefcase.routing.opa import OPARouter


def _run(coro):
    return asyncio.run(coro)


@dataclass
class FakeDecision:
    decision_id: str = "dec-opa-001"
    confidence: Optional[float] = 0.9
    context_version: str = "v1"
    model_parameters: Any = None
    tags: Dict[str, str] = field(default_factory=dict)


class FakeResponse:
    """Stand-in for an httpx.Response carrying an OPA result document."""

    def __init__(self, action: str = "auto", reason: Optional[str] = None,
                 error: Optional[Exception] = None):
        self.status_code = 200
        self._action = action
        self._reason = reason
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return {"result": {"action": self._action, "reason": self._reason}}


def _install_httpx_stub(monkeypatch, post):
    """Inject a stub httpx module whose AsyncClient.post delegates to post.

    Returns the list of recorded calls, each a dict with 'url' and 'json'.
    """
    stub = ModuleType("httpx")
    calls = []

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None):
            calls.append({"url": url, "json": json})
            return await post(url, json)

    stub.AsyncClient = FakeAsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", stub)
    return calls


def _make_router(**kwargs) -> OPARouter:
    return OPARouter(
        endpoint="http://opa:8181/v1/data/briefcase/routing",
        timeout_ms=kwargs.pop("timeout_ms", 50),
        cache_ttl_seconds=kwargs.pop("cache_ttl_seconds", 60),
        fallback_threshold=kwargs.pop("fallback_threshold", 0.85),
        **kwargs,
    )


def test_opa_sends_correct_input(monkeypatch):
    """OPARouter POSTs the expected input document structure to OPA."""
    async def post(url, json):
        return FakeResponse("auto")

    calls = _install_httpx_stub(monkeypatch, post)
    router = _make_router()
    _run(router.route(FakeDecision(confidence=0.9, context_version="v2")))

    assert len(calls) == 1
    input_doc = calls[0]["json"]["input"]
    assert set(input_doc) == {"confidence", "context_version", "model_name", "tags"}
    assert input_doc["confidence"] == pytest.approx(0.9)
    assert input_doc["context_version"] == "v2"


def test_opa_returns_human_review(monkeypatch):
    """OPA returning human_review yields action='human_review', source='opa'."""
    async def post(url, json):
        return FakeResponse("human_review")

    _install_httpx_stub(monkeypatch, post)
    router = _make_router()
    result = _run(router.route(FakeDecision()))

    assert result.action == "human_review"
    assert result.source == "opa"


def test_opa_returns_auto(monkeypatch):
    """OPA returning auto yields a RoutingDecision with action='auto'."""
    async def post(url, json):
        return FakeResponse("auto")

    _install_httpx_stub(monkeypatch, post)
    router = _make_router()
    result = _run(router.route(FakeDecision()))

    assert isinstance(result, RoutingDecision)
    assert result.action == "auto"
    assert result.source == "opa"


def test_opa_timeout_fallback(monkeypatch):
    """On timeout, OPARouter falls back to InternalRouter (source='internal')."""
    async def post(url, json):
        raise asyncio.TimeoutError("timed out")

    _install_httpx_stub(monkeypatch, post)
    router = _make_router()
    result = _run(router.route(FakeDecision(confidence=0.7)))

    assert result.source == "internal"
    assert result.action == "human_review"


def test_opa_error_fallback(monkeypatch):
    """On an HTTP error status, OPARouter falls back to InternalRouter."""
    async def post(url, json):
        return FakeResponse(error=Exception("500 Server Error"))

    _install_httpx_stub(monkeypatch, post)
    router = _make_router()
    result = _run(router.route(FakeDecision(confidence=0.3)))

    assert result.source == "internal"


def test_opa_cache_hit(monkeypatch):
    """The same input twice makes only one HTTP call (cache hit on second)."""
    async def post(url, json):
        return FakeResponse("auto")

    calls = _install_httpx_stub(monkeypatch, post)
    decision = FakeDecision(confidence=0.9)
    router = _make_router()
    first = _run(router.route(decision))
    second = _run(router.route(decision))

    assert len(calls) == 1
    assert first.action == second.action == "auto"


def test_opa_cache_miss_different_input(monkeypatch):
    """Two different inputs make two HTTP calls."""
    async def post(url, json):
        return FakeResponse("auto")

    calls = _install_httpx_stub(monkeypatch, post)
    router = _make_router()
    _run(router.route(FakeDecision(confidence=0.9)))
    _run(router.route(FakeDecision(confidence=0.5)))

    assert len(calls) == 2


def test_opa_cache_ttl_expiry(monkeypatch):
    """The same input after TTL expiry makes two HTTP calls."""
    async def post(url, json):
        return FakeResponse("auto")

    calls = _install_httpx_stub(monkeypatch, post)
    decision = FakeDecision(confidence=0.9)
    router = _make_router(cache_ttl_seconds=0.05)
    _run(router.route(decision))
    time.sleep(0.1)
    _run(router.route(decision))

    assert len(calls) == 2


def test_opa_eval_time_recorded(monkeypatch):
    """eval_time_ms is non-negative in the returned RoutingDecision."""
    async def post(url, json):
        return FakeResponse("auto")

    _install_httpx_stub(monkeypatch, post)
    router = _make_router()
    result = _run(router.route(FakeDecision()))

    assert result.eval_time_ms >= 0


def test_opa_emits_low_confidence_event(monkeypatch):
    """On human_review, emit_low_confidence is called once."""
    async def post(url, json):
        return FakeResponse("human_review")

    _install_httpx_stub(monkeypatch, post)

    emit_calls = []

    async def fake_emit_low_confidence(decision, confidence, threshold):
        emit_calls.append((decision, confidence, threshold))

    import briefcase.events.emitter as emitter
    monkeypatch.setattr(emitter, "emit_low_confidence", fake_emit_low_confidence)

    router = _make_router()
    result = _run(router.route(FakeDecision(confidence=0.3)))

    assert result.action == "human_review"
    assert len(emit_calls) == 1
    assert emit_calls[0][1] == pytest.approx(0.3)


def test_missing_httpx(monkeypatch):
    """Constructor raises ImportError with an install hint without httpx."""
    monkeypatch.setitem(sys.modules, "httpx", None)
    with pytest.raises(ImportError, match="pip install"):
        OPARouter(endpoint="http://opa:8181/v1/data/briefcase/routing")
