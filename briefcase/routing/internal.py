"""Threshold-based confidence routing."""

from __future__ import annotations

import time
from typing import Any, Optional

from briefcase.routing.base import BaseRouter, RoutingDecision


class InternalRouter(BaseRouter):
    """Route decisions based on a confidence threshold.

    If confidence >= threshold: action="auto".
    If confidence < threshold, or confidence is None: action="human_review".

    Args:
        confidence_threshold: Minimum confidence for automatic routing
            (default 0.85).
    """

    def __init__(self, confidence_threshold: float = 0.85) -> None:
        self._threshold = confidence_threshold

    @staticmethod
    def _get_confidence(decision_context: Any) -> Optional[float]:
        """Extract confidence from a decision context object.

        Reads ``decision_context.confidence`` first, then falls back to a
        nested ``outputs`` list or dict.
        """
        conf = getattr(decision_context, "confidence", None)
        if conf is not None:
            return float(conf)
        outputs = getattr(decision_context, "outputs", None)
        if isinstance(outputs, list):
            for out in outputs:
                c = getattr(out, "confidence", None)
                if c is not None:
                    return float(c)
        elif isinstance(outputs, dict):
            c = outputs.get("confidence")
            if c is not None:
                return float(c)
        return None

    async def route(self, decision_context: Any) -> RoutingDecision:
        """Return a RoutingDecision for the decision context."""
        start = time.monotonic()

        confidence = self._get_confidence(decision_context)

        if confidence is not None and confidence >= self._threshold:
            action = "auto"
        else:
            action = "human_review"

        eval_time_ms = (time.monotonic() - start) * 1000.0

        return RoutingDecision(
            action=action,
            source="internal",
            eval_time_ms=eval_time_ms,
            reason=f"confidence={confidence}, threshold={self._threshold}",
        )
