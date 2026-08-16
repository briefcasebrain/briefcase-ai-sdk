"""
Briefcase exporters: ship decision records to external systems.

``BaseExporter`` is the interface; ``ConsoleExporter``, ``JSONLFileExporter``, and
``MemoryExporter`` are ready-to-use implementations. The quickest way to wire one
up is ``briefcase.observe(...)``. ``OTelExporter`` and ``GCPCloudLoggingExporter``
ship records to external systems and import their clients lazily, so they need
their optional extras only at construction time.
"""

from briefcase.exporters.base import BaseExporter
from briefcase.exporters.console import ConsoleExporter
from briefcase.exporters.file import JSONLFileExporter
from briefcase.exporters.gcp_logging import GCPCloudLoggingExporter
from briefcase.exporters.memory import MemoryExporter
from briefcase.exporters.otel import OTelExporter

__all__ = [
    "BaseExporter",
    "ConsoleExporter",
    "GCPCloudLoggingExporter",
    "JSONLFileExporter",
    "MemoryExporter",
    "OTelExporter",
]
