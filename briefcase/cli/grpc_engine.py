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
import sys

from briefcase.cli.calls import RpcCall


class TransportUnavailable(RuntimeError):
    """Reflection isn't reachable, or the optional ``[run]`` gRPC deps aren't installed."""


class ApiFloorError(RuntimeError):
    """The server doesn't expose the minimum ``oci_jj.v1.*`` API the client requires."""


class EngineSelectionError(RuntimeError):
    """No engine could be selected (gRPC unavailable and no ``oci-jj`` binary to fall back to)."""


def grpc_available() -> bool:
    """Whether the optional gRPC dependencies are importable (decides ``auto`` engine selection).

    ``grpcio`` and ``protobuf`` are enough: the default transport builds its messages from the
    vendored contract (``briefcase.engine.client.DescriptorTransport``); ``grpcio-reflection``
    is only needed for the reflection fallback."""
    try:
        import grpc  # noqa: F401
        from google.protobuf import descriptor_pool  # noqa: F401

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


def _is_unauthenticated(exc) -> bool:
    code = getattr(exc, "code", None)
    if callable(code):
        try:
            return getattr(code(), "name", "") == "UNAUTHENTICATED"
        except Exception:
            return False
    return False


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
        if field is None or value is None:
            # None means "not given": leave the proto3 default instead of raising TypeError.
            continue
        if field.enum_type is not None and isinstance(value, str):
            value = field.enum_type.values_by_name[value].number
        if _is_repeated(field):
            # Repeated scalars (e.g. Compose runtime=[...], CheckoutPlan paths=[...]) cannot be assigned.
            getattr(msg, key).extend(value if isinstance(value, (list, tuple)) else [value])
            continue
        setattr(msg, key, value)
    return msg


def _is_repeated(field) -> bool:
    flag = getattr(field, "is_repeated", None)  # protobuf >= 5.28; `label` is deprecated there
    if isinstance(flag, bool):
        return flag
    return getattr(field, "label", 1) == 3


def _normalize_endpoint(endpoint: str) -> str:
    """``http://host:port`` → ``host:port`` (grpc channels want a bare authority)."""
    for prefix in ("http://", "https://", "grpcs://", "grpc://", "dns://"):
        if endpoint.startswith(prefix):
            return endpoint[len(prefix):]
    return endpoint


# The client settings oci-jj's own Rust and Go clients read (``ojj-proto/src/auth.rs``,
# ``go/grpcauth``), honored here so one environment configures every client of a secured server.
TOKEN_ENV = "OCI_JJ_AUTH_TOKEN"
TLS_CA_ENV = "OCI_JJ_TLS_CA"
TLS_CLIENT_CERT_ENV = "OCI_JJ_TLS_CLIENT_CERT"
TLS_CLIENT_KEY_ENV = "OCI_JJ_TLS_CLIENT_KEY"


def _tls_settings(endpoint: str, env) -> dict | None:
    """TLS material for ``endpoint``, or ``None`` for plaintext.

    Same rule as oci-jj's Go clients: TLS when the scheme is ``https://``/``grpcs://`` or
    ``OCI_JJ_TLS_CA`` is set. The CA file replaces the system roots; ``OCI_JJ_TLS_CLIENT_CERT`` and
    ``OCI_JJ_TLS_CLIENT_KEY`` together add a client identity (mTLS).
    """
    ca = env.get(TLS_CA_ENV) or ""
    if not (endpoint.startswith(("https://", "grpcs://")) or ca):
        return None
    cert = env.get(TLS_CLIENT_CERT_ENV) or ""
    key = env.get(TLS_CLIENT_KEY_ENV) or ""
    return {"ca": ca, "cert": cert if (cert and key) else "", "key": key if (cert and key) else ""}


def _read_pem(path: str, what: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise TransportUnavailable(f"cannot read {what} {path!r}: {e}") from e


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

    Security settings match oci-jj's own clients: an ``https://``/``grpcs://`` endpoint, or
    ``OCI_JJ_TLS_CA``, dials TLS (system roots unless the CA file is given; mTLS with
    ``OCI_JJ_TLS_CLIENT_CERT``/``_KEY``); ``OCI_JJ_AUTH_TOKEN`` (or ``token=``) is sent as
    ``authorization: Bearer <token>`` on every RPC, reflection included.
    """

    _CACHE: dict = {}

    def __init__(self, endpoint: str, timeout: float = 5.0, channel_factory=None,
                 token: str | None = None, env=None) -> None:
        try:
            import grpc
        except ImportError as e:  # optional [engine]/[run] deps not installed
            raise TransportUnavailable(
                "the gRPC client needs the optional dependencies "
                "(pip install 'briefcase-ai[engine]'): " + str(e)
            ) from e
        try:
            from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc
        except ImportError:  # only the reflection fallback needs it ([run] extra)
            reflection_pb2 = reflection_pb2_grpc = None
            if type(self) is ReflectionTransport:
                raise TransportUnavailable(
                    "server reflection needs grpcio-reflection (pip install 'briefcase-ai[run]')"
                )
        self._grpc = grpc
        self._reflection_pb2 = reflection_pb2
        self._reflection_pb2_grpc = reflection_pb2_grpc
        self._timeout = timeout
        env = os.environ if env is None else env
        self._tls = _tls_settings(endpoint, env)
        self.set_token(token if token is not None else env.get(TOKEN_ENV))
        self.endpoint = _normalize_endpoint(endpoint)
        transport_kind: object
        if channel_factory is not None:
            self._channel_factory = channel_factory
            transport_kind = None
        elif self._tls is not None:
            self._channel_factory = self._secure_channel
            # Different CA / client identity means a different channel, so it is part of the key.
            transport_kind = ("tls", self._tls["ca"], self._tls["cert"])
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

    def set_token(self, token) -> None:
        """Bearer token sent as call metadata (per call, so it does not change the cached channel)."""
        self._metadata = (("authorization", f"Bearer {token}"),) if token else None

    def _secure_channel(self, endpoint):
        tls = self._tls or {"ca": "", "cert": "", "key": ""}
        if not (tls["ca"] or tls["cert"]):
            return self._grpc.secure_channel(endpoint, self._grpc.ssl_channel_credentials())
        creds = self._grpc.ssl_channel_credentials(
            root_certificates=_read_pem(tls["ca"], TLS_CA_ENV) if tls["ca"] else None,
            private_key=_read_pem(tls["key"], TLS_CLIENT_KEY_ENV) if tls["cert"] else None,
            certificate_chain=_read_pem(tls["cert"], TLS_CLIENT_CERT_ENV) if tls["cert"] else None,
        )
        return self._grpc.secure_channel(endpoint, creds)

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
            if _is_unauthenticated(e):
                raise TransportUnavailable(
                    f"oci-jj-server at {self.endpoint} requires a bearer token "
                    f"({_rpc_detail(e)}); set {TOKEN_ENV}."
                ) from e
            raise TransportUnavailable(
                f"oci-jj-server reflection unreachable at {self.endpoint} ({e}); "
                f"is the engine running? try 'briefcase engine up', or set BRIEFCASE_ENGINE=cli."
            ) from e

    def _info(self, **kw):
        req = self._reflection_pb2.ServerReflectionRequest(**kw)
        return next(self._refl.ServerReflectionInfo(
            iter([req]), timeout=self._timeout, metadata=self._metadata
        ))

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
        return ResponseView(
            call(_build_request(req_cls, request), timeout=self._timeout, metadata=self._metadata)
        )

    def server_stream(self, service, method, request):
        resolved = self._get_resolved()
        req_cls, resp_cls, path = resolved.method_io(service, method)
        call = self._channel.unary_stream(
            path,
            request_serializer=req_cls.SerializeToString,
            response_deserializer=resp_cls.FromString,
        )
        for resp in call(_build_request(req_cls, request), timeout=self._timeout, metadata=self._metadata):
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
        # --server, then BRIEFCASE_ENGINE_ADDR / OCI_JJ_SERVER, then a local `briefcase engine
        # install`, then the default; see briefcase.engine.client_settings.
        from briefcase.engine import client_settings

        settings = client_settings(server=server)
        self.server = settings["addr"]
        self.repo = repo or os.environ.get("OCI_JJ_REPO", "rl-gym-env")
        if transport is None:
            # The vendored contract (no reflection round-trip); BRIEFCASE_GRPC_REFLECTION=1
            # selects reflection instead. Construction does no I/O either way.
            from briefcase.engine.client import make_transport

            transport = make_transport(self.server, settings["token"], settings["ca"], timeout=5.0)
        self._transport = transport
        self._ready = False

    def _ensure_ready(self):
        """Lazily verify the server meets the API floor on first real RPC (not at construction, so
        offline ``--dry-run`` never reflects)."""
        if self._ready:
            return
        if type(self._transport).__name__ == "DescriptorTransport":
            # The vendored contract is the floor; an RPC an older server lacks fails with
            # UNIMPLEMENTED on the call itself.
            self._ready = True
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

    def call_attach_result(self, candidate, baseline, result_path, manifest_path=None) -> RpcCall:
        """AttachBench mode (a): record a verdict.json computed elsewhere (``run=false``)."""
        with open(result_path, encoding="utf-8") as f:
            verdict_json = f.read()
        manifest_json = ""
        if manifest_path:
            with open(manifest_path, encoding="utf-8") as f:
                manifest_json = f.read()
        req = {"repo": self.repo, "ref": candidate, "run": False, "verdict_json": verdict_json,
               "manifest_json": manifest_json}
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
        # ``env`` mirrors OciJJEngine.exec. oci-jj has no RPC field that carries run secrets or env
        # vars to the verdict worker, so the only key that has an effect over gRPC is the client's
        # own OCI_JJ_AUTH_TOKEN (e.g. stored with `briefcase secret set`). Say so instead of
        # dropping the rest silently.
        env = env or {}
        token = env.get(TOKEN_ENV)
        if token and hasattr(self._transport, "set_token"):
            self._transport.set_token(token)
        dropped = sorted(k for k in env if k != TOKEN_ENV)
        if dropped:
            print(
                "warning: not sent to oci-jj (no secret/env channel for runs over gRPC): "
                + ", ".join(dropped),
                file=sys.stderr,
            )
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
