"""
Briefcase exporters: ship decision records to external systems.

``BaseExporter`` is the interface; ``ConsoleExporter``, ``JSONLFileExporter``,
``MemoryExporter``, ``HTTPExporter`` (stdlib-only JSON batches over
HTTPS) and ``ChainedSqliteExporter`` (tamper-evident hash chain; needs the
compiled core) are ready-to-use implementations. The quickest way to wire one
up is ``briefcase.observe(...)``. ``OTelExporter`` and ``GCPCloudLoggingExporter``
ship records to external systems and import their clients lazily, so they need
their optional extras only at construction time.
"""

from briefcase.exporters.base import BaseExporter, get_field
from briefcase.exporters.chained import ChainedSqliteExporter
from briefcase.exporters.console import ConsoleExporter
from briefcase.exporters.file import JSONLFileExporter
from briefcase.exporters.gcp_logging import GCPCloudLoggingExporter
from briefcase.exporters.http import HTTPExporter
from briefcase.exporters.memory import MemoryExporter
from briefcase.exporters.otel import OTelExporter

__all__ = [
    "BaseExporter",
    "ChainedSqliteExporter",
    "ConsoleExporter",
    "GCPCloudLoggingExporter",
    "HTTPExporter",
    "JSONLFileExporter",
    "MemoryExporter",
    "OTelExporter",
    "get_field",
]
