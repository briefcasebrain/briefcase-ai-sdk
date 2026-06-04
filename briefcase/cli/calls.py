"""Transport-neutral *call* objects shared by the engines.

A run verb is built as an opaque ``Call`` so the façade (``commands.py``) never sees whether the
engine speaks subprocess or gRPC. The handler builds a call, renders ``describe()`` for ``--dry-run``,
stores ``record()`` in the run registry, and hands the call to ``engine.exec(call, env)``.

``OciJJEngine`` builds ``ArgvCall`` (an ``oci-jj`` argv list); ``GrpcEngine`` builds ``RpcCall`` (a
``VcsService`` method + request dict). Neither type imports ``grpc``/``subprocess`` — they are plain
data, so the verb layer stays free of the transport.
"""
from __future__ import annotations


class ArgvCall:
    """A subprocess invocation: the ``oci-jj`` argv built by ``OciJJEngine.argv_*``."""

    streaming = False

    def __init__(self, argv) -> None:
        self.argv = list(argv)

    def describe(self) -> str:
        return " ".join(self.argv)

    def record(self) -> dict:
        return {"argv": self.argv}


class RpcCall:
    """A gRPC invocation: a ``VcsService`` method plus its request dict (``GrpcEngine.call_*``)."""

    def __init__(self, service: str, method: str, request: dict, streaming: bool = False) -> None:
        self.service = service
        self.method = method
        self.request = dict(request)
        self.streaming = streaming

    def describe(self) -> str:
        fields = ", ".join(f"{k}={v!r}" for k, v in self.request.items())
        return f"{self.method}({fields})"

    def record(self) -> dict:
        return {"rpc": f"{self.service}/{self.method}", "request": self.request}
