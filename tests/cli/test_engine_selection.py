"""make_engine: BRIEFCASE_ENGINE selection (grpc | cli | auto). Construction is I/O-free; reflection
and the binary path are only exercised on a real exec, so selection is decided by dep availability."""
from argparse import Namespace

import pytest

from briefcase.cli import __main__ as cli_main
from briefcase.cli.engine import OciJJEngine
from briefcase.cli.grpc_engine import TransportUnavailable


def _args():
    return Namespace(server="http://127.0.0.1:50051", repo="R")


def test_cli_mode_returns_subprocess_engine(monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE", "cli")
    assert isinstance(cli_main.make_engine(_args()), OciJJEngine)


def test_grpc_mode_constructs_grpc_engine(monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE", "grpc")
    created = {}

    class FakeGrpc:
        def __init__(self, server=None, repo=None):
            created["args"] = (server, repo)

    monkeypatch.setattr("briefcase.cli.grpc_engine.GrpcEngine", FakeGrpc)
    cli_main.make_engine(_args())
    assert created["args"] == ("http://127.0.0.1:50051", "R")


def test_grpc_mode_fails_loudly_when_deps_missing(monkeypatch):
    # Explicit grpc mode constructs GrpcEngine regardless; if the [run] deps are absent the default
    # ReflectionTransport raises TransportUnavailable at construction — a loud, explicit failure.
    monkeypatch.setenv("BRIEFCASE_ENGINE", "grpc")

    class Boom:
        def __init__(self, **kw):
            raise TransportUnavailable("no deps")

    monkeypatch.setattr("briefcase.cli.grpc_engine.GrpcEngine", Boom)
    with pytest.raises(TransportUnavailable):
        cli_main.make_engine(_args())


def test_auto_uses_grpc_when_deps_available(monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE", "auto")
    monkeypatch.setattr("briefcase.cli.grpc_engine.grpc_available", lambda: True)
    sentinel = object()
    monkeypatch.setattr("briefcase.cli.grpc_engine.GrpcEngine", lambda **kw: sentinel)
    assert cli_main.make_engine(_args()) is sentinel


def test_auto_uses_cli_when_deps_missing(monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE", "auto")
    monkeypatch.setattr("briefcase.cli.grpc_engine.grpc_available", lambda: False)
    assert isinstance(cli_main.make_engine(_args()), OciJJEngine)
