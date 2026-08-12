"""Tests for Python bindings - async bridge behavior (GIL release, error types)."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest
from briefcase import init_with_config, is_initialized
from briefcase._native import BriefcaseClient, ReplayEngine, SqliteBackend

SERVER_SCRIPT = r"""
import http.server
import json
import sys
import time

DELAY = float(sys.argv[1])
STATUS = int(sys.argv[2])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        time.sleep(DELAY)
        if STATUS == 200:
            body = json.dumps(
                {
                    "valid": True,
                    "client": {
                        "client_id": "test-client",
                        "permissions": ["read"],
                    },
                    "expires_at": "2030-01-01T00:00:00Z",
                }
            ).encode()
        else:
            body = b"unauthorized"
        self.send_response(STATUS)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
"""


def _ensure_runtime():
    if not is_initialized():
        init_with_config(2)


def _start_server(delay: float, status: int):
    proc = subprocess.Popen(
        [sys.executable, "-c", SERVER_SCRIPT, str(delay), str(status)],
        stdout=subprocess.PIPE,
        text=True,
    )
    port = int(proc.stdout.readline())
    return proc, f"http://127.0.0.1:{port}"


class TestGilRelease:
    def test_python_threads_run_during_client_validation(self):
        """Other Python threads must make progress while a native call blocks on I/O."""
        _ensure_runtime()
        proc, url = _start_server(delay=1.0, status=200)
        try:
            counter = {"n": 0}
            stop = threading.Event()

            def tick():
                while not stop.is_set():
                    counter["n"] += 1
                    time.sleep(0.01)

            thread = threading.Thread(target=tick, daemon=True)
            thread.start()
            time.sleep(0.05)

            before = counter["n"]
            client = BriefcaseClient("sk-test", url)
            after = counter["n"]

            stop.set()
            thread.join(timeout=2)

            assert client.client_id == "test-client"
            progress = after - before
            assert progress >= 10, (
                f"counter advanced only {progress} times during a ~1s native "
                "call; the GIL was held across blocking I/O"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestBridgedErrorTypes:
    def test_auth_failure_raises_permission_error(self):
        _ensure_runtime()
        proc, url = _start_server(delay=0.0, status=401)
        try:
            with pytest.raises(PermissionError):
                BriefcaseClient("sk-bad", url)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_unreachable_server_raises_connection_error(self):
        _ensure_runtime()
        with pytest.raises(ConnectionError):
            BriefcaseClient("sk-test", "http://127.0.0.1:9")

    def test_storage_not_found_raises_key_error(self):
        _ensure_runtime()
        backend = SqliteBackend(None)
        with pytest.raises(KeyError):
            backend.load("no-such-snapshot")


class TestReplayBatchFailures:
    def test_replay_batch_raises_first_failure_typed(self):
        """A batch failure raises the same exception type as the single-item
        call (missing snapshot: KeyError), so callers catch one type."""
        _ensure_runtime()
        backend = SqliteBackend(None)
        engine = ReplayEngine(backend)
        with pytest.raises(KeyError, match="no-such-snapshot"):
            engine.replay_batch(["no-such-snapshot"], None, None)

    def test_replay_batch_message_lists_every_failed_item(self):
        _ensure_runtime()
        backend = SqliteBackend(None)
        engine = ReplayEngine(backend)
        with pytest.raises(KeyError, match="2 of 2 batch items failed"):
            engine.replay_batch(["missing-a", "missing-b"], None, None)
