"""The export pipeline in forked workers (gunicorn, Celery prefork,
multiprocessing "fork", os.fork), exercised with real forks.

A fork copies this process's memory but none of its threads. Without the
at-fork hooks in ``briefcase._export_mixin``:

- a lock some other thread held at the moment of the fork stayed locked in
  the child, and the child's first record hung the application;
- records the parent had queued could be exported a second time by the child;
- a multiprocessing child (which leaves through ``os._exit``) lost what it
  had queued last;
- the HTTP exporter's buffered records were sent by parent *and* child, and
  its periodic flush never ran in the child.
"""

import json
import multiprocessing
import os
import signal
import threading
import time
import warnings

import pytest

import briefcase
from briefcase import _export_mixin as em
from briefcase.exporters import JSONLFileExporter

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")

CHILD_TIMEOUT = 15.0


def run_in_fork(child, timeout=CHILD_TIMEOUT):
    """Fork; run ``child()`` in the child and leave with its return value
    (or 1 when it raised); return that exit code, or "timeout"."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # multi-threaded fork
        pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        code = 1
        try:
            code = int(child() or 0)
        finally:
            os._exit(code)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    return "timeout"


def lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def sink(tmp_path):
    path = tmp_path / "records.jsonl"
    exporter = JSONLFileExporter(str(path))
    yield path, exporter
    em.wait_for_pending_exports(5)


def make_capture(exporter):
    @briefcase.capture(exporter=exporter)
    def work(label):
        return label

    return work


def test_records_made_in_a_forked_child_are_exported(sink):
    path, exporter = sink
    work = make_capture(exporter)
    work("parent")
    assert em.wait_for_pending_exports(5)

    def child():
        work("child")
        return 0 if briefcase.flush(5) else 3

    assert run_in_fork(child) == 0
    labels = [r["outputs"]["result"] for r in lines(path)]
    assert labels.count("'child'") == 1 and labels.count("'parent'") == 1


def test_a_lock_held_by_another_thread_at_the_fork_does_not_hang_the_child(sink):
    path, exporter = sink
    work = make_capture(exporter)
    work("warm-up")
    assert em.wait_for_pending_exports(5)
    held, release = threading.Event(), threading.Event()

    def hold_locks():
        with em._worker_state_lock, exporter._lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold_locks, daemon=True)
    holder.start()
    assert held.wait(5)
    try:
        def child():
            work("child")
            return 0 if briefcase.flush(5) else 3

        assert run_in_fork(child) == 0
    finally:
        release.set()
        holder.join(5)
    assert sum(r["outputs"]["result"] == "'child'" for r in lines(path)) == 1


class _GatedExporter:
    """Blocks exports made in the parent until released, so a record is
    still queued when the fork happens."""

    def __init__(self, path, parent_pid):
        self.path = path
        self.parent_pid = parent_pid
        self.gate = threading.Event()

    async def export(self, record):
        if os.getpid() == self.parent_pid:
            self.gate.wait(10)
        with open(self.path, "a") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "result": record["outputs"]["result"]}) + "\n")
        return True


def test_records_queued_in_the_parent_are_not_exported_again_by_the_child(tmp_path, monkeypatch):
    monkeypatch.setattr(em, "FORK_DRAIN_TIMEOUT_SECONDS", 0.05)
    path = tmp_path / "gated.jsonl"
    exporter = _GatedExporter(str(path), os.getpid())
    work = make_capture(exporter)
    work("blocked")   # taken by the worker, which waits on the gate
    work("queued")    # still in the queue at the fork

    def child():
        work("child")
        return 0 if briefcase.flush(5) else 3

    try:
        assert run_in_fork(child) == 0
    finally:
        exporter.gate.set()
    assert em.wait_for_pending_exports(10)
    rows = lines(path)
    results = sorted(row["result"] for row in rows)
    assert results == ["'blocked'", "'child'", "'queued'"], rows
    assert {row["pid"] for row in rows if row["result"] != "'child'"} == {os.getpid()}


def _mp_child(path):  # pragma: no cover - runs in the child
    work = make_capture(JSONLFileExporter(path))
    for index in range(20):
        work(f"mp-{index}")
    # No flush: a multiprocessing child leaves through os._exit, which skips
    # atexit. The pipeline drains through multiprocessing's own exit path.


def test_a_multiprocessing_fork_child_exports_what_it_queued_before_exiting(tmp_path):
    path = tmp_path / "mp.jsonl"
    context = multiprocessing.get_context("fork")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        process = context.Process(target=_mp_child, args=(str(path),))
        process.start()
    process.join(CHILD_TIMEOUT)
    assert process.exitcode == 0
    results = sorted(r["outputs"]["result"] for r in lines(path))
    assert results == sorted(f"'mp-{index}'" for index in range(20))


def test_the_http_exporter_buffer_is_not_sent_twice_and_its_timer_restarts(monkeypatch):
    from briefcase.exporters import http

    sent = []
    monkeypatch.setattr(http, "_post", lambda url, body, headers, timeout: (
        sent.append((os.getpid(), json.loads(body)["records"])) or (200, None)))
    exporter = http.HTTPExporter("http://127.0.0.1:9/records", batch_size=100, flush_interval=0.05)
    exporter._append({"n": "parent-buffered"})
    exporter._ensure_timer()
    exporter._stop.set()  # keep the parent's timer from sending during the test
    em._remember_exporter(exporter)

    def child():
        if exporter._buffer:
            return 4  # the parent's unsent records were copied into the child
        exporter._append({"n": "child"})
        exporter._ensure_timer()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not exporter._buffer:
                return 0  # the child's own timer flushed it
            time.sleep(0.02)
        return 5

    assert run_in_fork(child) == 0
    assert exporter._buffer == [{"n": "parent-buffered"}]


def test_a_provider_call_in_flight_at_the_fork_records_only_in_the_parent(sink):
    from briefcase.integrations.providers._base import ProviderInstrumentor

    path, exporter = sink
    inst = ProviderInstrumentor("openai", exporter=exporter, async_capture=False)
    try:
        call = inst.begin("llm.openai.chat", "openai.chat.completions.create", stream=True)

        def child():
            call.finish({"model": "copied"})  # the child's copy must stay silent
            return 0 if briefcase.flush(5) else 3

        assert run_in_fork(child) == 0
        assert lines(path) == []
        call.finish({"model": "parent"})
        records = lines(path)
        assert [r["model_parameters"]["model_name"] for r in records] == ["parent"]
    finally:
        inst.uninstall()


def test_fork_hooks_are_registered_once():
    assert em._fork_hooks_registered is True
    em._register_fork_hooks()  # idempotent
    assert em._fork_hooks_registered is True
