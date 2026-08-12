"""Tests for ExportMixin._trigger_export, in particular that a synchronous
export cannot hold the caller's event loop hostage."""

import asyncio
import threading
import time

from briefcase._export_mixin import ExportMixin


class _Handler(ExportMixin):
    def __init__(self, exporter, async_capture=False):
        self._exporter = exporter
        self.async_capture = async_capture


class _SlowAsyncExporter:
    """Blocks the helper thread far longer than the export timeout allows."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.started = threading.Event()
        self.records = []

    async def export(self, record):
        self.started.set()
        time.sleep(self.seconds)
        self.records.append(record)
        return True


class _RecordingExporter:
    def __init__(self):
        self.records = []

    async def export(self, record):
        self.records.append(record)
        return True


def test_sync_export_still_completes_a_fast_exporter():
    exporter = _RecordingExporter()
    _Handler(exporter)._trigger_export({"decision_id": "a"})
    assert exporter.records == [{"decision_id": "a"}]


def test_sync_export_completes_under_a_running_loop():
    exporter = _RecordingExporter()

    async def main():
        _Handler(exporter)._trigger_export({"decision_id": "b"})

    asyncio.run(main())
    assert exporter.records == [{"decision_id": "b"}]


def test_slow_export_does_not_hold_the_running_loop():
    exporter = _SlowAsyncExporter(seconds=30)

    async def main():
        started = time.monotonic()
        _Handler(exporter)._trigger_export({"decision_id": "c"})
        return time.monotonic() - started

    elapsed = asyncio.run(main())
    assert exporter.started.is_set()
    # Bounded by SYNC_EXPORT_TIMEOUT_SECONDS rather than by the exporter's 30s.
    assert elapsed < 10.0, f"export blocked the event loop for {elapsed:.1f}s"


def test_export_timeout_is_configurable(monkeypatch):
    monkeypatch.setattr("briefcase._export_mixin.SYNC_EXPORT_TIMEOUT_SECONDS", 0.2)
    exporter = _SlowAsyncExporter(seconds=30)

    async def main():
        started = time.monotonic()
        _Handler(exporter)._trigger_export({"decision_id": "d"})
        return time.monotonic() - started

    assert asyncio.run(main()) < 2.0


def test_timed_out_export_still_completes_in_the_background(monkeypatch):
    """Giving up on the join does not cancel the export. The helper thread runs
    on and delivers the record, so the log must not claim it was dropped."""
    monkeypatch.setattr("briefcase._export_mixin.SYNC_EXPORT_TIMEOUT_SECONDS", 0.2)
    exporter = _SlowAsyncExporter(seconds=1.0)

    async def main():
        _Handler(exporter)._trigger_export({"decision_id": "f"})

    asyncio.run(main())
    assert exporter.records == []

    for _ in range(50):
        if exporter.records:
            break
        time.sleep(0.1)
    assert exporter.records == [{"decision_id": "f"}]


def test_timeout_warning_does_not_claim_the_record_was_dropped(monkeypatch, caplog):
    monkeypatch.setattr("briefcase._export_mixin.SYNC_EXPORT_TIMEOUT_SECONDS", 0.2)

    async def main():
        _Handler(_SlowAsyncExporter(seconds=1.0))._trigger_export({"decision_id": "g"})

    with caplog.at_level("WARNING", logger="briefcase._export_mixin"):
        asyncio.run(main())

    assert caplog.records, "no warning emitted"
    message = caplog.records[-1].getMessage()
    assert "dropped" not in message
    assert "background" in message


def test_export_errors_never_reach_the_caller():
    class _Boom:
        async def export(self, record):
            raise RuntimeError("exporter down")

    _Handler(_Boom())._trigger_export({"decision_id": "e"})  # must not raise
