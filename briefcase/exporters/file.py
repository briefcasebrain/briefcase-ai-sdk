"""JSON Lines file exporter — appends decision records to a .jsonl file."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional, TextIO, Union

from briefcase.exporters.base import BaseExporter


def _opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


class JSONLFileExporter(BaseExporter):
    """Append decision records to a file as JSON Lines (one object per line).

    Durable, append-only, and thread-safe — safe to share across the background
    export threads spawned by ``@capture``. Parent directories are created on
    demand with mode 0700, and the file is opened owner-only (0600); records can
    carry decision content, so both are private regardless of umask.

    Example:
        import briefcase
        briefcase.observe("runs.jsonl")   # or JSONLFileExporter("runs.jsonl")
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._fh: Optional[TextIO] = None
        parent = self._path.parent
        if parent and not parent.exists():
            # Create each missing level with mode 0700 at creation time
            # (masked by umask, so never wider), top-down; no probe loop and
            # no post-hoc chmod window.
            missing = []
            probe = parent
            while not probe.exists():
                missing.append(probe)
                if probe.parent == probe:
                    break
                probe = probe.parent
            for directory in reversed(missing):
                try:
                    os.mkdir(directory, 0o700)
                except FileExistsError:
                    pass

    def _ensure_open(self):
        if self._fh is None:
            self._fh = open(self._path, "a", encoding="utf-8", opener=_opener)
            # fchmod on the open descriptor: tightens a pre-existing file
            # without re-resolving the path (no symlink-following chmod).
            # Windows has no fchmod; modes are advisory there anyway.
            if hasattr(os, "fchmod"):
                os.fchmod(self._fh.fileno(), 0o600)
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
