"""Threshold-based confidence routing."""

from __future__ import annotations

import time
from typing import Any, Optional

from briefcase.exporters.base import get_field
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
        """Extract confidence from a decision context, which may be a dict
        (a ``@capture`` record) or an object.

        Reads the ``confidence`` field first, then falls back to a nested
        ``outputs`` list or dict.
        """
        conf = get_field(decision_context, "confidence")
        if conf is not None:
            return float(conf)
        outputs = get_field(decision_context, "outputs")
        if isinstance(outputs, list):
            for out in outputs:
                c = get_field(out, "confidence")
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
