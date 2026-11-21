"""The ``briefcase receive`` HTTP server: a small local OTLP/HTTP receiver.

Standard library only (``ThreadingHTTPServer``). It accepts ``POST /v1/logs``,
``/v1/metrics`` and ``/v1/traces`` as OTLP/JSON, or as OTLP/protobuf when
``opentelemetry-proto`` is installed, plus ``POST /v1/hooks/cursor`` and
``GET /healthz``. Decoded events become decision records and leave through an
ordinary briefcase exporter.

It binds loopback by default. Any other host requires a bearer token, which is
compared in constant time.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from briefcase._export_mixin import ExportMixin
from briefcase._logging import get_logger
from briefcase.receiver import normalize, otlp
from briefcase.receiver.records import RecordMapper, Redactor

logger = get_logger(__name__)

SERVICE = "briefcase-receiver"
DEFAULT_PORT = 4318
DEFAULT_HOST = "127.0.0.1"
MAX_BODY_BYTES = 8 * 1024 * 1024
OTLP_PATHS = {"/v1/logs": "logs", "/v1/metrics": "metrics", "/v1/traces": "traces"}
CURSOR_HOOK_PATH = "/v1/hooks/cursor"
JSON_TYPE = "application/json"
PROTOBUF_TYPE = "application/x-protobuf"

Response = Tuple[int, bytes, str]


class BodyTooLarge(Exception):
    pass


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_exporter(to: Optional[str]) -> Any:
    """``console`` | ``memory`` | ``PATH.jsonl`` | ``http(s)://...`` | ``otlp``."""
    import os

    if to is None or to == "console":
        from briefcase.exporters.console import ConsoleExporter
        return ConsoleExporter(stream=sys.stdout)
    if to == "otlp":
        from briefcase.exporters.otel import OTelExporter
        return OTelExporter(endpoint=os.environ.get("BRIEFCASE_OTLP_ENDPOINT", "http://localhost:4317"),
                            service_name=SERVICE)
    if to.lower().startswith(("http://", "https://")):
        from briefcase.exporters.http import HTTPExporter
        return HTTPExporter(to, token=os.environ.get("BRIEFCASE_EXPORT_TOKEN") or None)
    from briefcase._observe import _resolve_exporter
    return _resolve_exporter(to)


def _json(status: int, payload: Dict[str, Any]) -> Response:
    return status, json.dumps(payload).encode("utf-8"), JSON_TYPE


def gunzip(data: bytes, limit: int) -> bytes:
    """Decompress at most ``limit`` bytes, so a small gzip body cannot expand
    into an arbitrarily large one."""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decompressor.decompress(data, limit + 1)
    except zlib.error as exc:
        raise ValueError(f"malformed gzip body: {exc}")
    if len(out) > limit or decompressor.unconsumed_tail:
        raise BodyTooLarge()
    return out


class Receiver(ExportMixin):
    """Transport-independent request handling; the HTTP handler is a thin shell."""

    def __init__(self, exporter: Any, *, keep_content: bool = False, emit_metrics: bool = False,
                 token: Optional[str] = None, max_body_bytes: int = MAX_BODY_BYTES,
                 async_export: bool = True, redactor: Optional[Redactor] = None) -> None:
        self._exporter = exporter
        self.async_capture = async_export
        self.token = token or None
        self.max_body_bytes = max_body_bytes
        self.keep_content = keep_content
        self.mapper = RecordMapper(redactor or Redactor(keep_content=keep_content), emit_metrics=emit_metrics)
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "rejected": 0, "signals": 0, "records": 0, "unmapped": 0}

    # -- auth ------------------------------------------------------------------
    def authorized(self, header: Optional[str]) -> bool:
        if self.token is None:
            return True
        expected = f"Bearer {self.token}".encode("utf-8")
        return hmac.compare_digest((header or "").encode("utf-8"), expected)

    # -- ingest ----------------------------------------------------------------
    def _emit(self, events: List[Optional[Dict[str, Any]]], total: int) -> None:
        with self._lock:
            self.stats["signals"] += total
            records: List[Dict[str, Any]] = []
            for event in events:
                if event is None:
                    self.stats["unmapped"] += 1
                    continue
                try:
                    records.extend(self.mapper.ingest(event))
                except Exception:
                    self.stats["unmapped"] += 1
                    logger.warning("could not map a %s event", event.get("type"), exc_info=True)
            self.stats["records"] += len(records)
        for record in records:
            self._trigger_export(record)

    def health(self) -> Dict[str, Any]:
        with self._lock:
            stats = dict(self.stats)
        return {
            "status": "ok", "service": SERVICE, "protobuf": otlp.protobuf_available(),
            "redaction": "off" if self.keep_content else "on",
            "sanitizer": self.mapper.redactor.sanitizer_available, "stats": stats,
        }

    def handle_post(self, path: str, content_type: str, body: bytes) -> Response:
        media = (content_type or "").split(";")[0].strip().lower()
        with self._lock:
            self.stats["requests"] += 1
        try:
            if path == CURSOR_HOOK_PATH:
                if media != JSON_TYPE:
                    return self._reject(415, "the Cursor hook endpoint takes application/json")
                payload = otlp.decode_json(body)
                self._emit([normalize.normalize_cursor(payload)], 1)
                return _json(200, {})
            kind = OTLP_PATHS.get(path)
            if kind is None:
                return self._reject(404, f"unknown path {path}")
            if media == JSON_TYPE:
                signals = otlp.parse(kind, otlp.decode_json(body))
                reply: Response = _json(200, {"partialSuccess": {}})
            elif media == PROTOBUF_TYPE:
                signals = otlp.parse(kind, otlp.decode_protobuf(kind, body), ids_base64=True)
                # An empty Export*ServiceResponse serialises to zero bytes.
                reply = (200, b"", PROTOBUF_TYPE)
            else:
                return self._reject(415, f"unsupported Content-Type {media or '(none)'}; send "
                                         f"{JSON_TYPE} or {PROTOBUF_TYPE}")
        except otlp.ProtobufUnavailable as exc:
            return self._reject(415, str(exc))
        except ValueError as exc:
            return self._reject(400, str(exc))
        normaliser = {"logs": normalize.normalize_log, "traces": normalize.normalize_span,
                      "metrics": normalize.normalize_metric}[kind]
        self._emit([normaliser(signal) for signal in signals], len(signals))
        return reply

    def _reject(self, status: int, message: str) -> Response:
        with self._lock:
            self.stats["rejected"] += 1
        return _json(status, {"error": message})

    def close(self) -> None:
        """Emit records still waiting for their other half, then flush exporters."""
        with self._lock:
            records = self.mapper.flush()
            self.stats["records"] += len(records)
        for record in records:
            self._trigger_export(record)
        try:
            from briefcase._export_mixin import flush
            flush(timeout=5.0)
        except Exception:
            logger.debug("flush on close failed", exc_info=True)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "briefcase-receiver"
    sys_version = ""
    receiver: Receiver  # set on the subclass built in make_server

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.debug("%s %s", self.address_string(), format % args)

    def _send(self, response: Response, close: bool = False) -> None:
        status, body, content_type = response
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _read_chunked(self, limit: int) -> bytes:
        chunks: List[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(1026)
            size = int(line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while self.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
                    pass
                return b"".join(chunks)
            total += size
            if total > limit:
                raise BodyTooLarge()
            chunks.append(self.rfile.read(size))
            self.rfile.readline(3)

    def _read_body(self) -> bytes:
        limit = self.receiver.max_body_bytes
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            body = self._read_chunked(limit)
        else:
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                raise ValueError("a Content-Length header is required")
            if length < 0:
                raise ValueError("invalid Content-Length")
            if length > limit:
                raise BodyTooLarge()
            body = self.rfile.read(length)
        encoding = (self.headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding == "gzip":
            body = gunzip(body, limit)
        elif encoding not in ("identity", ""):
            raise ValueError(f"unsupported Content-Encoding {encoding}")
        return body

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.split("?")[0] == "/healthz":
            self._send(_json(200, self.receiver.health()))
        else:
            self._send(_json(404, {"error": "not found"}))

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        receiver = self.receiver
        if not receiver.authorized(self.headers.get("Authorization")):
            # The body is unread, so the connection cannot be reused.
            self._send(receiver._reject(401, "missing or wrong bearer token"), close=True)
            return
        try:
            body = self._read_body()
        except BodyTooLarge:
            self._send(receiver._reject(413, f"body larger than {receiver.max_body_bytes} bytes"), close=True)
            return
        except ValueError as exc:
            self._send(receiver._reject(400, str(exc)), close=True)
            return
        try:
            response = receiver.handle_post(self.path.split("?")[0], self.headers.get("Content-Type") or "", body)
        except Exception:
            logger.warning("unhandled error in receiver", exc_info=True)
            response = _json(500, {"error": "internal error"})
        self._send(response)


def make_server(receiver: Receiver, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Bind the server. A non-loopback host without a token is refused here, so
    no caller can expose an unauthenticated receiver by accident."""
    if not is_loopback(host) and not receiver.token:
        raise ValueError(
            f"refusing to listen on {host} without a token: pass --token (or set "
            "BRIEFCASE_RECEIVER_TOKEN), or bind 127.0.0.1"
        )
    handler = type("ReceiverHandler", (_Handler,), {"receiver": receiver})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
