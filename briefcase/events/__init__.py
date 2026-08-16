"""
Briefcase events  typed event records emitted by the SDK.
"""

from briefcase.events.types import BriefcaseEvent
from briefcase.events.emitter import emit, emit_low_confidence, emit_drift_detected
from briefcase.events.kafka import KafkaPublisher
from briefcase.events.webhook import WebhookEmitter

__all__ = [
    "BriefcaseEvent",
    "KafkaPublisher",
    "WebhookEmitter",
    "emit",
    "emit_low_confidence",
    "emit_drift_detected",
]
