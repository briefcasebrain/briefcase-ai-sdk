from abc import ABC, abstractmethod
from typing import Any, Mapping


def get_field(record: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a decision record that is either a mapping (what
    ``@capture`` and the framework handlers emit) or an object with
    attributes (``DecisionSnapshot`` and friends)."""
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


class BaseExporter(ABC):
    """Base class for all decision trace exporters.

    Exporters receive decision records, either plain dicts (from ``@capture``
    and the framework handlers) or DecisionSnapshot-style objects, and ship
    them to external observability/compliance systems. Use :func:`get_field`
    to read a field from either shape.
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
