"""
Central event emission helpers.

emit() dispatches a BriefcaseEvent to the event bus and/or webhook
configured on BriefcaseConfig (``setup(event_bus=...)``,
``setup(webhook_url=..., webhook_secret=..., events=[...])``).

Helper factories:
  emit_low_confidence(decision, confidence, threshold)
  emit_drift_detected(decision, details)
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

from briefcase._logging import get_logger
from briefcase.config import BriefcaseConfig
from briefcase.events.types import BriefcaseEvent
from briefcase.exporters.base import get_field


logger = get_logger(__name__)

# The WebhookEmitter built from the config's webhook settings, cached with
# the settings it was built from so a later setup() call takes effect.
_webhook_cache: Tuple[Optional[tuple], Any] = (None, None)


def _configured_webhook(config: BriefcaseConfig) -> Any:
    """Return a WebhookEmitter for ``config.webhook_url``, or None."""
    global _webhook_cache
    if not config.webhook_url:
        return None
    key = (
        config.webhook_url,
        config.webhook_secret,
        tuple(config.subscribed_events or ()),
    )
    cached_key, cached = _webhook_cache
    if cached_key == key:
        return cached
    from briefcase.events.webhook import WebhookEmitter

    try:
        emitter: Any = WebhookEmitter(
            url=config.webhook_url,
            secret=config.webhook_secret or "",
            subscribed_events=list(config.subscribed_events or []),
        )
    except ValueError as exc:
        logger.warning("Configured webhook_url is not usable: %s", exc)
        emitter = None
    _webhook_cache = (key, emitter)
    return emitter


async def emit(event: BriefcaseEvent) -> None:
    """Dispatch *event* to the configured event bus and webhook."""
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

    webhook = _configured_webhook(config)
    if webhook is not None:
        # WebhookEmitter.emit logs its own failures and never raises.
        await webhook.emit(event)


async def emit_low_confidence(
    decision: Any,
    confidence: float,
    threshold: float,
) -> None:
    """Emit a 'decision.low_confidence' event."""
    decision_id = (
        get_field(decision, "decision_id")
        or get_field(decision, "id")
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
        get_field(decision, "decision_id")
        or get_field(decision, "id")
        or ""
    )
    event = BriefcaseEvent(
        event_type="drift.detected",
        decision_id=str(decision_id),
        payload=details or {},
    )
    await emit(event)
