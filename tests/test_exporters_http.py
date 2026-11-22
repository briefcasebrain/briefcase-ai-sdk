"""Tests for briefcase/exporters/http.py against a local http.server."""

import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from briefcase.exporters import HTTPExporter


class _Collector:
    """Local HTTP endpoint that records requests and replays statuses."""

    def __init__(self):
        self.requests = []
        self.statuses = []  # consumed first; then 200
        self.retry_after = None
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                collector.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": json.loads(body)}
                )
                status = collector.statuses.pop(0) if collector.statuses else 200
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                if collector.retry_after is not None:
                    self.send_header("Retry-After", collector.retry_after)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/decisions"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    def records(self):
        return [r for req in self.requests for r in req["body"]["records"]]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def collector():
    c = _Collector()
    yield c
    c.close()


def _exporter(collector, **kwargs):
    kwargs.setdefault("flush_interval", None)
    kwargs.setdefault("backoff_base", 0.0)
    return HTTPExporter(collector.url, **kwargs)


def _run(coro):
    return asyncio.run(coro)


def test_posts_json_batches_by_size(collector):
    exp = _exporter(collector, batch_size=3)
    for i in range(7):
        assert _run(exp.export({"i": i})) is True
    assert [len(r["body"]["records"]) for r in collector.requests] == [3, 3]
    assert exp.pending == 1
    _run(exp.close())
    assert [r["i"] for r in collector.records()] == list(range(7))
    assert collector.requests[0]["headers"]["Content-Type"] == "application/json"
    assert collector.requests[0]["path"] == "/v1/decisions"
    assert exp.sent_records == 7


def test_bearer_token_and_custom_headers(collector):
    exp = _exporter(collector, token="s3cret", headers={"X-Tenant": "acme"})
    _run(exp.export({"a": 1}))
    _run(exp.flush())
    headers = collector.requests[0]["headers"]
    assert headers["Authorization"] == "Bearer s3cret"
    assert headers["X-Tenant"] == "acme"


def test_flush_interval_sends_without_reaching_batch_size(collector):
    exp = _exporter(collector, batch_size=100, flush_interval=0.05)
    _run(exp.export({"a": 1}))
    deadline = time.monotonic() + 5.0
    while not collector.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    assert collector.records() == [{"a": 1}]
    _run(exp.close())
    assert exp._timer is None


@pytest.mark.parametrize("status", [500, 503, 429])
def test_retries_transient_statuses(collector, status):
    collector.statuses = [status, status]
    exp = _exporter(collector, max_retries=3)
    _run(exp.export({"a": 1}))
    _run(exp.flush())
    assert len(collector.requests) == 3
    assert exp.pending == 0 and exp.sent_records == 1


def test_exhausted_retries_keep_the_batch_and_warn(collector, caplog):
    collector.statuses = [503, 503, 503]
    exp = _exporter(collector, max_retries=1)
    _run(exp.export({"a": 1}))
    with caplog.at_level(logging.WARNING, logger="briefcase.exporters.http"):
        _run(exp.flush())
    assert len(collector.requests) == 2
    assert exp.pending == 1 and exp.failed_batches == 1
    assert any("HTTP 503" in r.getMessage() for r in caplog.records)
    _run(exp.flush())  # third 503
    _run(exp.flush())  # endpoint recovered: delivered exactly once
    assert collector.requests[-1]["body"]["records"] == [{"a": 1}]
    assert exp.pending == 0


def test_client_error_is_not_retried_and_drops_the_batch(collector, caplog):
    collector.statuses = [400]
    exp = _exporter(collector)
    _run(exp.export({"a": 1}))
    with caplog.at_level(logging.WARNING, logger="briefcase.exporters.http"):
        _run(exp.flush())
    assert len(collector.requests) == 1
    assert exp.pending == 0 and exp.dropped_records == 1
    assert any("not retryable" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_warns_once_per_exporter_naming_the_env_var(collector, caplog, status):
    collector.statuses = [status] * 5
    exp = _exporter(collector)
    with caplog.at_level(logging.WARNING, logger="briefcase.exporters.http"):
        for i in range(3):
            _run(exp.export({"i": i}))
            _run(exp.flush())
    assert len(collector.requests) == 3
    assert exp.dropped_records == 3
    auth = [r for r in caplog.records if "BRIEFCASE_EXPORTER_TOKEN" in r.getMessage()]
    assert len(auth) == 1, [r.getMessage() for r in caplog.records]
    message = auth[0].getMessage()
    assert str(status) in message and "dropped" in message
    # one warning in total, not one more per batch from the generic path
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_auth_warning_does_not_leak_the_token(collector, caplog):
    collector.statuses = [401]
    exp = _exporter(collector, token="s3cret-token")
    _run(exp.export({"a": 1}))
    with caplog.at_level(logging.WARNING, logger="briefcase.exporters.http"):
        _run(exp.flush())
    assert "s3cret-token" not in caplog.text


def test_network_error_is_retried_then_kept(caplog):
    # Nothing listens on this port.
    exp = HTTPExporter("http://127.0.0.1:9/x", flush_interval=None,
                       backoff_base=0.0, max_retries=2, timeout=0.5)
    _run(exp.export({"a": 1}))
    with caplog.at_level(logging.WARNING, logger="briefcase.exporters.http"):
        _run(exp.flush())
    assert exp.pending == 1
    assert any("after 3 attempt" in r.getMessage() for r in caplog.records)


def test_backoff_grows_and_honours_retry_after(collector, monkeypatch):
    collector.statuses = [503, 429]
    collector.retry_after = "2"
    exp = _exporter(collector, max_retries=2, backoff_base=0.1, backoff_max=30.0)
    delays = []
    monkeypatch.setattr(exp, "_sleep", lambda s: delays.append(s) or False)
    _run(exp.export({"a": 1}))
    _run(exp.flush())
    assert delays == [2.0, 2.0]  # Retry-After (2 s) exceeds the computed backoff
    collector.retry_after = None
    assert 0.05 <= exp._retry_delay(0, None) <= 0.1
    assert 0.2 <= exp._retry_delay(2, None) <= 0.4
    assert exp._retry_delay(20, None) <= 30.0


def test_redirects_are_not_followed(collector):
    collector.statuses = [302]
    exp = _exporter(collector)
    _run(exp.export({"a": 1}))
    _run(exp.flush())
    assert len(collector.requests) == 1  # treated as a rejection, not followed
    assert exp.dropped_records == 1


def test_buffer_is_bounded(collector):
    collector.statuses = [503] * 100
    exp = _exporter(collector, batch_size=2, max_buffer=4, max_retries=0)
    for i in range(10):
        _run(exp.export({"i": i}))
    assert exp.pending <= 4
    assert exp.dropped_records > 0


def test_https_required_unless_loopback_or_opted_in():
    with pytest.raises(ValueError, match="cleartext"):
        HTTPExporter("http://collector.example.com/x")
    with pytest.raises(ValueError, match="http\\(s\\) URL"):
        HTTPExporter("ftp://collector.example.com/x")
    HTTPExporter("https://collector.example.com/x", flush_interval=None)
    HTTPExporter("http://localhost:8080/x", flush_interval=None)
    HTTPExporter("http://collector.example.com/x", allow_insecure=True, flush_interval=None)


def test_works_end_to_end_with_capture(collector):
    import briefcase
    from briefcase.decorators import capture

    exp = _exporter(collector, batch_size=50)

    @capture(exporter=exp)
    def f(x):
        return x * 2

    for i in range(5):
        f(i)
    assert briefcase.shutdown(timeout=5.0) is True
    records = collector.records()
    assert len(records) == 5
    assert {r["function_name"] for r in records} == {"f"}
