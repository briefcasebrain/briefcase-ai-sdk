"""In-memory exporter — collects decision records in a list."""

from __future__ import annotations

from collections import deque
from typing import Any, Optional

from briefcase.exporters.base import BaseExporter


class MemoryExporter(BaseExporter):
    """Collect decision records in memory for inspection.

    Ideal for tests, notebooks, and Streamlit apps where you want to read back
    the captured decisions. Records are exposed on the ``records`` attribute.

    Args:
        max_records: Keep only the most recent ``max_records`` records,
            discarding the oldest. The default, ``None``, keeps everything,
            so in a long-running process either set a bound or call
            :meth:`clear` periodically. ``dropped`` counts discarded records.

    Example:
        import briefcase
        mem = briefcase.observe("memory")

        @briefcase.capture(async_capture=False)
        def classify(text): return text.upper()

        classify("hi")
        assert mem.records[0]["function_name"] == "classify"
    """

    def __init__(self, max_records: Optional[int] = None) -> None:
        if max_records is not None and max_records < 1:
            raise ValueError("max_records must be >= 1 or None")
        self.max_records = max_records
        self.dropped = 0
        # A plain list when unbounded (the historical type); a bounded deque
        # otherwise, so eviction of the oldest record is O(1).
        self.records: Any = [] if max_records is None else deque(maxlen=max_records)

    async def export(self, decision: Any) -> bool:
        if self.max_records is not None and len(self.records) >= self.max_records:
            self.dropped += 1
        self.records.append(decision)
        return True

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        # Keep records so callers can still inspect them after close().
        pass

    def clear(self) -> None:
        """Drop all collected records."""
        self.records.clear()
