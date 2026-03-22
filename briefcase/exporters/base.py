from abc import ABC, abstractmethod
from typing import Any


class BaseExporter(ABC):
    """Base class for all decision trace exporters.

    Exporters receive CapturedDecision or DecisionSnapshot objects
    and ship them to external observability/compliance systems.
    """

    @abstractmethod
    async def export(self, decision: Any) -> bool:
        """Export a single decision record. Returns True on success."""
        ...

    @abstractmethod
    async def flush(self) -> None:
        """Flush any buffered records."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
        ...
