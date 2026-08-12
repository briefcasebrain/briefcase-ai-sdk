"""Stub-free gRPC client for ``oci-jj-server`` — schema discovered live via server reflection.

Instead of running ``protoc``/``grpcio-tools`` to emit checked-in ``*_pb2`` stubs (which drift and pin
a proto revision), ``GrpcEngine`` reads the server's ``FileDescriptorProto``s over gRPC reflection at
first use, builds dynamic message classes, and invokes the ``oci_jj.v1.VcsService`` RPCs. The client
always matches what the running server serves — honoring "proto is the single source of truth" (oci-jj
ADR-0005) at *runtime* (ADR-0059 enabled reflection server-side).

Layers, top to bottom:

* ``GrpcEngine`` — the verb surface (``call_attach_bench``/``call_diff_bench``/…). Drop-in beside
  ``OciJJEngine``: pure ``call_*`` builders return an ``RpcCall``; the single side effect is ``exec``.
* ``Transport`` — the wire-format seam (``unary``/``server_stream``/``list_services``). ``GrpcEngine``
  only ever speaks ``(service, method, request_dict)``, so a future zero-dep ``httpx``/JSON transport
  drops in unchanged. ``ReflectionTransport`` is the gRPC implementation.
* descriptor plumbing — collect reflected files, topologically sort them (``DescriptorPool.Add`` is
  dependency-ordered), build dynamic message classes (``message_factory.GetMessageClass``).

Everything is **lazy**: constructing a ``GrpcEngine`` does no network I/O, so an offline ``--dry-run``
(which only builds calls) never touches the server. Reflection + the API-floor check happen on the
first real ``exec``. ``grpc``/``grpc_reflection``/protobuf are imported lazily so this module loads
without the optional ``[run]`` dependencies (the engine selector must be able to inspect it cheaply).
"""
from __future__ import annotations

import os

from briefcase.cli.calls import RpcCall


class TransportUnavailable(RuntimeError):
    """Reflection isn't reachable, or the optional ``[run]`` gRPC deps aren't installed."""


class ApiFloorError(RuntimeError):
    """The server doesn't expose the minimum ``oci_jj.v1.*`` API the client requires."""


class EngineSelectionError(RuntimeError):
    """No engine could be selected (gRPC unavailable and no ``oci-jj`` binary to fall back to)."""


def grpc_available() -> bool:
    """Whether the optional gRPC dependencies are importable (decides ``auto`` engine selection)."""
    try:
        import grpc  # noqa: F401
        from grpc_reflection.v1alpha import reflection_pb2_grpc  # noqa: F401

        return True
    except ImportError:
        return False


def _rpc_detail(exc) -> str:
    details = getattr(exc, "details", None)
    if callable(details):
        try:
            return details() or str(exc)
        except Exception:
            return str(exc)
    return str(exc)


class _FailedView:
    """A transport-neutral *failed* result (returncode != 0) carrying the server/transport error, so a
    gRPC error becomes a clean ``status=error`` + message instead of an unhandled ``RpcError`` traceback."""

    returncode = 1

    def __init__(self, detail: str) -> None:
        self._detail = detail

    def get(self, name, default=None):
        if name in ("unavailable_reason", "error"):
            return self._detail
        return default


class ResponseView:
    """Transport-neutral read view over a response (a protobuf message or a JSON/dict body)."""

    def __init__(self, backing) -> None:
        self._b = backing

    def get(self, name, default=None):
        b = self._b
        if hasattr(b, "DESCRIPTOR"):  # protobuf message
            return getattr(b, name, default)
        if isinstance(b, dict):
            return b.get(name, default)
        return getattr(b, name, default)

    def as_dict(self) -> dict:
        b = self._b
        if hasattr(b, "DESCRIPTOR"):
            from google.protobuf.json_format import MessageToDict

            return MessageToDict(b, preserving_proto_field_name=True)
        return dict(b) if isinstance(b, dict) else dict(vars(b))


# --------------------------------------------------------------------------------------
# Pure descriptor helpers (protobuf only — no grpc / grpc_reflection). Unit-tested directly.
# --------------------------------------------------------------------------------------
def _toposort_files(files: dict):
    """Order ``FileDescriptorProto``s so every file's dependencies precede it.

    ``DescriptorPool.Add`` raises if a file is added before a file it ``import``s, so the reflected
    set (which arrives in arbitrary order) must be topologically sorted first. ``files`` maps
    filename → ``FileDescriptorProto``.
    """
    ordered = []
    seen = set()

    def visit(name, stack):
        if name in seen or name in stack:
            return
        fdp = files.get(name)
        if fdp is None:
            return  # dependency not in the collected set (e.g. a well-known type already in the pool)
        stack.add(name)
        for dep in fdp.dependency:
            visit(dep, stack)
        stack.discard(name)
        seen.add(name)
        ordered.append(fdp)

    for name in list(files):
        visit(name, set())
    return ordered


def _build_pool(files: dict):
    """Build a fresh ``DescriptorPool`` from collected files (added in dependency order)."""
    from google.protobuf import descriptor_pool

    pool = descriptor_pool.DescriptorPool()
    for fdp in _toposort_files(files):
        pool.Add(fdp)
    return pool


def _message_class(pool, full_name: str):
    from google.protobuf import message_factory

    return message_factory.GetMessageClass(pool.FindMessageTypeByName(full_name))


def _build_request(req_cls, request: dict):
    """Populate a request message from a dict.

    Resolves enum *names* to numbers (``"BENCH"`` → 7) via the field descriptor and ignores unknown
    keys, so the client stays forward-compatible with additive proto changes.
    """
    msg = req_cls()
    for key, value in request.items():
        field = msg.DESCRIPTOR.fields_by_name.get(key)
        if field is None:
            continue
        if field.enum_type is not None and isinstance(value, str):
            value = field.enum_type.values_by_name[value].number
        setattr(msg, key, value)
    return msg


def _normalize_endpoint(endpoint: str) -> str:
    """``http://host:port`` → ``host:port`` (grpc channels want a bare authority)."""
    for prefix in ("http://", "https://", "grpc://", "dns://"):
        if endpoint.startswith(prefix):
            return endpoint[len(prefix):]
    return endpoint


# --------------------------------------------------------------------------------------
# Transport seam
# --------------------------------------------------------------------------------------
class Transport:
    """Wire-format seam. The verb layer only ever speaks ``(service, method, request_dict)``."""

    def unary(self, service: str, method: str, request: dict) -> "ResponseView":
        raise NotImplementedError

    def server_stream(self, service: str, method: str, request: dict):
        raise NotImplementedError

    def list_services(self) -> list:
        raise NotImplementedError

    def close(self) -> None:
        pass


class _Resolved:
    """Per-endpoint, per-process cache: descriptor pool, message classes, channel callables."""

    def __init__(self, pool, services, channel) -> None:
        self.pool = pool
        self.services = services
        self.channel = channel
        self._msg: dict = {}
        self._calls: dict = {}

    def _mc(self, full_name):
        if full_name not in self._msg:
            self._msg[full_name] = _message_class(self.pool, full_name)
        return self._msg[full_name]

    def method_io(self, service, method):
        svc = self.pool.FindServiceByName(service)
        m = svc.FindMethodByName(method)
        return self._mc(m.input_type.full_name), self._mc(m.output_type.full_name), f"/{service}/{method}"


class ReflectionTransport(Transport):
    """gRPC transport that discovers the schema via server reflection.

    Construction does no network I/O — the channel and the reflected descriptor pool are built lazily
    on first use (and cached per endpoint for the process), so an offline ``--dry-run`` never reflects.
    An ``https://`` endpoint dials TLS with the system trust roots; other endpoints dial plaintext.
    """

    _CACHE: dict = {}

    def __init__(self, endpoint: str, timeout: float = 5.0, channel_factory=None) -> None:
        try:
            import grpc
            from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc
        except ImportError as e:  # optional [run] deps not installed
            raise TransportUnavailable(
                "the gRPC engine needs the optional dependencies "
                "(pip install 'briefcase-ai[run]'): " + str(e)
            ) from e
        self._grpc = grpc
        self._reflection_pb2 = reflection_pb2
        self._reflection_pb2_grpc = reflection_pb2_grpc
        self._timeout = timeout
        use_tls = endpoint.startswith("https://")
        self.endpoint = _normalize_endpoint(endpoint)
        if channel_factory is not None:
            self._channel_factory = channel_factory
            transport_kind = None
        elif use_tls:
            self._channel_factory = self._secure_channel
            transport_kind = "tls"
        else:
            self._channel_factory = grpc.insecure_channel
            transport_kind = "plain"
        # The cache key carries the transport kind: http and https to one
        # authority resolve to different channels, so they must not share
        # a _Resolved (its call objects are bound to one channel). A custom
        # factory gets no key at all and resolves per instance: only the
        # factory object identifies its channel, and identity is not a usable
        # cache key (CPython reuses an id once the factory is collected).
        self._cache_key = None if transport_kind is None else (self.endpoint, transport_kind)
        self._channel = None
        self._refl = None
        self._resolved = None

    def _secure_channel(self, endpoint):
        return self._grpc.secure_channel(endpoint, self._grpc.ssl_channel_credentials())

    # ---- lazy reflection → descriptor pool (cached per endpoint) ----
    def _get_resolved(self):
        if self._resolved is not None:
            return self._resolved
        try:
            if self._channel is None:
                self._channel = self._channel_factory(self.endpoint)
                self._refl = self._reflection_pb2_grpc.ServerReflectionStub(self._channel)
            cached = self._CACHE.get(self._cache_key) if self._cache_key else None
            if cached is None:
                services = self._list_services_raw()
                files = self._collect_files(services)
                cached = _Resolved(_build_pool(files), services, self._channel)
                if self._cache_key:
                    self._CACHE[self._cache_key] = cached
            self._resolved = cached
            return cached
        except TransportUnavailable:
            raise
        except Exception as e:  # grpc.RpcError, protobuf TypeError on a bad descriptor, …
            raise TransportUnavailable(
                f"oci-jj-server reflection unreachable at {self.endpoint} ({e}); "
                f"is the stack up? try 'briefcase stack up', or set BRIEFCASE_ENGINE=cli."
            ) from e

    def _info(self, **kw):
        req = self._reflection_pb2.ServerReflectionRequest(**kw)
        return next(self._refl.ServerReflectionInfo(iter([req]), timeout=self._timeout))

    def _list_services_raw(self):
        resp = self._info(list_services="")
        return [s.name for s in resp.list_services_response.service]

    def _collect_files(self, services):
        """FileContainingSymbol for each service (tonic returns the transitive closure), then resolve
        any still-missing ``import`` by filename until the dependency set is closed."""
        from google.protobuf import descriptor_pb2

        out: dict = {}
        pending = []

        def ingest(raw):
            fdp = descriptor_pb2.FileDescriptorProto.FromString(raw)
            if fdp.name not in out:
                out[fdp.name] = fdp
                pending.extend(d for d in fdp.dependency if d not in out)

        for svc in services:
            if svc.startswith("grpc.reflection."):
                continue
            resp = self._info(file_containing_symbol=svc)
            for raw in resp.file_descriptor_response.file_descriptor_proto:
                ingest(raw)
        while pending:
            name = pending.pop()
            if name in out:
                continue
            resp = self._info(file_by_filename=name)
            for raw in resp.file_descriptor_response.file_descriptor_proto:
                ingest(raw)
        return out

    # ---- invocation ----
    def unary(self, service, method, request):
        resolved = self._get_resolved()
        req_cls, resp_cls, path = resolved.method_io(service, method)
        call = resolved._calls.get(("u", path))
        if call is None:
            call = self._channel.unary_unary(
                path,
                request_serializer=req_cls.SerializeToString,
                response_deserializer=resp_cls.FromString,
            )
            resolved._calls[("u", path)] = call
        return ResponseView(call(_build_request(req_cls, request), timeout=self._timeout))

    def server_stream(self, service, method, request):
        resolved = self._get_resolved()
        req_cls, resp_cls, path = resolved.method_io(service, method)
        call = self._channel.unary_stream(
            path,
            request_serializer=req_cls.SerializeToString,
            response_deserializer=resp_cls.FromString,
        )
        for resp in call(_build_request(req_cls, request), timeout=self._timeout):
            yield ResponseView(resp)

    def list_services(self):
        return list(self._get_resolved().services)

    def close(self):
        try:
            if self._channel is not None:
                self._channel.close()
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# GrpcEngine — the verb surface (drop-in beside OciJJEngine)
# --------------------------------------------------------------------------------------
class GrpcEngine:
    SERVICE = "oci_jj.v1.VcsService"
    API_FLOOR = "oci_jj.v1"

    def __init__(self, server=None, repo=None, transport=None) -> None:
        self.server = server or os.environ.get("OCI_JJ_SERVER", "http://127.0.0.1:50051")
        self.repo = repo or os.environ.get("OCI_JJ_REPO", "rl-gym-env")
        # Constructing the default transport does no I/O; reflection happens on first exec.
        self._transport = transport if transport is not None else ReflectionTransport(self.server)
        self._ready = False

    def _ensure_ready(self):
        """Lazily verify the server meets the API floor on first real RPC (not at construction, so
        offline ``--dry-run`` never reflects)."""
        if self._ready:
            return
        services = self._transport.list_services()
        if not any(s.startswith(self.API_FLOOR + ".") for s in services):
            raise ApiFloorError(
                f"server at {self.server} exposes no {self.API_FLOOR}.* service "
                f"(saw: {', '.join(services) or 'none'}). Upgrade the oci-jj stack, "
                f"or set BRIEFCASE_ENGINE=cli to use the oci-jj binary."
            )
        self._ready = True

    # ---- pure call builders (Call seam — mirror OciJJEngine.argv_*; no I/O) ----
    def call_attach_bench(self, candidate, baseline, dataset_uri, metric="f1") -> RpcCall:
        # repo = the oci-jj repository (--repo); ref = the candidate; baseline_ref = the checkpoint.
        req = {"repo": self.repo, "ref": candidate, "run": True,
               "dataset_uri": dataset_uri, "metric": metric}
        if baseline:
            req["baseline_ref"] = baseline
        return RpcCall(self.SERVICE, "AttachBench", req)

    def call_hunt(self, candidate, dataset_uri, params_json=None, tag_prefix=None) -> RpcCall:
        req = {"repo": self.repo, "env_ref": candidate, "dataset_uri": dataset_uri}
        if params_json:
            req["params_json"] = params_json
        if tag_prefix:
            req["tag_prefix"] = tag_prefix
        return RpcCall(self.SERVICE, "RunHunt", req)

    def call_diff_bench(self, baseline, candidate) -> RpcCall:
        return RpcCall(self.SERVICE, "Diff",
                       {"repo": self.repo, "from_ref": baseline, "to_ref": candidate, "depth": "BENCH"})

    def call_provenance(self, ref) -> RpcCall:
        return RpcCall(self.SERVICE, "Provenance", {"repo": self.repo, "ref": ref})

    def call_log(self, family, limit=0) -> RpcCall:
        return RpcCall(self.SERVICE, "Log",
                       {"repo": self.repo, "ref": family, "limit": limit}, streaming=True)

    # ---- the single side effect ----
    def exec(self, call, env=None):
        # ``env`` is accepted for signature-parity with OciJJEngine.exec but ignored: a gRPC run's
        # secrets flow into the verdict job server-side, not through the client process env.
        self._ensure_ready()
        try:
            if getattr(call, "streaming", False):
                return list(self._transport.server_stream(call.service, call.method, call.request))
            return self._transport.unary(call.service, call.method, call.request)
        except TransportUnavailable as e:
            return _FailedView(str(e))
        except Exception as e:
            # A server-side gRPC error (RpcError has .code()/.details()) becomes a clean failed result;
            # anything else (a real client bug) propagates.
            if hasattr(e, "code") and hasattr(e, "details"):
                return _FailedView(_rpc_detail(e))
            raise
