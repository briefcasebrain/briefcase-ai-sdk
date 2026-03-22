from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict
import uuid


@dataclass
class BriefcaseEvent:
    event_type: str  # e.g., "decision.low_confidence"
    decision_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload: Dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))
