from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class RoutingDecision:
    action: str  # "auto" or "human_review"
    source: str  # "internal", "opa", etc.
    eval_time_ms: float
    reason: Optional[str] = None


class BaseRouter(ABC):
    @abstractmethod
    async def route(self, decision_context: Any) -> RoutingDecision: ...
