"""
Briefcase exporters — ship decision records to external systems.

``BaseExporter`` is the interface; ``ConsoleExporter``, ``JSONLFileExporter``, and
``MemoryExporter`` are ready-to-use implementations. The quickest way to wire one
up is ``briefcase.observe(...)``.
"""

from briefcase.exporters.base import BaseExporter
from briefcase.exporters.console import ConsoleExporter
from briefcase.exporters.file import JSONLFileExporter
from briefcase.exporters.memory import MemoryExporter

__all__ = [
    "BaseExporter",
    "ConsoleExporter",
    "JSONLFileExporter",
    "MemoryExporter",
]
