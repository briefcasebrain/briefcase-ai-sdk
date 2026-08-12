"""ReflectionTransport channel selection: an https:// endpoint dials TLS, other schemes plaintext.

Needs grpcio (the ``[run]`` extra); if it is absent, importing it here fails and conftest's
``pytest_ignore_collect`` skips the file.
"""
import grpc
import pytest

from briefcase.cli.grpc_engine import ReflectionTransport


def test_https_endpoint_dials_secure_channel(monkeypatch):
    calls = {}

    def fake_secure(endpoint, creds):
        calls["endpoint"] = endpoint
        calls["creds"] = creds
        return "SECURE-CHANNEL"

    monkeypatch.setattr(grpc, "secure_channel", fake_secure)
    monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: "CREDS")
    t = ReflectionTransport("https://api.example.com:443")
    assert t.endpoint == "api.example.com:443"
    assert t._channel_factory(t.endpoint) == "SECURE-CHANNEL"
    assert calls == {"endpoint": "api.example.com:443", "creds": "CREDS"}


def test_http_endpoint_stays_insecure():
    t = ReflectionTransport("http://127.0.0.1:50051")
    assert t._channel_factory is grpc.insecure_channel


def test_explicit_channel_factory_wins_over_scheme():
    sentinel = object()
    t = ReflectionTransport("https://api.example.com:443", channel_factory=lambda ep: sentinel)
    assert t._channel_factory(t.endpoint) is sentinel


def test_cache_key_separates_schemes(monkeypatch):
    """http and https transports for one authority must not share a resolved cache
    entry, or an https caller reuses call objects bound to a plaintext channel."""
    monkeypatch.setattr(grpc, "secure_channel", lambda ep, creds: "SECURE")
    monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: "CREDS")

    plain = ReflectionTransport("http://api.example.com:50051")
    tls = ReflectionTransport("https://api.example.com:50051")
    tls2 = ReflectionTransport("https://api.example.com:50051")

    assert plain._cache_key != tls._cache_key
    assert tls._cache_key == tls2._cache_key


def test_get_resolved_uses_scheme_qualified_key(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(grpc, "secure_channel", lambda ep, creds: Mock(name="secure-channel"))
    monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: "CREDS")
    monkeypatch.setattr(
        grpc, "insecure_channel", lambda ep: (_ for _ in ()).throw(AssertionError("dialed plaintext"))
    )

    t = ReflectionTransport("https://api.example.com:50051")
    sentinel = object()
    monkeypatch.setitem(ReflectionTransport._CACHE, t._cache_key, sentinel)
    monkeypatch.setattr(
        "briefcase.cli.grpc_engine.ReflectionTransport._list_services_raw",
        lambda self: (_ for _ in ()).throw(AssertionError("cache missed")),
    )
    assert t._get_resolved() is sentinel


def test_cpython_reuses_the_id_of_a_freed_factory():
    """The allocation behaviour that made id()-keying dangerous, demonstrated.

    CPython hands the next same-sized allocation the address it just freed, so
    two transports built from short-lived factories reliably see the same
    id(). Any cache keyed on that id serves the first transport's entry, whose
    call objects are bound to a channel that is already closed.
    """
    import gc

    ids = []
    for _ in range(10):
        factory = lambda ep: ep  # noqa: E731
        ids.append(id(factory))
        del factory
        gc.collect()

    # The first allocation may come off a fresh block; every one after it lands
    # on the address the previous iteration released.
    assert len(set(ids)) < len(ids), (
        f"no address reuse across 10 alloc/free cycles: {ids}"
    )


def test_freed_factory_id_does_not_resurrect_a_stale_cache_entry(monkeypatch):
    """The hazard end to end: seed the cache under the key the id()-based
    scheme produced, free that transport, and build a second one whose factory
    lands on the same address. The second transport must not inherit it."""
    import gc
    from unittest.mock import Mock

    endpoint = "http://api.example.com:50051"

    def build():
        return ReflectionTransport(endpoint, channel_factory=lambda ep: Mock(name="chan"))

    first = build()
    stale_key = ("api.example.com:50051", f"custom:{id(first._channel_factory)}")
    monkeypatch.setitem(ReflectionTransport._CACHE, stale_key, object())
    del first
    gc.collect()

    second = build()
    second_key = ("api.example.com:50051", f"custom:{id(second._channel_factory)}")
    if second_key != stale_key:  # pragma: no cover - non-CPython allocators
        pytest.skip("runtime did not reuse the freed factory address")

    monkeypatch.setattr(
        "briefcase.cli.grpc_engine.ReflectionTransport._list_services_raw", lambda self: []
    )
    monkeypatch.setattr(
        "briefcase.cli.grpc_engine.ReflectionTransport._collect_files", lambda self, s: []
    )
    monkeypatch.setattr("briefcase.cli.grpc_engine._build_pool", lambda files: Mock(name="pool"))

    resolved = second._get_resolved()
    assert resolved is not ReflectionTransport._CACHE[stale_key]
    assert second._cache_key is None


def test_custom_factory_transports_opt_out_of_the_shared_cache():
    """A custom channel_factory gets no shared cache key. Keying one by
    id(factory) lets CPython reuse the id after the factory is collected, so a
    later transport would receive call objects bound to a closed channel."""
    from unittest.mock import Mock

    custom = ReflectionTransport(
        "http://api.example.com:50051", channel_factory=lambda ep: Mock(name="chan")
    )
    shared = ReflectionTransport("http://api.example.com:50051")

    assert custom._cache_key is None
    assert shared._cache_key == ("api.example.com:50051", "plain")


def test_custom_factory_resolution_is_not_written_to_the_shared_cache(monkeypatch):
    from unittest.mock import Mock

    before = dict(ReflectionTransport._CACHE)
    t = ReflectionTransport("http://api.example.com:50051", channel_factory=lambda ep: Mock())
    monkeypatch.setattr(
        "briefcase.cli.grpc_engine.ReflectionTransport._list_services_raw", lambda self: []
    )
    monkeypatch.setattr(
        "briefcase.cli.grpc_engine.ReflectionTransport._collect_files", lambda self, s: []
    )
    monkeypatch.setattr("briefcase.cli.grpc_engine._build_pool", lambda files: Mock(name="pool"))

    resolved = t._get_resolved()
    assert resolved is not None
    assert dict(ReflectionTransport._CACHE) == before
