"""End-to-end ReflectionTransport against a real, in-process gRPC server with reflection enabled.

Exercises the parts the pure-helper tests can't: the live reflection RPC walk (list services + collect
descriptors), per-endpoint caching, and a full dynamic unary round-trip — including enum-by-name across
the wire. No oci-jj or Docker needed. Skipped automatically (via conftest) when grpcio-reflection is
absent, since this module imports it at top.
"""
from concurrent import futures

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from grpc_reflection.v1alpha import reflection

import pytest

from briefcase.cli.grpc_engine import ReflectionTransport

_STR = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
_INT32 = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
_ENUM = descriptor_pb2.FieldDescriptorProto.TYPE_ENUM
_OPT = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

SERVICE = "bench.v1.Echo"


def _file_descriptor():
    fdp = descriptor_pb2.FileDescriptorProto(name="bench/v1/echo.proto", package="bench.v1", syntax="proto3")
    req = fdp.message_type.add(name="PingRequest")
    req.field.add(name="text", number=1, type=_STR, label=_OPT)
    depth = req.enum_type.add(name="Depth")
    depth.value.add(name="SHALLOW", number=0)
    depth.value.add(name="BENCH", number=7)
    req.field.add(name="depth", number=2, type=_ENUM, type_name=".bench.v1.PingRequest.Depth", label=_OPT)
    rep = fdp.message_type.add(name="PingReply")
    rep.field.add(name="echoed", number=1, type=_STR, label=_OPT)
    rep.field.add(name="depth_seen", number=2, type=_INT32, label=_OPT)
    svc = fdp.service.add(name="Echo")
    m = svc.method.add(name="Ping")
    m.input_type = ".bench.v1.PingRequest"
    m.output_type = ".bench.v1.PingReply"
    return fdp


@pytest.fixture
def reflection_server():
    pool = descriptor_pool.DescriptorPool()
    pool.Add(_file_descriptor())
    PingRequest = message_factory.GetMessageClass(pool.FindMessageTypeByName("bench.v1.PingRequest"))
    PingReply = message_factory.GetMessageClass(pool.FindMessageTypeByName("bench.v1.PingReply"))

    def ping(request, context):
        return PingReply(echoed=request.text, depth_seen=request.depth)

    handler = grpc.method_handlers_generic_handler(
        SERVICE,
        {"Ping": grpc.unary_unary_rpc_method_handler(
            ping, request_deserializer=PingRequest.FromString, response_serializer=PingReply.SerializeToString)},
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((handler,))
    reflection.enable_server_reflection((SERVICE,), server, pool=pool)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.stop(0)
        ReflectionTransport._CACHE.clear()


def test_list_services_discovers_the_service(reflection_server):
    t = ReflectionTransport(reflection_server, timeout=5.0)
    assert SERVICE in t.list_services()


def test_unary_roundtrip_with_enum_by_name(reflection_server):
    t = ReflectionTransport(reflection_server, timeout=5.0)
    resp = t.unary(SERVICE, "Ping", {"text": "hi", "depth": "BENCH"})
    assert resp.get("echoed") == "hi"
    assert resp.get("depth_seen") == 7  # "BENCH" resolved to 7 by name and crossed the wire


def test_resolution_is_cached_per_endpoint(reflection_server):
    t = ReflectionTransport(reflection_server, timeout=5.0)
    t.list_services()
    first = t._get_resolved()
    second = t._get_resolved()
    assert first is second
    # a second transport to the same endpoint reuses the process-wide cache
    t2 = ReflectionTransport(reflection_server, timeout=5.0)
    assert t2._get_resolved() is first


class _RequireBearer(grpc.ServerInterceptor):
    """Rejects every RPC (reflection included) without `authorization: Bearer sek`, like
    oci-jj-server with OCI_JJ_AUTH_TOKENS set (rust/crates/oci-jj-server/src/auth.rs)."""

    def intercept_service(self, continuation, details):
        if ("authorization", "Bearer sek") in tuple(details.invocation_metadata or ()):
            return continuation(details)

        def deny(request_or_iterator, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid bearer token")

        handler = continuation(details)
        if handler is not None and handler.request_streaming and handler.response_streaming:
            return grpc.stream_stream_rpc_method_handler(deny)
        return grpc.unary_unary_rpc_method_handler(deny)


@pytest.fixture
def secured_server():
    pool = descriptor_pool.DescriptorPool()
    pool.Add(_file_descriptor())
    PingRequest = message_factory.GetMessageClass(pool.FindMessageTypeByName("bench.v1.PingRequest"))
    PingReply = message_factory.GetMessageClass(pool.FindMessageTypeByName("bench.v1.PingReply"))
    handler = grpc.method_handlers_generic_handler(SERVICE, {"Ping": grpc.unary_unary_rpc_method_handler(
        lambda req, ctx: PingReply(echoed=req.text),
        request_deserializer=PingRequest.FromString, response_serializer=PingReply.SerializeToString)})
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2), interceptors=(_RequireBearer(),))
    server.add_generic_rpc_handlers((handler,))
    reflection.enable_server_reflection((SERVICE,), server, pool=pool)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.stop(0)
        ReflectionTransport._CACHE.clear()


def test_bearer_token_reaches_reflection_and_calls(secured_server):
    t = ReflectionTransport(secured_server, timeout=5.0, env={"OCI_JJ_AUTH_TOKEN": "sek"})
    assert SERVICE in t.list_services()
    assert t.unary(SERVICE, "Ping", {"text": "hi"}).get("echoed") == "hi"


def test_missing_token_names_the_setting(secured_server):
    from briefcase.cli.grpc_engine import TransportUnavailable

    with pytest.raises(TransportUnavailable, match="OCI_JJ_AUTH_TOKEN"):
        ReflectionTransport(secured_server, timeout=5.0, env={}).list_services()
