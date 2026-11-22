"""One-line observability setup: :func:`briefcase.observe`."""

from __future__ import annotations

import os
import warnings
from typing import Any, Mapping, Optional, Union
from urllib.parse import urlsplit

from briefcase._logging import get_logger
from briefcase.config import setup
from briefcase.exporters.base import BaseExporter
from briefcase.exporters.console import ConsoleExporter
from briefcase.exporters.file import JSONLFileExporter
from briefcase.exporters.http import HTTPExporter
from briefcase.exporters.memory import MemoryExporter

logger = get_logger(__name__)

# Default argument of observe(): "nothing passed, resolve from the environment".
_FROM_ENV: Any = object()

ENV_EXPORTER = "BRIEFCASE_EXPORTER"
ENV_EXPORTER_TOKEN = "BRIEFCASE_EXPORTER_TOKEN"
_OTLP_SHORTHANDS = ("otlp", "otel")
_DEFAULT_OTLP_ENDPOINT = {"grpc": "http://localhost:4317", "http": "http://localhost:4318"}


def _resolve_exporter(
    exporter: Union[str, BaseExporter, None],
    *,
    token: Optional[str] = None,
) -> BaseExporter:
    if isinstance(exporter, BaseExporter):
        return exporter
    if exporter is None or exporter == "console":
        return ConsoleExporter()
    if exporter == "memory":
        return MemoryExporter()
    if isinstance(exporter, str) and exporter.lower().startswith(("http://", "https://")):
        return HTTPExporter(exporter, token=token) if token else HTTPExporter(exporter)
    if isinstance(exporter, str) and exporter.endswith(".jsonl"):
        return JSONLFileExporter(exporter)
    if isinstance(exporter, str) and exporter.startswith("chain:") and exporter[6:].strip():
        # Tamper-evident hash-chained SQLite store (needs the compiled core).
        from briefcase.exporters.chained import ChainedSqliteExporter

        return ChainedSqliteExporter(exporter[6:].strip())
    raise ValueError(
        f"Unknown exporter shorthand: {exporter!r}. Pass a BaseExporter instance, "
        "'console', 'memory', a path ending in '.jsonl', 'chain:PATH.db', or an http(s):// URL."
    )


def _otlp_protocol(env: Mapping[str, str], endpoint: Optional[str]) -> str:
    """Map the OTel protocol variables onto OTelExporter's ``"grpc"`` /
    ``"http"``. With no protocol set, an endpoint on port 4318 or one ending
    in ``/v1/traces`` is HTTP and anything else gRPC (OTelExporter's own
    default)."""
    raw = (
        env.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
        or env.get("OTEL_EXPORTER_OTLP_PROTOCOL")
        or ""
    ).strip().lower()
    if raw == "grpc":
        return "grpc"
    if raw in ("http/protobuf", "http"):
        return "http"
    if raw:
        logger.warning(
            "OTLP protocol %r is not supported by OTelExporter; using http/protobuf.", raw,
        )
        return "http"
    if endpoint:
        try:
            parts = urlsplit(endpoint)
            if parts.port == 4318 or parts.path.rstrip("/").endswith("/v1/traces"):
                return "http"
        except ValueError:
            pass
    return "grpc"


def _otel_from_env(env: Mapping[str, str]) -> BaseExporter:
    """Build an OTelExporter from the standard OpenTelemetry variables.

    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` is used as given;
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is a base URL, so for HTTP the
    ``/v1/traces`` signal path is appended (as the OTel SDK does).
    ``OTEL_EXPORTER_OTLP_HEADERS`` (and the ``_TRACES_`` variant) are read
    by the underlying OTLP span exporter itself.

    Falls back to the console, with a warning naming the pip extra, when
    the OpenTelemetry packages are not installed.
    """
    traces_endpoint = (env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip()
    base_endpoint = (env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    protocol = _otlp_protocol(env, traces_endpoint or base_endpoint)
    if traces_endpoint:
        endpoint = traces_endpoint
    elif base_endpoint:
        endpoint = base_endpoint
        if protocol == "http" and not endpoint.rstrip("/").endswith("/v1/traces"):
            endpoint = endpoint.rstrip("/") + "/v1/traces"
    else:
        endpoint = _DEFAULT_OTLP_ENDPOINT[protocol]
        if protocol == "http":
            endpoint += "/v1/traces"
    service_name = (env.get("OTEL_SERVICE_NAME") or "").strip() or "briefcase-ai"

    try:
        from briefcase.exporters.otel import OTelExporter

        return OTelExporter(endpoint=endpoint, service_name=service_name, protocol=protocol)
    except ImportError as exc:
        message = (
            "briefcase.observe(): an OTLP destination is configured in the "
            "environment, but the OpenTelemetry packages are not installed "
            f"({exc}). Falling back to the console exporter. Install with: "
            "pip install 'briefcase-ai[otel]'"
        )
        logger.warning(message)
        warnings.warn(message, RuntimeWarning, stacklevel=3)
        return ConsoleExporter()


def _exporter_from_env(env: Optional[Mapping[str, str]] = None) -> BaseExporter:
    """Resolve the destination from the environment, in order:

    1. ``BRIEFCASE_EXPORTER``: ``console``, ``memory``, a ``*.jsonl`` path,
       an ``http(s)://`` URL (sent with ``BRIEFCASE_EXPORTER_TOKEN`` as a
       bearer token when that is set), or ``otlp`` to use the OpenTelemetry
       variables below.
    2. ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``
       (with ``OTEL_EXPORTER_OTLP_PROTOCOL``, ``OTEL_EXPORTER_OTLP_HEADERS``
       and ``OTEL_SERVICE_NAME``): :class:`~briefcase.exporters.otel.OTelExporter`.
    3. The console.
    """
    env = os.environ if env is None else env
    choice = (env.get(ENV_EXPORTER) or "").strip()
    if choice.lower() in _OTLP_SHORTHANDS:
        return _otel_from_env(env)
    if choice:
        token = (env.get(ENV_EXPORTER_TOKEN) or "").strip() or None
        return _resolve_exporter(choice, token=token)
    if (env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip() or (
        env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or ""
    ).strip():
        return _otel_from_env(env)
    return ConsoleExporter()


def observe(
    exporter: Union[str, BaseExporter, None] = _FROM_ENV,
    *,
    level: Optional[Union[int, str]] = None,
) -> BaseExporter:
    """Wire up decision-capture export in one call.

    Without this, ``@capture`` records decisions but has nowhere to send them.
    ``observe`` configures the global exporter so captured records are emitted.

    Args:
        exporter: A :class:`~briefcase.exporters.base.BaseExporter` instance, or a
            shorthand string:
              - ``"console"`` → :class:`ConsoleExporter`
              - ``"memory"`` → :class:`MemoryExporter`
              - a path ending in ``.jsonl`` → :class:`JSONLFileExporter`
              - ``"chain:PATH.db"`` → :class:`~briefcase.exporters.chained.ChainedSqliteExporter`
                (tamper-evident hash chain; checkpoints signed with the key file
                in ``BRIEFCASE_SIGNING_KEY`` when set)
              - an ``https://`` URL (or ``http://`` to localhost) →
                :class:`~briefcase.exporters.http.HTTPExporter`, which POSTs
                JSON batches; construct it yourself to pass a token

            When omitted, the destination comes from the environment:
            ``BRIEFCASE_EXPORTER`` (any shorthand above, or ``otlp``; an
            http(s) URL is sent with ``BRIEFCASE_EXPORTER_TOKEN`` as its
            bearer token when set), else the standard OpenTelemetry
            variables (``OTEL_EXPORTER_OTLP_ENDPOINT``,
            ``OTEL_EXPORTER_OTLP_PROTOCOL``, ``OTEL_EXPORTER_OTLP_HEADERS``,
            ``OTEL_SERVICE_NAME``) →
            :class:`~briefcase.exporters.otel.OTelExporter`, else the
            console. If an OTLP endpoint is set but the ``otel`` extra is not
            installed, records go to the console and a warning says so.
        level: If given, also enable briefcase logging at this level
            (see :func:`briefcase.enable_logging`).

    Returns:
        The configured exporter — so a :class:`MemoryExporter` can be inspected.

    Example:
        import briefcase
        mem = briefcase.observe("memory")

        @briefcase.capture(async_capture=False)
        def classify(text): return text.upper()

        classify("hello")
        print(mem.records)  # -> [{'decision_id': ..., 'function_name': 'classify', ...}]
    """
    if exporter is _FROM_ENV:
        resolved = _exporter_from_env()
    else:
        resolved = _resolve_exporter(exporter)
    setup(exporter=resolved)
    if level is not None:
        from briefcase._logging import enable_logging

        enable_logging(level)
    return resolved


__all__ = ["observe"]
