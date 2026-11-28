"""gRPC client for the engine, built from the vendored contract (``_proto/oci_jj_v1.binpb``).

``DescriptorTransport`` is the default transport of :class:`briefcase.cli.grpc_engine.GrpcEngine`
and of :class:`EngineClient`. It builds message classes from the vendored descriptor set, so it
needs no server reflection and no generated ``*_pb2`` modules (see
``scripts/engine/regen_proto.py`` for why). Server reflection stays available as a fallback:
``BRIEFCASE_GRPC_REFLECTION=1`` selects the old reflection transport, for a server newer than
the vendored contract.

``grpcio`` and ``protobuf`` come with the ``engine`` extra (``pip install 'briefcase-ai[engine]'``)
and are imported lazily, so this module imports without them.
"""
from __future__ import annotations

import os
from importlib.resources import files
from typing import Any, Dict, Iterable, List, Mapping, Optional

from briefcase.cli.grpc_engine import (
    ReflectionTransport,
    ResponseView,
    TransportUnavailable,
    _Resolved,
    _build_pool,
    _is_unauthenticated,
    _rpc_detail,
)

SERVICE = "oci_jj.v1.VcsService"
EXTRA_HINT = "pip install 'briefcase-ai[engine]'"


class EngineError(RuntimeError):
    """A failed engine call, with a message fit to print."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def descriptor_files() -> Dict[str, Any]:
    """The vendored ``FileDescriptorProto``s, by file name."""
    from google.protobuf import descriptor_pb2

    raw = (files("briefcase.engine") / "_proto" / "oci_jj_v1.binpb").read_bytes()
    fds = descriptor_pb2.FileDescriptorSet.FromString(raw)
    return {f.name: f for f in fds.file}


def vendored_services() -> List[str]:
    out: List[str] = []
    for fdp in descriptor_files().values():
        out.extend(f"{fdp.package}.{svc.name}" for svc in fdp.service)
    return out


def vendored_methods(service: str = SERVICE) -> List[str]:
    package, _, name = service.rpartition(".")
    for fdp in descriptor_files().values():
        if fdp.package == package:
            for svc in fdp.service:
                if svc.name == name:
                    return [m.name for m in svc.method]
    return []


class DescriptorTransport(ReflectionTransport):
    """The reflection transport's channel, TLS and token handling, with the schema taken from
    the vendored descriptor set instead of the server."""

    _POOL = None

    def _get_resolved(self):
        if self._resolved is not None:
            return self._resolved
        if DescriptorTransport._POOL is None:
            DescriptorTransport._POOL = _build_pool(descriptor_files())
        if self._channel is None:
            self._channel = self._channel_factory(self.endpoint)
        self._resolved = _Resolved(DescriptorTransport._POOL, vendored_services(), self._channel)
        return self._resolved

    def _wrap(self, exc):
        code = getattr(exc, "code", None)
        name = ""
        if callable(code):
            try:
                name = getattr(code(), "name", "")
            except Exception:  # noqa: BLE001
                name = ""
        if name == "UNAVAILABLE":
            return TransportUnavailable(
                f"the engine is not reachable at {self.endpoint} ({_rpc_detail(exc)}); "
                "check the managed endpoint in BRIEFCASE_ENGINE_ADDR"
            )
        if _is_unauthenticated(exc):
            return TransportUnavailable(
                f"the engine at {self.endpoint} refused the call ({_rpc_detail(exc)}); "
                "set OCI_JJ_AUTH_TOKEN, or run `briefcase-ai engine status` to check the local install"
            )
        return None

    def unary(self, service, method, request):
        try:
            return super().unary(service, method, request)
        except Exception as exc:  # noqa: BLE001 - re-raised below unless it is a transport error
            wrapped = self._wrap(exc)
            if wrapped is not None:
                raise wrapped from exc
            raise

    def server_stream(self, service, method, request):
        try:
            yield from super().server_stream(service, method, request)
        except Exception as exc:  # noqa: BLE001
            wrapped = self._wrap(exc)
            if wrapped is not None:
                raise wrapped from exc
            raise

    def list_services(self):
        """Services the server really serves (asks reflection), for doctor-style probes."""
        return ReflectionTransport.list_services(ReflectionTransport(self.endpoint))


def make_transport(addr: str, token: Optional[str] = None, ca: Optional[str] = None,
                   timeout: float = 10.0, env: Optional[Mapping[str, str]] = None):
    """The default transport for ``addr``: vendored descriptors, or reflection when
    ``BRIEFCASE_GRPC_REFLECTION=1``."""
    env = dict(os.environ if env is None else env)
    if ca and not env.get("OCI_JJ_TLS_CA"):
        env["OCI_JJ_TLS_CA"] = ca
    cls = ReflectionTransport if env.get("BRIEFCASE_GRPC_REFLECTION") == "1" else DescriptorTransport
    return cls(addr, timeout=timeout, token=token, env=env)


def to_dict(msg: Any) -> Dict[str, Any]:
    """A protobuf message as plain data: every field present (defaults included), bytes as
    lowercase hex (digests read the way the engine prints them), enums by name."""
    from briefcase.cli.grpc_engine import _is_repeated

    out: Dict[str, Any] = {}
    for fd in msg.DESCRIPTOR.fields:
        value = getattr(msg, fd.name)
        entry = fd.message_type
        if entry is not None and entry.GetOptions().map_entry:
            value_fd = entry.fields_by_name["value"]
            out[fd.name] = {key: _convert(value_fd, item) for key, item in value.items()}
        elif _is_repeated(fd):
            out[fd.name] = [_convert(fd, item) for item in value]
        else:
            out[fd.name] = _convert(fd, value)
    return out


def _convert(fd: Any, value: Any) -> Any:
    if fd.message_type is not None:
        return to_dict(value)
    if isinstance(value, bytes):
        return value.hex()
    if fd.enum_type is not None:
        named = fd.enum_type.values_by_number.get(value)
        return named.name if named is not None else value
    return value


def _message_to_dict(view: Any) -> Dict[str, Any]:
    backing = getattr(view, "_b", view) if isinstance(view, ResponseView) else view
    if hasattr(backing, "DESCRIPTOR"):
        return to_dict(backing)
    if isinstance(backing, dict):
        return dict(backing)
    return dict(vars(backing))


class EngineClient:
    """``VcsService`` calls by method name, returning plain dicts (proto field names; bytes
    fields as base64 strings, as ``MessageToDict`` renders them).

    ``transport`` is injectable (tests pass a fake). Errors become :class:`EngineError` with a
    message that names the fix; an RPC the server does not implement says the engine is too old.
    """

    def __init__(self, server: Optional[str] = None, repo: Optional[str] = None,
                 transport: Any = None, env: Optional[Mapping[str, str]] = None,
                 timeout: float = 10.0) -> None:
        from briefcase.engine import client_settings

        settings = client_settings(env, server=server)
        env = os.environ if env is None else env
        self.addr = settings["addr"]
        self.repo = repo or env.get("OCI_JJ_REPO") or "rl-gym-env"
        self.settings = settings
        if transport is None:
            try:
                transport = make_transport(self.addr, settings["token"], settings["ca"], timeout, env)
            except TransportUnavailable as exc:
                raise EngineError(f"the engine commands need gRPC ({exc})", code="missing_extra") from exc
        self.transport = transport

    def _raise(self, method: str, exc: BaseException):
        if isinstance(exc, TransportUnavailable):
            raise EngineError(str(exc), code="unavailable") from exc
        code = getattr(exc, "code", None)
        name = ""
        if callable(code):
            try:
                name = getattr(code(), "name", "")
            except Exception:  # noqa: BLE001
                name = ""
        detail = _rpc_detail(exc)
        if name == "UNIMPLEMENTED":
            raise EngineError(f"{method} is not available on this engine ({detail}); "
                              "contact your managed workspace administrator", code=name) from exc
        if name == "PERMISSION_DENIED":
            raise EngineError(f"{method} was refused: {detail}", code=name) from exc
        if name in ("NOT_FOUND", "INVALID_ARGUMENT", "FAILED_PRECONDITION"):
            raise EngineError(detail, code=name) from exc
        if name:
            raise EngineError(f"{method} failed ({name}): {detail}", code=name) from exc
        raise exc

    def call(self, method: str, request: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return _message_to_dict(self.transport.unary(SERVICE, method, request))
        except Exception as exc:  # noqa: BLE001 - classified in _raise
            self._raise(method, exc)
            raise  # pragma: no cover - _raise always raises

    def stream(self, method: str, request: Dict[str, Any], limit: Optional[int] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            items: Iterable[Any] = self.transport.server_stream(SERVICE, method, request)
            for item in items:
                out.append(_message_to_dict(item))
                if limit and len(out) >= limit:
                    break
        except Exception as exc:  # noqa: BLE001
            self._raise(method, exc)
        return out

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()
