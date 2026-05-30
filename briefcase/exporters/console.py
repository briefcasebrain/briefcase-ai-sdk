"""Console exporter — writes captured decision records to a stream."""

from __future__ import annotations

import json
import sys
from typing import Any, Optional, TextIO

from briefcase.exporters.base import BaseExporter


class ConsoleExporter(BaseExporter):
    """Write each decision record as a line of JSON to a stream.

    The simplest way to confirm ``@capture`` is producing records during local
    development or in an AI-app prototype. Writes to ``sys.stderr`` by default so
    it does not pollute program output.

    Example:
        import briefcase
        briefcase.observe("console")  # or setup(exporter=ConsoleExporter())
    """

    def __init__(self, stream: Optional[TextIO] = None, *, pretty: bool = False) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._pretty = pretty

    async def export(self, decision: Any) -> bool:
        indent = 2 if self._pretty else None
        self._stream.write(json.dumps(decision, default=str, indent=indent) + "\n")
        self._stream.flush()
        return True

    async def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:
            pass

    async def close(self) -> None:
        # Never close a shared stream like sys.stderr / sys.stdout.
        pass
