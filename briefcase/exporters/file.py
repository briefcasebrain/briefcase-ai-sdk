"""JSON Lines file exporter — appends decision records to a .jsonl file."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Union

from briefcase.exporters.base import BaseExporter


class JSONLFileExporter(BaseExporter):
    """Append decision records to a file as JSON Lines (one object per line).

    Durable, append-only, and thread-safe — safe to share across the background
    export threads spawned by ``@capture``. Parent directories are created on
    demand.

    Example:
        import briefcase
        briefcase.observe("runs.jsonl")   # or JSONLFileExporter("runs.jsonl")
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._fh = None  # type: ignore[var-annotated]
        parent = self._path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

    def _ensure_open(self):
        if self._fh is None:
            self._fh = open(self._path, "a", encoding="utf-8")
        return self._fh

    async def export(self, decision: Any) -> bool:
        line = json.dumps(decision, default=str)
        with self._lock:
            fh = self._ensure_open()
            fh.write(line + "\n")
            fh.flush()
        return True

    async def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()

    async def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
