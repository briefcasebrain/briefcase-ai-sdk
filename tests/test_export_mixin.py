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


def test_background_exports_share_one_worker_and_drain():
    from briefcase._export_mixin import wait_for_pending_exports
    from briefcase.exporters import MemoryExporter

    mem = MemoryExporter()
    mixin = _Handler(mem, async_capture=True)
    before = threading.active_count()
    for i in range(20):
        mixin._trigger_export({"n": i})
    assert wait_for_pending_exports(timeout=5.0) is True
    assert [r["n"] for r in mem.records] == list(range(20))
    # One shared worker, not one thread per record.
    assert threading.active_count() <= before + 1


def test_background_export_errors_stay_contained():
    from briefcase._export_mixin import wait_for_pending_exports
    from briefcase.exporters import MemoryExporter

    class Exploding:
        async def export(self, record):
            raise RuntimeError("boom")

    mem = MemoryExporter()
    _Handler(Exploding(), async_capture=True)._trigger_export({"bad": 1})
    _Handler(mem, async_capture=True)._trigger_export({"good": 1})
    assert wait_for_pending_exports(timeout=5.0) is True
    assert mem.records == [{"good": 1}]


# --- bounded queue, failure logging, retry, flush/shutdown ------------------

import logging  # noqa: E402

import pytest  # noqa: E402

import briefcase  # noqa: E402
from briefcase import _export_mixin as em  # noqa: E402


@pytest.fixture
def pipeline():
    """Fresh warning limiters and restored settings around each test."""
    em.wait_for_pending_exports(5.0)
    saved = dict(vars(em._settings))
    em._drop_warning.reset()
    em._failure_warning.reset()
    em.configure_export(retry_backoff_seconds=0.0)
    yield em
    em.wait_for_pending_exports(5.0)
    vars(em._settings).update(saved)
    em.configure_export(max_queue_size=saved["max_queue_size"])
    em._drop_warning.reset()
    em._failure_warning.reset()


class _GatedExporter:
    def __init__(self):
        self.gate = threading.Event()
        self.started = threading.Event()
        self.records = []

    def export(self, record):
        self.started.set()
        self.gate.wait(5.0)
        self.records.append(record)
        return True


def test_full_queue_drops_counts_and_warns_once(pipeline, caplog):
    exporter = _GatedExporter()
    handler = _Handler(exporter, async_capture=True)
    handler._trigger_export({"n": "first"})
    assert exporter.started.wait(2.0)  # worker is now parked inside export()
    pipeline.configure_export(max_queue_size=2)
    before = pipeline.dropped_export_count()
    with caplog.at_level(logging.WARNING, logger="briefcase._export_mixin"):
        for i in range(10):
            handler._trigger_export({"n": i})
    try:
        assert pipeline.dropped_export_count() - before == 8
        assert briefcase.export_stats()["dropped"] == pipeline.dropped_export_count()
        assert briefcase.export_stats()["max_queue_size"] == 2
        drops = [r for r in caplog.records if "Export queue full" in r.getMessage()]
        assert len(drops) == 1  # rate-limited, not one line per record
        assert drops[0].levelno == logging.WARNING
    finally:
        exporter.gate.set()
    assert pipeline.wait_for_pending_exports(5.0)
    assert [r["n"] for r in exporter.records] == ["first", 0, 1]


def test_background_false_return_logs_warning(pipeline, caplog):
    class _Refusing:
        def export(self, record):
            return False

    with caplog.at_level(logging.WARNING, logger="briefcase._export_mixin"):
        _Handler(_Refusing(), async_capture=True)._trigger_export({})
        assert pipeline.wait_for_pending_exports(5.0)
    assert any(
        r.levelno == logging.WARNING and "returned False" in r.getMessage()
        for r in caplog.records
    )


def test_sync_false_return_logs_warning(pipeline, caplog):
    class _Refusing:
        async def export(self, record):
            return False

    with caplog.at_level(logging.WARNING, logger="briefcase._export_mixin"):
        _Handler(_Refusing(), async_capture=False)._trigger_export({})
    assert any("returned False" in r.getMessage() for r in caplog.records)


def test_background_exception_logs_warning_not_debug(pipeline, caplog):
    class _Broken:
        def export(self, record):
            raise RuntimeError("collector down")

    pipeline.configure_export(max_retries=0)
    failed = pipeline.export_stats()["failed"]
    with caplog.at_level(logging.WARNING, logger="briefcase._export_mixin"):
        _Handler(_Broken(), async_capture=True)._trigger_export({})
        assert pipeline.wait_for_pending_exports(5.0)
    assert any(
        r.levelno == logging.WARNING and "failed after 1 attempt" in r.getMessage()
        for r in caplog.records
    )
    assert pipeline.export_stats()["failed"] == failed + 1


def test_background_export_retries_then_succeeds(pipeline):
    class _Flaky:
        def __init__(self):
            self.calls = 0
            self.records = []

        async def export(self, record):
            self.calls += 1
            if self.calls < 3:
                raise ConnectionError("transient")
            self.records.append(record)
            return True

    exporter = _Flaky()
    pipeline.configure_export(max_retries=2)
    _Handler(exporter, async_capture=True)._trigger_export({"id": 1})
    assert pipeline.wait_for_pending_exports(5.0)
    assert exporter.calls == 3
    assert exporter.records == [{"id": 1}]


def test_retries_are_bounded(pipeline):
    class _Broken:
        calls = 0

        def export(self, record):
            type(self).calls += 1
            raise RuntimeError("always")

    pipeline.configure_export(max_retries=1)
    _Handler(_Broken(), async_capture=True)._trigger_export({})
    assert pipeline.wait_for_pending_exports(5.0)
    assert _Broken.calls == 2


def test_hung_async_export_times_out_and_frees_the_worker(pipeline):
    class _Hung:
        async def export(self, record):
            await asyncio.sleep(30)

    pipeline.configure_export(export_timeout_seconds=0.05)
    recorder = _RecordingExporter()
    _Handler(_Hung(), async_capture=True)._trigger_export({})
    _Handler(recorder, async_capture=True)._trigger_export({"after": True})
    assert pipeline.wait_for_pending_exports(5.0)
    assert recorder.records == [{"after": True}]


def test_flush_and_shutdown_drain_then_flush_or_close_exporters(pipeline):
    class _Tracking(_RecordingExporter):
        def __init__(self):
            super().__init__()
            self.flushed = 0
            self.closed = 0

        async def flush(self):
            self.flushed += 1

        async def close(self):
            self.closed += 1

    exporter = _Tracking()
    handler = _Handler(exporter, async_capture=True)
    for i in range(20):
        handler._trigger_export({"i": i})
    assert briefcase.flush(timeout=5.0) is True
    assert len(exporter.records) == 20
    assert exporter.flushed == 1 and exporter.closed == 0
    assert briefcase.shutdown(timeout=5.0) is True
    assert exporter.closed == 1


def test_shutdown_reports_false_when_the_deadline_passes(pipeline):
    exporter = _GatedExporter()
    _Handler(exporter, async_capture=True)._trigger_export({})
    assert exporter.started.wait(2.0)
    try:
        assert briefcase.shutdown(timeout=0.05) is False
    finally:
        exporter.gate.set()


def test_wait_for_pending_exports_is_reexported():
    assert briefcase.wait_for_pending_exports is em.wait_for_pending_exports


def test_atexit_drains_queued_records(tmp_path):
    import subprocess
    import sys
    import textwrap

    out = tmp_path / "out.jsonl"
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(__import__('pathlib').Path(__file__).parent.parent)!r})
        from tests import mock_core  # noqa: F401
        import briefcase

        class Slow:
            def export(self, record):
                time.sleep(0.01)
                with open({str(out)!r}, "a") as fh:
                    fh.write("x\\n")
                return True
            def flush(self): pass
            def close(self): pass

        @briefcase.capture(exporter=Slow())
        def fn(i):
            return i

        for i in range(30):
            fn(i)
    """)
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60)
    assert len(out.read_text().splitlines()) == 30
