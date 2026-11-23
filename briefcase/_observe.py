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
_SHORTHANDS = ("console", "memory") + _OTLP_SHORTHANDS
#: Names a ``briefcase.exporters`` plugin cannot take (the built-in always wins).
BUILTIN_EXPORTER_NAMES = _SHORTHANDS + ("http", "https", "file", "jsonl")
_PLUGIN_GROUP = "briefcase.exporters"
# The OpenTelemetry specification's default OTLP protocol is http/protobuf,
# whose default endpoint is http://localhost:4318 (traces: /v1/traces).
_DEFAULT_OTLP_PROTOCOL = "http"
_DEFAULT_OTLP_ENDPOINT = {"grpc": "http://localhost:4317", "http": "http://localhost:4318"}
_OTLP_VARIABLES = (
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT",
)


class _DisabledExporter(BaseExporter):
    """Accepts and discards records: ``OTEL_SDK_DISABLED=true`` with an OTLP
    destination."""

    async def export(self, decision: Any) -> bool:
        return True

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _safe_url(value: Any) -> str:
    """``value`` for a log or error message: credentials in the user-info
    part and the whole query string are removed."""
    text = str(value)
    try:
        parts = urlsplit(text)
        if parts.scheme and parts.netloc:
            host = parts.hostname or ""
            if parts.port:
                host += f":{parts.port}"
            marker = "***@" if "@" in parts.netloc else ""
            query = "?***" if parts.query else ""
            return f"{parts.scheme}://{marker}{host}{parts.path}{query}"
    except ValueError:
        pass
    if len(text) > 80:
        text = text[:77] + "..."
    return text


def _check_url(value: str, what: str) -> None:
    """Raise ValueError unless ``value`` is an absolute http(s) URL."""
    try:
        parts = urlsplit(value)
        port_ok = parts.port is None or 0 < parts.port < 65536
    except ValueError:
        parts, port_ok = None, False
    if (parts is None or not port_ok or parts.scheme.lower() not in ("http", "https")
            or not parts.hostname):
        raise ValueError(
            f"{what} is not a valid http(s) URL: {_safe_url(value)!r}. Expected something "
            "like http://localhost:4318 (the scheme is required)."
        )


def _env_token(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """``BRIEFCASE_EXPORTER_TOKEN`` from ``env`` (default: the process
    environment), stripped; None when unset or blank."""
    env = os.environ if env is None else env
    return (env.get(ENV_EXPORTER_TOKEN) or "").strip() or None


def _resolve_exporter(
    exporter: Union[str, BaseExporter, None],
    *,
    token: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> BaseExporter:
    """Build the exporter a shorthand names.

    For an ``http(s)://`` URL the bearer token is ``token`` when given, else
    ``BRIEFCASE_EXPORTER_TOKEN``: the explicit form and the environment form
    authenticate the same way. ``token`` and ``headers`` are meaningless for
    any other destination and raise ValueError rather than being ignored.
    """
    is_url = isinstance(exporter, str) and exporter.lower().startswith(("http://", "https://"))
    if (token or headers) and not is_url:
        given = "token" if token else "headers"
        raise ValueError(
            f"observe(..., {given}=...) only applies to an http(s):// URL destination; "
            "pass the credential to the exporter's own constructor instead."
        )
    if isinstance(exporter, BaseExporter):
        return exporter
    if exporter is None or exporter == "console":
        return ConsoleExporter()
    if exporter == "memory":
        return MemoryExporter()
    if isinstance(exporter, str) and exporter.lower().startswith(("http://", "https://")):
        _check_url(exporter, "The exporter URL")
        return HTTPExporter(exporter, token=token or _env_token(env), headers=headers)
    if isinstance(exporter, str) and exporter.startswith("chain:") and exporter[6:].strip():
        # Tamper-evident hash-chained SQLite store (needs the compiled core). Built-in
        # shorthands are resolved before plugins, so a plugin cannot shadow them.
        from briefcase.exporters.chained import ChainedSqliteExporter

        return ChainedSqliteExporter(exporter[6:].strip())
    if isinstance(exporter, str):
        # "NAME" / "NAME:TARGET" of an installed plugin; checked before the ".jsonl" rule so
        # "acme:out.jsonl" reaches the plugin. Unknown names fall through.
        plugin = _plugin_exporter(exporter)
        if plugin is not None:
            return plugin
    if isinstance(exporter, str) and exporter.endswith(".jsonl"):
        return JSONLFileExporter(exporter)
    raise ValueError(
        f"Unknown exporter shorthand: {_safe_url(exporter)!r}. Pass a BaseExporter instance, "
        "'console', 'memory', a path ending in '.jsonl', 'chain:PATH.db', an http(s):// URL, "
        "or the name of an installed exporter plugin ('NAME' or 'NAME:TARGET'; see `briefcase plugins`)."
    )


def _plugin_name(value: str) -> Optional[str]:
    if ":" not in value and value.endswith(".jsonl"):
        return None  # a plain file path
    name = value.partition(":")[0].strip()
    if len(name) < 2 or name.lower() in BUILTIN_EXPORTER_NAMES or not name.replace("-", "").replace(
            "_", "").replace(".", "").isalnum():
        return None
    return name


def _plugin_exporter(value: str) -> Optional[BaseExporter]:
    """``NAME`` or ``NAME:TARGET`` resolved through a ``briefcase.exporters`` entry point.
    ``None`` when no installed package provides ``NAME``; ValueError when one does but it
    cannot be used (the message says why)."""
    name = _plugin_name(value)
    if name is None:
        return None
    from briefcase import plugins

    if plugins.find(_PLUGIN_GROUP, name) is None:
        return None
    cls = plugins.get(_PLUGIN_GROUP, name, BUILTIN_EXPORTER_NAMES)
    if cls is None:
        raise ValueError(f"Exporter plugin {name!r} cannot be used: {plugins.explain(_PLUGIN_GROUP, name)}")
    target = value.partition(":")[2]
    if hasattr(cls, "from_target"):
        instance = cls.from_target(target)
    elif target:
        instance = cls(target)
    else:
        instance = cls()
    if not isinstance(instance, BaseExporter):
        raise ValueError(f"Exporter plugin {name!r} built {type(instance).__name__}, not a BaseExporter")
    return instance


def _otlp_protocol(env: Mapping[str, str], endpoint: Optional[str] = None) -> str:
    """Map the OTel protocol variables onto OTelExporter's ``"grpc"`` /
    ``"http"``.

    ``OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`` wins over
    ``OTEL_EXPORTER_OTLP_PROTOCOL``. With neither set the answer is the
    OpenTelemetry specification's default, ``http/protobuf``. The protocol
    is never guessed from the endpoint: a port number is a convention, not
    a contract (collectors are routinely remapped or proxied), and a wrong
    guess sends nothing while looking configured. An endpoint on the
    conventional gRPC port with no protocol set is pointed out at WARNING.

    Raises:
        ValueError: a protocol OTelExporter cannot speak (``http/json``).
    """
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
        raise ValueError(
            f"OTLP protocol {raw!r} is not supported; set OTEL_EXPORTER_OTLP_PROTOCOL to "
            "'http/protobuf' or 'grpc'."
        )
    if endpoint:
        try:
            port = urlsplit(endpoint).port
        except ValueError:
            port = None
        if port == 4317:
            logger.warning(
                "briefcase.observe(): the OTLP endpoint uses port 4317, which is "
                "conventionally gRPC, but no OTEL_EXPORTER_OTLP_PROTOCOL is set, so the "
                "specification default http/protobuf is used. Set "
                "OTEL_EXPORTER_OTLP_PROTOCOL=grpc if the collector expects gRPC."
            )
    return _DEFAULT_OTLP_PROTOCOL


def _otlp_settings(env: Mapping[str, str]) -> "tuple[str, str, str]":
    """``(endpoint, protocol, service_name)`` from the standard variables.

    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` is used as given;
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is a base URL, so for HTTP the
    ``/v1/traces`` signal path is appended (as the OTel SDK does).

    Raises:
        ValueError: an endpoint that is not an absolute http(s) URL, or an
            unsupported protocol.
    """
    traces_endpoint = (env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip()
    base_endpoint = (env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    protocol = _otlp_protocol(env, traces_endpoint or base_endpoint)
    if traces_endpoint:
        _check_url(traces_endpoint, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        endpoint = traces_endpoint
    elif base_endpoint:
        _check_url(base_endpoint, "OTEL_EXPORTER_OTLP_ENDPOINT")
        endpoint = base_endpoint
        if protocol == "http" and not endpoint.rstrip("/").endswith("/v1/traces"):
            endpoint = endpoint.rstrip("/") + "/v1/traces"
    else:
        endpoint = _DEFAULT_OTLP_ENDPOINT[protocol]
        if protocol == "http":
            endpoint += "/v1/traces"
    service_name = (env.get("OTEL_SERVICE_NAME") or "").strip() or "briefcase-ai"
    return endpoint, protocol, service_name


def _sdk_disabled(env: Mapping[str, str]) -> bool:
    return (env.get("OTEL_SDK_DISABLED") or "").strip().lower() == "true"


def _otel_from_env(env: Mapping[str, str], strict: bool = False) -> BaseExporter:
    """Build an OTelExporter from the standard OpenTelemetry variables.

    ``OTEL_EXPORTER_OTLP_HEADERS`` (and the ``_TRACES_`` variant) are read
    by the underlying OTLP span exporter itself; they are never read,
    logged or put in an error message here.

    ``OTEL_SDK_DISABLED=true`` yields an exporter that discards records
    (with a WARNING): the variable asks for no OpenTelemetry export, and
    falling back to another destination would send data somewhere it was
    not asked to go.

    When the OpenTelemetry packages are not installed: with ``strict`` an
    ImportError; otherwise the console, with a warning naming the pip
    extra.
    """
    endpoint, protocol, service_name = _otlp_settings(env)
    if _sdk_disabled(env):
        logger.warning(
            "briefcase.observe(): OTEL_SDK_DISABLED=true, so decision records are NOT "
            "exported (the OTLP destination is disabled). Unset it, or set "
            "BRIEFCASE_EXPORTER to another destination."
        )
        return _DisabledExporter()

    try:
        from briefcase.exporters.otel import OTelExporter

        return OTelExporter(endpoint=endpoint, service_name=service_name, protocol=protocol)
    except ImportError as exc:
        message = (
            "briefcase.observe(): an OTLP destination is configured in the "
            "environment, but the OpenTelemetry packages for it are not installed "
            f"({exc}). Install with: pip install 'briefcase-ai[otel]'"
        )
        if strict:
            raise ImportError(message) from exc
        message += ". Falling back to the console exporter."
        logger.warning(message)
        warnings.warn(message, RuntimeWarning, stacklevel=3)
        return ConsoleExporter()


def _exporter_from_env(env: Optional[Mapping[str, str]] = None,
                       strict: bool = False, *,
                       token: Optional[str] = None,
                       headers: Optional[Mapping[str, str]] = None) -> BaseExporter:
    """Resolve the destination from the environment, in order:

    1. ``BRIEFCASE_EXPORTER``: ``console``, ``memory``, a ``*.jsonl`` path,
       an ``http(s)://`` URL (sent with ``BRIEFCASE_EXPORTER_TOKEN`` as a
       bearer token when that is set), or ``otlp`` to use the OpenTelemetry
       variables below. Anything else is a ValueError, never a silent
       fallback.
    2. ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_ENDPOINT``
       (with ``OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`` /
       ``OTEL_EXPORTER_OTLP_PROTOCOL``, ``OTEL_EXPORTER_OTLP_HEADERS``,
       ``OTEL_SERVICE_NAME`` and ``OTEL_SDK_DISABLED``):
       :class:`~briefcase.exporters.otel.OTelExporter`.
    3. The console.
    """
    env = os.environ if env is None else env
    choice = (env.get(ENV_EXPORTER) or "").strip()
    if choice.lower() in _OTLP_SHORTHANDS:
        return _otel_from_env(env, strict)
    if choice:
        return _resolve_exporter(choice, token=token, headers=headers, env=env)
    if any((env.get(name) or "").strip() for name in _OTLP_VARIABLES):
        return _otel_from_env(env, strict)
    return ConsoleExporter()


def check_environment(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Why the destination configured in ``env`` cannot work, or None.

    Checks only what can be checked without side effects (no file is
    opened, no connection made, no exporter built): the
    ``BRIEFCASE_EXPORTER`` value is a known shorthand, and the OTLP
    endpoint and protocol are well formed. ``briefcase exec`` calls this
    before starting the program so a typo fails at once instead of sending
    records to the wrong place. Secrets are not echoed."""
    env = os.environ if env is None else env
    choice = (env.get(ENV_EXPORTER) or "").strip()
    try:
        if choice.lower() in _OTLP_SHORTHANDS or (
            not choice and any((env.get(name) or "").strip() for name in _OTLP_VARIABLES)
        ):
            _otlp_settings(env)
        elif choice.lower().startswith(("http://", "https://")):
            _check_url(choice, f"{ENV_EXPORTER}")
        elif choice.startswith("chain:") and choice[6:].strip():
            return None
        elif choice and choice not in ("console", "memory") and not choice.endswith(".jsonl"):
            name = _plugin_name(choice)
            if name is not None:
                from briefcase import plugins

                plugin = plugins.find(_PLUGIN_GROUP, name)
                if plugin is not None and plugin.state in (plugins.AVAILABLE, plugins.LOADED):
                    return None
                if plugin is not None:
                    return (f"{ENV_EXPORTER} names exporter plugin {name!r}, which cannot be used: "
                            f"{plugins.explain(_PLUGIN_GROUP, name)}")
            hint = ""
            if choice.lower() in _SHORTHANDS:
                hint = f" (did you mean {choice.lower()!r}?)"
            return (
                f"{ENV_EXPORTER}={_safe_url(choice)!r} is not a destination{hint}. Use "
                "console, memory, otlp, a path ending in .jsonl, chain:PATH.db, an http(s):// URL, "
                "or an installed exporter plugin (`briefcase plugins list`)."
            )
    except ValueError as exc:
        return str(exc)
    return None


def observe(
    exporter: Union[str, BaseExporter, None] = _FROM_ENV,
    *,
    level: Optional[Union[int, str]] = None,
    token: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
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
                JSON batches. Its bearer token is ``token``, else
                ``BRIEFCASE_EXPORTER_TOKEN`` when set
              - ``"NAME"`` or ``"NAME:TARGET"`` → the exporter an installed
                package registers under the ``briefcase.exporters`` entry
                point group (see :mod:`briefcase.plugins`)

            When omitted, the destination comes from the environment:
            ``BRIEFCASE_EXPORTER`` (any shorthand above, or ``otlp``; an
            http(s) URL is sent with ``BRIEFCASE_EXPORTER_TOKEN`` as its
            bearer token when set), else the standard OpenTelemetry
            variables (``OTEL_EXPORTER_OTLP_ENDPOINT`` or the
            ``_TRACES_ENDPOINT`` override, ``OTEL_EXPORTER_OTLP_PROTOCOL``,
            ``OTEL_EXPORTER_OTLP_HEADERS``, ``OTEL_SERVICE_NAME``) →
            :class:`~briefcase.exporters.otel.OTelExporter`, else the
            console. The protocol is the one the variables name, else the
            OpenTelemetry default ``http/protobuf``; it is not guessed from
            the port. A malformed endpoint, an unsupported protocol or an
            unknown ``BRIEFCASE_EXPORTER`` value raises ValueError.
            ``OTEL_SDK_DISABLED=true`` with an OTLP destination exports
            nothing (a warning says so). If an OTLP endpoint is set but the
            ``otel`` extra is not installed, records go to the console and a
            warning says so.
        level: If given, also enable briefcase logging at this level
            (see :func:`briefcase.enable_logging`).
        token: Bearer token for an ``http(s)://`` destination (the URL form
            or ``BRIEFCASE_EXPORTER`` naming a URL). Overrides
            ``BRIEFCASE_EXPORTER_TOKEN``. A ValueError for any other
            destination.
        headers: Extra request headers for an ``http(s)://`` destination
            (they override the defaults, including ``Authorization``).

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
        resolved = _exporter_from_env(token=token, headers=headers)
        if (token or headers) and not isinstance(resolved, HTTPExporter):
            raise ValueError(
                "observe(token=..., headers=...) only applies to an http(s):// destination, "
                "but the environment resolved to something else."
            )
    else:
        resolved = _resolve_exporter(exporter, token=token, headers=headers)
    setup(exporter=resolved)
    if level is not None:
        from briefcase._logging import enable_logging

        enable_logging(level)
    return resolved


__all__ = ["check_environment", "observe"]
