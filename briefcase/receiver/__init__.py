"""``briefcase receive``: a zero-config local OTLP receiver.

Coding tools configured by ``briefcase connect`` send their OpenTelemetry
logs, metrics and traces (and Cursor its hook events) here; the receiver turns
them into briefcase decision records and hands them to an exporter.

    from briefcase.exporters import MemoryExporter
    from briefcase.receiver import Receiver, make_server

    receiver = Receiver(MemoryExporter())
    server = make_server(receiver, port=0)      # 127.0.0.1, ephemeral port
"""

from briefcase.receiver.records import RecordMapper, Redactor
from briefcase.receiver.server import DEFAULT_HOST, DEFAULT_PORT, Receiver, make_exporter, make_server

__all__ = [
    "DEFAULT_HOST", "DEFAULT_PORT", "Receiver", "RecordMapper", "Redactor", "make_exporter", "make_server",
]
