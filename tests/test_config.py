"""Tests for briefcase.config: singleton locking, setup() semantics, and the
config fields consumed by briefcase.events.emit."""

import asyncio
import threading

import pytest

import briefcase
from briefcase.config import BriefcaseConfig, setup
from briefcase.events import emitter as emitter_mod
from briefcase.events.types import BriefcaseEvent


@pytest.fixture(autouse=True)
def _reset():
    BriefcaseConfig.reset()
    emitter_mod._webhook_cache = (None, None)
    yield
    BriefcaseConfig.reset()
    emitter_mod._webhook_cache = (None, None)


def test_get_returns_one_instance_under_thread_contention(monkeypatch):
    created = []
    original_init = BriefcaseConfig.__init__

    def slow_init(self):
        created.append(self)
        threading.Event().wait(0.02)  # widen the race window
        original_init(self)

    monkeypatch.setattr(BriefcaseConfig, "__init__", slow_init)
    barrier = threading.Barrier(16)
    seen = []

    def grab():
        barrier.wait()
        seen.append(BriefcaseConfig.get())

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(created) == 1
    assert len({id(c) for c in seen}) == 1


def test_setup_only_changes_what_is_passed():
    exporter, bus = object(), object()
    setup(exporter=exporter, event_bus=bus)
    setup(router="r")
    config = BriefcaseConfig.get()
    assert config.exporter is exporter
    assert config.event_bus is bus
    assert config.router == "r"


def test_setup_exporter_none_clears_explicitly():
    setup(exporter=object(), webhook_url="https://example.com/hook")
    setup(exporter=None)
    config = BriefcaseConfig.get()
    assert config.exporter is None
    assert config.webhook_url == "https://example.com/hook"

    calls = []

    @briefcase.capture(async_capture=False)
    def fn():
        calls.append(1)
        return 1

    assert fn() == 1 and calls == [1]  # no exporter: capture is a pass-through


def test_guardrail_packs_still_rejected():
    with pytest.raises(ValueError):
        setup(guardrail_packs=["x"])


def _event(event_type="decision.low_confidence"):
    return BriefcaseEvent(event_type=event_type, decision_id="d1")


def test_emit_delivers_to_the_configured_webhook(monkeypatch):
    from briefcase.events import webhook as webhook_mod

    posts = []

    def fake_post(url, body, headers, timeout):
        posts.append((url, headers))
        return 200

    monkeypatch.setattr(webhook_mod, "_post", fake_post)
    setup(webhook_url="https://example.com/hook", webhook_secret="s3",
          events=["decision.low_confidence"])

    asyncio.run(emitter_mod.emit(_event()))
    asyncio.run(emitter_mod.emit(_event("drift.detected")))  # not subscribed

    assert len(posts) == 1
    url, headers = posts[0]
    assert url == "https://example.com/hook"
    assert "X-Briefcase-Signature" in headers  # webhook_secret is used


def test_emit_rebuilds_the_webhook_when_config_changes(monkeypatch):
    from briefcase.events import webhook as webhook_mod

    urls = []
    monkeypatch.setattr(webhook_mod, "_post", lambda url, b, h, t: urls.append(url) or 200)
    setup(webhook_url="https://a.example.com/hook", webhook_secret="s")
    asyncio.run(emitter_mod.emit(_event()))
    setup(webhook_url="https://b.example.com/hook")
    asyncio.run(emitter_mod.emit(_event()))
    setup(webhook_url=None)
    asyncio.run(emitter_mod.emit(_event()))
    assert urls == ["https://a.example.com/hook", "https://b.example.com/hook"]


def test_emit_with_unusable_webhook_url_warns_and_continues(caplog):
    import logging

    received = []

    class Bus:
        def publish(self, event):
            received.append(event)

    setup(webhook_url="http://remote.example.com/hook", event_bus=Bus())
    with caplog.at_level(logging.WARNING, logger="briefcase.events.emitter"):
        asyncio.run(emitter_mod.emit(_event()))
    assert len(received) == 1
    assert any("not usable" in r.getMessage() for r in caplog.records)


def test_emit_low_confidence_reads_dict_records():
    received = []

    class Bus:
        def publish(self, event):
            received.append(event)

    setup(event_bus=Bus())
    asyncio.run(emitter_mod.emit_low_confidence({"decision_id": "dict-1"}, 0.2, 0.85))
    assert received[0].decision_id == "dict-1"
