"""In-memory exporter — collects decision records in a list."""

from __future__ import annotations

from typing import Any, List

from briefcase.exporters.base import BaseExporter


class MemoryExporter(BaseExporter):
    """Collect decision records in memory for inspection.

    Ideal for tests, notebooks, and Streamlit apps where you want to read back
    the captured decisions. Records are exposed on the ``records`` attribute.

    Example:
        import briefcase
        mem = briefcase.observe("memory")

        @briefcase.capture(async_capture=False)
        def classify(text): return text.upper()

        classify("hi")
        assert mem.records[0]["function_name"] == "classify"
    """

    def __init__(self) -> None:
        self.records: List[Any] = []

    async def export(self, decision: Any) -> bool:
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
