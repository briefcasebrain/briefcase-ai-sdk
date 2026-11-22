"""Records queued before a normal interpreter exit reach their destination.

Each test runs a real child interpreter that configures ``observe()`` to a
local HTTP endpoint and a JSONL file, makes a few ``@capture`` calls and
leaves *without* calling ``flush()`` / ``shutdown()``.

Regression: on Python 3.12+ the exit-time drain ran after
``threading._shutdown``, where the first use of asyncio's default executor
(``asyncio.to_thread`` in the HTTP exporter) raises ``RuntimeError: can't
register atexit after shutdown`` (or "cannot schedule new futures after
interpreter shutdown"); the HTTP exporter's buffered records were dropped.
"""

import json
import os
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

CHILD_TIMEOUT = 60
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Sink:
    def __init__(self):
        self.records = []
        self.auth = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                sink.auth.append(self.headers.get("Authorization"))
                sink.records.extend(json.loads(body)["records"])
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/decisions"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sink():
    s = _Sink()
    yield s
    s.close()


SCRIPT = textwrap.dedent('''
    import sys
    import briefcase
    from briefcase.exporters import JSONLFileExporter

    url, jsonl, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    asynchronous = "sync" not in mode
    briefcase.observe(url)
    to_file = JSONLFileExporter(jsonl)

    @briefcase.capture(async_capture=asynchronous)
    def to_http(n):
        return n

    @briefcase.capture(exporter=to_file, async_capture=asynchronous)
    def to_jsonl(n):
        return n

    for n in range(4):
        to_http(n)
        to_jsonl(n)
    if "exit3" in mode:
        sys.exit(3)
''')


def run_child(sink, tmp_path, mode, extra_env=None):
    script = tmp_path / "child.py"
    script.write_text(SCRIPT)
    jsonl = tmp_path / "out.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BRIEFCASE_", "OTEL_"))}
    env.update(extra_env or {})
    env["PYTHONPATH"] = REPO_ROOT
    proc = subprocess.run(
        [sys.executable, str(script), sink.url, str(jsonl), mode],
        capture_output=True, text=True, timeout=CHILD_TIMEOUT, env=env,
    )
    lines = jsonl.read_text().splitlines() if jsonl.exists() else []
    return proc, [json.loads(line) for line in lines]


@pytest.mark.parametrize("mode,code", [
    ("async", 0),
    ("sync", 0),
    ("async-exit3", 3),
    ("sync-exit3", 3),
])
def test_records_are_flushed_at_a_normal_exit(sink, tmp_path, mode, code):
    proc, file_records = run_child(sink, tmp_path, mode)
    assert proc.returncode == code, proc.stderr
    assert "Traceback" not in proc.stderr and "RuntimeError" not in proc.stderr, proc.stderr
    assert sorted(r["outputs"]["result"] for r in sink.records) == ["0", "1", "2", "3"], proc.stderr
    assert sorted(r["outputs"]["result"] for r in file_records) == ["0", "1", "2", "3"], proc.stderr


def test_the_exit_drain_sends_the_env_token(sink, tmp_path):
    proc, _ = run_child(sink, tmp_path, "async", {"BRIEFCASE_EXPORTER_TOKEN": "tok-exit"})
    assert proc.returncode == 0, proc.stderr
    assert sink.auth and set(sink.auth) == {"Bearer tok-exit"}


def test_the_exit_drain_starts_no_thread_and_registers_no_atexit_late(sink, tmp_path):
    """The hook is registered when observe() configures the pipeline, not
    lazily during shutdown, and flushing at exit creates no thread."""
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent('''
        import atexit, sys, threading
        import briefcase
        from briefcase import _export_mixin as em

        briefcase.observe(sys.argv[1])
        assert em._atexit_registered, "atexit hook must be registered by observe()"

        started = []
        real_start = threading.Thread.start

        def tracking_start(self):
            if em.exiting():
                started.append(self.name)
            return real_start(self)

        threading.Thread.start = tracking_start

        @briefcase.capture
        def f(n):
            return n

        for n in range(3):
            f(n)

        def report():
            # registered first, so it runs last: after the briefcase drain
            print("THREADS_AT_EXIT=" + repr(started))

        atexit.unregister(em._atexit_flush)
        atexit.register(report)
        atexit.register(em._atexit_flush)
    '''))
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)
    proc = subprocess.run([sys.executable, str(script), sink.url], capture_output=True,
                          text=True, timeout=CHILD_TIMEOUT, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "THREADS_AT_EXIT=[]" in proc.stdout, proc.stdout + proc.stderr
    assert len(sink.records) == 3
