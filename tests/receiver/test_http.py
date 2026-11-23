"""The HTTP surface: content types, limits, auth, and POST -> MemoryExporter end to end."""

import gzip
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from briefcase.cli import hook_commands
from briefcase.exporters import MemoryExporter
from briefcase.receiver import Receiver, make_server, otlp
from briefcase.receiver.server import gunzip, make_exporter
from tests.receiver import payloads as p

JSON = {"Content-Type": "application/json"}


class Running:
    def __init__(self, **kwargs):
        self.memory = MemoryExporter()
        kwargs.setdefault("async_export", False)
        self.receiver = Receiver(self.memory, **kwargs)
        self.server = make_server(self.receiver, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def post(self, path, payload, headers=None):
        return self.request("POST", path, json.dumps(payload).encode(), dict(JSON, **(headers or {})))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture
def running():
    instances = []

    def start(**kwargs):
        instance = Running(**kwargs)
        instances.append(instance)
        return instance

    yield start
    for instance in instances:
        instance.close()


def test_end_to_end_post_to_memory_exporter(running):
    r = running()
    status, body, headers = r.post("/v1/logs", p.claude_code_logs())
    assert status == 200 and json.loads(body) == {"partialSuccess": {}}
    assert headers["Content-Type"] == "application/json"
    records = r.memory.records
    assert [rec["decision_type"] for rec in records] == [
        "user_prompt", "model_call", "tool_call", "tool_call", "tool_call", "assistant_response",
        "model_call", "model_call"]
    model = records[1]
    assert model["function_name"] == "claude_code.api_request" and model["outputs"]["input_tokens"] == 1200
    blob = json.dumps(list(records))
    for sentinel in p.SENTINELS:
        assert sentinel not in blob

    assert r.post("/v1/traces", p.copilot_traces())[0] == 200
    assert r.post("/v1/metrics", p.claude_code_metrics())[0] == 200  # accepted, dropped by default
    assert len(r.memory.records) == 11

    status, body, _ = r.request("GET", "/healthz")
    health = json.loads(body)
    assert status == 200 and health["service"] == "briefcase-receiver" and health["redaction"] == "on"
    assert health["stats"]["requests"] == 3 and health["stats"]["records"] == 11


def test_join_state_spans_separate_requests(running):
    r = running()
    full = p.claude_code_logs()
    records = full["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    for record in records[2:4]:  # tool_decision, then tool_result in its own export
        single = json.loads(json.dumps(full))
        single["resourceLogs"][0]["scopeLogs"][0]["logRecords"] = [record]
        assert r.post("/v1/logs", single)[0] == 200
    (tool,) = r.memory.records
    assert tool["function_name"] == "claude_code.tool.Bash" and tool["inputs"]["decision"] == "accept"


def test_gzip_and_chunked_bodies(running):
    r = running()
    raw = json.dumps(p.claude_code_logs()).encode()
    status, _, _ = r.request("POST", "/v1/logs", gzip.compress(raw), dict(JSON, **{"Content-Encoding": "gzip"}))
    assert status == 200 and len(r.memory.records) == 8

    connection = http.client.HTTPConnection("127.0.0.1", r.port, timeout=10)
    connection.request("POST", "/v1/logs", body=iter([raw[:100], raw[100:]]),
                       headers=JSON)  # http.client chunk-encodes an iterable body
    assert connection.getresponse().status == 200
    connection.close()
    assert len(r.memory.records) == 16


def test_keep_alive_connection_serves_several_requests(running):
    r = running()
    connection = http.client.HTTPConnection("127.0.0.1", r.port, timeout=10)
    for _ in range(3):
        connection.request("POST", "/v1/logs", json.dumps(p.codex_logs()).encode(), JSON)
        response = connection.getresponse()
        assert response.status == 200 and response.read() == b'{"partialSuccess": {}}'
    connection.close()


def test_unsupported_media_type_is_415(running):
    r = running()
    status, body, _ = r.request("POST", "/v1/logs", b"hello", {"Content-Type": "text/plain"})
    assert status == 415 and "application/json" in json.loads(body)["error"]
    assert r.request("POST", "/v1/hooks/cursor", b"x", {"Content-Type": "application/x-protobuf"})[0] == 415
    assert r.memory.records == []


def test_protobuf_without_the_extra_is_415_with_the_install_hint(running, monkeypatch):
    def unavailable(kind, body):
        raise otlp.ProtobufUnavailable(otlp.PROTOBUF_HINT)

    monkeypatch.setattr(otlp, "decode_protobuf", unavailable)
    r = running()
    status, body, _ = r.request("POST", "/v1/logs", b"\x0a\x00", {"Content-Type": "application/x-protobuf"})
    assert status == 415 and "pip install 'briefcase-ai[receiver]'" in json.loads(body)["error"]


def test_malformed_bodies_are_400(running):
    r = running()
    assert r.request("POST", "/v1/logs", b"{not json", JSON)[0] == 400
    assert r.request("POST", "/v1/logs", b"[1]", JSON)[0] == 400
    assert r.request("POST", "/v1/logs", b"not gzip", dict(JSON, **{"Content-Encoding": "gzip"}))[0] == 400
    assert r.request("POST", "/v1/logs", b"{}", dict(JSON, **{"Content-Encoding": "br"}))[0] == 400
    assert r.request("POST", "/v1/nope", b"{}", JSON)[0] == 404
    assert r.request("GET", "/v1/logs")[0] == 404
    assert r.receiver.stats["rejected"] == 5


def test_oversize_bodies_are_413(running):
    r = running(max_body_bytes=1024)
    assert r.request("POST", "/v1/logs", b"{" + b" " * 2000 + b"}", JSON)[0] == 413
    bomb = gzip.compress(b"{" + b" " * 200_000 + b"}")
    assert len(bomb) < 1024
    assert r.request("POST", "/v1/logs", bomb, dict(JSON, **{"Content-Encoding": "gzip"}))[0] == 413
    connection = http.client.HTTPConnection("127.0.0.1", r.port, timeout=10)
    connection.request("POST", "/v1/logs", body=iter([b"x" * 600, b"y" * 600]),
                       headers=JSON)  # http.client chunk-encodes an iterable body
    assert connection.getresponse().status == 413
    connection.close()
    assert r.memory.records == []


def test_gunzip_respects_the_limit():
    assert gunzip(gzip.compress(b"abc"), 3) == b"abc"
    with pytest.raises(Exception):
        gunzip(gzip.compress(b"abcd"), 3)


def test_bearer_token_is_required_when_configured(running):
    r = running(token="s3cret")
    assert r.post("/v1/logs", p.codex_logs())[0] == 401
    assert r.post("/v1/logs", p.codex_logs(), {"Authorization": "Bearer wrong"})[0] == 401
    assert r.post("/v1/logs", p.codex_logs(), {"Authorization": "s3cret"})[0] == 401
    assert r.post("/v1/hooks/cursor", p.cursor_hooks()[0])[0] == 401
    assert r.memory.records == []
    assert r.post("/v1/logs", p.codex_logs(), {"Authorization": "Bearer s3cret"})[0] == 200
    assert len(r.memory.records) == 5


def test_non_loopback_bind_requires_a_token():
    with pytest.raises(ValueError, match="without a token"):
        make_server(Receiver(MemoryExporter()), "0.0.0.0", 0)
    server = make_server(Receiver(MemoryExporter(), token="t"), "0.0.0.0", 0)
    server.server_close()


def test_protobuf_logs_traces_and_metrics(running):
    pytest.importorskip("opentelemetry.proto.collector.logs.v1.logs_service_pb2")
    from google.protobuf.json_format import ParseDict
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    proto = {"Content-Type": "application/x-protobuf"}
    r = running(emit_metrics=True)

    body = ParseDict(p.claude_code_logs(), ExportLogsServiceRequest()).SerializeToString()
    status, reply, headers = r.request("POST", "/v1/logs", body, proto)
    assert status == 200 and reply == b"" and headers["Content-Type"] == "application/x-protobuf"
    assert [rec["decision_type"] for rec in r.memory.records][:2] == ["user_prompt", "model_call"]
    assert r.memory.records[1]["outputs"]["input_tokens"] == 1200
    r.memory.clear()

    # Protobuf carries ids as bytes; the JSON fixture's hex ids are converted here.
    traces = json.loads(json.dumps(p.copilot_traces()))
    import base64
    for span in traces["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        for key in ("traceId", "spanId", "parentSpanId"):
            if span.get(key):
                span[key] = base64.b64encode(bytes.fromhex(span[key])).decode()
    body = ParseDict(traces, ExportTraceServiceRequest()).SerializeToString()
    assert r.request("POST", "/v1/traces", gzip.compress(body), dict(proto, **{"Content-Encoding": "gzip"}))[0] == 200
    chat = r.memory.records[1]
    assert chat["trace_id"] == "0af7651916cd43dd8448eb211c80319c" and chat["parent_span_id"] == "00f067aa0ba902b7"
    r.memory.clear()

    body = ParseDict(p.claude_code_metrics(), ExportMetricsServiceRequest()).SerializeToString()
    assert r.request("POST", "/v1/metrics", body, proto)[0] == 200
    assert [rec["outputs"]["value"] for rec in r.memory.records] == [1200, 340, 0.0123, 1]

    assert r.request("POST", "/v1/logs", b"\xff\xff\xff\xff", proto)[0] == 400
    blob = json.dumps(list(r.memory.records))
    assert p.EMAIL not in blob


def test_cursor_hook_endpoint(running):
    r = running()
    for payload in p.cursor_hooks():
        assert r.post("/v1/hooks/cursor", payload)[0] == 200
    assert [rec["function_name"] for rec in r.memory.records] == [
        "cursor.session_start", "cursor.user_prompt", "cursor.tool.Shell", "cursor.assistant_response", "cursor.stop"]
    blob = json.dumps(list(r.memory.records))
    for sentinel in p.SENTINELS:
        assert sentinel not in blob


# --- briefcase-ai hook cursor ----------------------------------------------------

def test_hook_scrub_drops_email_and_content_unless_capturing():
    payload = p.cursor_hooks()[1]
    scrubbed = hook_commands.scrub(payload, capture_content=False)
    assert "user_email" not in scrubbed and "prompt" not in scrubbed and "workspace_roots" not in scrubbed
    assert scrubbed["prompt_length"] == len(p.PROMPT) and scrubbed["conversation_id"] == "cur-conv-1"
    assert p.EMAIL not in json.dumps(scrubbed) and "secret-project" not in json.dumps(scrubbed)
    assert hook_commands.scrub(payload, capture_content=True) == payload


def test_hook_forwards_to_the_receiver_without_email(running, capsys):
    r = running(keep_content=True)  # even a keep-content receiver never sees what the hook dropped
    stdin = io.StringIO(json.dumps(p.cursor_hooks()[1]))
    assert hook_commands.run_cursor(["--endpoint", r.endpoint], stdin=stdin) == 0
    (record,) = r.memory.records
    assert record["function_name"] == "cursor.user_prompt" and record["inputs"]["prompt_length"] == len(p.PROMPT)
    assert p.EMAIL not in json.dumps(record) and p.PROMPT not in json.dumps(record)
    assert capsys.readouterr().out == ""

    stdin = io.StringIO(json.dumps(p.cursor_hooks()[1]))
    assert hook_commands.run_cursor(["--endpoint", r.endpoint, "--capture-content"], stdin=stdin) == 0
    assert r.memory.records[1]["inputs"]["user_email"] == p.EMAIL
    assert r.memory.records[1]["inputs"]["prompt"] == p.PROMPT


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("stdin_text, argv", [
    ("{not json", []),
    ("", []),
    ("[1, 2]", []),
    (json.dumps({"hook_event_name": "stop"}), ["--endpoint", "http://127.0.0.1:1"]),  # nothing listening
    (json.dumps({"hook_event_name": "stop"}), ["--no-such-flag", "--timeout", "nan-ish"]),
    (json.dumps({"hook_event_name": "stop"}), ["--endpoint", "not a url"]),
])
def test_hook_fails_open_and_stays_silent(stdin_text, argv, capsys):
    assert hook_commands.run_cursor(argv, stdin=io.StringIO(stdin_text)) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_hook_via_the_cli_entry_point_is_silent_and_exits_zero():
    env = dict(os.environ, PYTHONPATH=os.getcwd())
    done = subprocess.run(
        [sys.executable, "-m", "briefcase.cli", "hook", "cursor", "--endpoint", f"http://127.0.0.1:{_free_port()}",
         "--bogus"], input=json.dumps(p.cursor_hooks()[0]), capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""


def test_hook_times_out_quickly_against_a_stalled_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)  # accepts the connection, never answers
        started = time.monotonic()
        code = hook_commands.run_cursor(
            ["--endpoint", f"http://127.0.0.1:{sock.getsockname()[1]}", "--timeout", "0.3"],
            stdin=io.StringIO(json.dumps({"hook_event_name": "stop"})))
        assert code == 0 and time.monotonic() - started < 3


# --- exporters ----------------------------------------------------------------

def test_make_exporter_shorthands(tmp_path):
    from briefcase.exporters import ConsoleExporter, HTTPExporter, JSONLFileExporter

    assert isinstance(make_exporter(None), ConsoleExporter)
    assert isinstance(make_exporter("console"), ConsoleExporter)
    assert isinstance(make_exporter(str(tmp_path / "r.jsonl")), JSONLFileExporter)
    assert isinstance(make_exporter("https://collector.example.com/v1/decisions"), HTTPExporter)
    with pytest.raises(ValueError):
        make_exporter("somewhere")


def test_records_reach_a_jsonl_file(tmp_path):
    from briefcase.exporters import JSONLFileExporter

    path = tmp_path / "records.jsonl"
    receiver = Receiver(JSONLFileExporter(path), async_export=False)
    status, _, _ = receiver.handle_post("/v1/logs", "application/json; charset=utf-8",
                                        json.dumps(p.claude_code_logs()).encode())
    assert status == 200
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 8 and lines[1]["decision_type"] == "model_call"


def test_close_flushes_half_joined_records():
    memory = MemoryExporter()
    receiver = Receiver(memory, async_export=False)
    payload = p.claude_code_logs()
    scope = payload["resourceLogs"][0]["scopeLogs"][0]
    scope["logRecords"] = scope["logRecords"][2:3]
    receiver.handle_post("/v1/logs", "application/json", json.dumps(payload).encode())
    assert memory.records == []
    receiver.close()
    assert memory.records[0]["status"] == "no_result"


# --- detach / stop / status -----------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="POSIX process management")
def test_detach_status_stop_round_trip(tmp_path, monkeypatch):
    from briefcase.receiver import daemon

    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path / "bc"))
    monkeypatch.setenv("PYTHONPATH", os.getcwd())
    port = _free_port()
    sink = tmp_path / "out.jsonl"
    assert daemon.status() == {"running": False}
    info = daemon.detach(["--port", str(port), "--host", "127.0.0.1", "--to", str(sink)],
                         "127.0.0.1", port, str(sink), None, wait=30)
    try:
        assert info["endpoint"] == f"http://127.0.0.1:{port}"
        state = daemon.status()
        assert state["running"] and state["pid"] == info["pid"] and state["health"]["redaction"] == "on"
        with pytest.raises(RuntimeError, match="already running"):
            daemon.detach([], "127.0.0.1", port, str(sink), None)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("POST", "/v1/logs", json.dumps(p.claude_code_logs()).encode(), JSON)
        assert connection.getresponse().status == 200
        connection.close()
    finally:
        stopped = daemon.stop(wait=15)
    assert stopped and stopped["pid"] == info["pid"]
    assert daemon.status() == {"running": False} and daemon.stop() is None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (sink.exists() and len(sink.read_text().splitlines()) >= 8):
        time.sleep(0.1)
    lines = [json.loads(line) for line in sink.read_text().splitlines()]
    assert [line["decision_type"] for line in lines][:2] == ["user_prompt", "model_call"]
    assert p.EMAIL not in sink.read_text()
    assert "token" not in (tmp_path / "bc" / "receiver" / "receiver.log").read_text().lower()


def test_exporter_token_env_names(monkeypatch, capsys):
    from briefcase.receiver import server

    monkeypatch.setattr(server, "_warned_token_alias", False)
    assert server.exporter_token({}) is None
    assert server.exporter_token({"BRIEFCASE_EXPORTER_TOKEN": "new"}) == "new"
    assert capsys.readouterr().err == ""
    # the deprecated alias still works, warns once, and never prints the value
    assert server.exporter_token({"BRIEFCASE_EXPORT_TOKEN": "old-secret"}) == "old-secret"
    assert server.exporter_token({"BRIEFCASE_EXPORT_TOKEN": "old-secret"}) == "old-secret"
    err = capsys.readouterr().err
    assert err.count("BRIEFCASE_EXPORT_TOKEN is deprecated") == 1 and "old-secret" not in err
    assert server.exporter_token(
        {"BRIEFCASE_EXPORT_TOKEN": "old", "BRIEFCASE_EXPORTER_TOKEN": "new"}) == "new"
