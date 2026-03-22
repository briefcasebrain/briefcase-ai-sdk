"""
Central event emission helpers.

emit() dispatches a BriefcaseEvent to the configured event bus
according to BriefcaseConfig.

Helper factories:
  emit_low_confidence(decision, confidence, threshold)
  emit_drift_detected(decision, details)
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from briefcase.config import BriefcaseConfig
from briefcase.events.types import BriefcaseEvent


async def emit(event: BriefcaseEvent) -> None:
    """Dispatch *event* to the configured event bus."""
    config = BriefcaseConfig.get()

    if config.event_bus is not None:
        bus = config.event_bus
        if asyncio.iscoroutinefunction(getattr(bus, "emit", None)):
            await bus.emit(event)
        elif hasattr(bus, "publish"):
            publish = bus.publish
            if asyncio.iscoroutinefunction(publish):
                await publish(event)
            else:
                publish(event)


async def emit_low_confidence(
    decision: Any,
    confidence: float,
    threshold: float,
) -> None:
    """Emit a 'decision.low_confidence' event."""
    decision_id = (
        getattr(decision, "decision_id", None)
        or getattr(decision, "id", None)
        or ""
    )
    event = BriefcaseEvent(
        event_type="decision.low_confidence",
        decision_id=str(decision_id),
        payload={
            "confidence": confidence,
            "threshold": threshold,
        },
    )
    await emit(event)


async def emit_drift_detected(
    decision: Any,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a 'drift.detected' event."""
    decision_id = (
        getattr(decision, "decision_id", None)
        or getattr(decision, "id", None)
        or ""
    )
    event = BriefcaseEvent(
        event_type="drift.detected",
        decision_id=str(decision_id),
        payload=details or {},
    )
    await emit(event)
