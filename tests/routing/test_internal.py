"""Tests for briefcase.routing.internal InternalRouter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from briefcase.routing.base import RoutingDecision
from briefcase.routing.internal import InternalRouter


def _run(coro):
    return asyncio.run(coro)


@dataclass
class FakeDecision:
    confidence: Optional[float] = None


def test_internal_auto_high_confidence():
    """confidence=0.95, threshold=0.85 routes to 'auto'."""
    router = InternalRouter(confidence_threshold=0.85)
    result = _run(router.route(FakeDecision(confidence=0.95)))
    assert result.action == "auto"


def test_internal_review_low_confidence():
    """confidence=0.70, threshold=0.85 routes to 'human_review'."""
    router = InternalRouter(confidence_threshold=0.85)
    result = _run(router.route(FakeDecision(confidence=0.70)))
    assert result.action == "human_review"


def test_internal_review_none_confidence():
    """confidence=None routes to 'human_review' (safe default)."""
    router = InternalRouter(confidence_threshold=0.85)
    result = _run(router.route(FakeDecision(confidence=None)))
    assert result.action == "human_review"


def test_internal_review_at_threshold():
    """confidence equal to the threshold routes to 'auto' (>= boundary)."""
    router = InternalRouter(confidence_threshold=0.85)
    result = _run(router.route(FakeDecision(confidence=0.85)))
    assert result.action == "auto"


def test_internal_custom_threshold():
    """threshold=0.5, confidence=0.6 routes to 'auto'."""
    router = InternalRouter(confidence_threshold=0.5)
    result = _run(router.route(FakeDecision(confidence=0.6)))
    assert result.action == "auto"


def test_internal_source_is_internal():
    """source is 'internal'."""
    router = InternalRouter()
    result = _run(router.route(FakeDecision(confidence=0.9)))
    assert result.source == "internal"


def test_internal_eval_time_recorded():
    """eval_time_ms is non-negative."""
    router = InternalRouter()
    result = _run(router.route(FakeDecision(confidence=0.9)))
    assert result.eval_time_ms >= 0


def test_internal_returns_routing_decision():
    """route() returns a RoutingDecision instance."""
    router = InternalRouter()
    result = _run(router.route(FakeDecision(confidence=0.5)))
    assert isinstance(result, RoutingDecision)


def test_internal_confidence_below_zero():
    """Negative confidence routes to 'human_review'."""
    router = InternalRouter(confidence_threshold=0.85)
    result = _run(router.route(FakeDecision(confidence=-0.1)))
    assert result.action == "human_review"


def test_internal_outputs_list_confidence():
    """Confidence is read from an outputs list when .confidence is absent."""
    class FakeOutput:
        confidence = 0.9

    class DecisionWithOutputs:
        confidence = None
        outputs = [FakeOutput()]

    router = InternalRouter(confidence_threshold=0.85)
    result = _run(router.route(DecisionWithOutputs()))
    assert result.action == "auto"


def test_internal_outputs_dict_confidence():
    """Confidence is read from an outputs dict."""
    class DecisionWithDictOutputs:
        confidence = None
        outputs = {"confidence": 0.5}

    router = InternalRouter(confidence_threshold=0.85)
    result = _run(router.route(DecisionWithDictOutputs()))
    assert result.action == "human_review"
