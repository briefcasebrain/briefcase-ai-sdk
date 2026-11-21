"""Local servers used by the telemetry step.

``Tee`` stands in for the vendor's model API (a canned reply, so no credentials and no model
call are needed) and sits in front of ``briefcase receive`` on the OTLP paths: every telemetry
request the REAL tool sends is saved verbatim and forwarded to the real receiver. Nothing in
the telemetry is invented by the harness; only the model's answer is canned.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import Sandbox, briefcase_cli, free_port, http_get_json, wait_for

OTLP_PATHS = ("/v1/logs", "/v1/metrics", "/v1/traces")

ANTHROPIC_REPLY = {
    "id": "msg_conformance", "type": "message", "role": "assistant", "model": "conformance-stub",
    "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 1},
}


class Tee:
    def __init__(self, receiver_url: str, model_reply: Optional[Dict[str, Any]] = None) -> None:
        self.receiver_url = receiver_url.rstrip("/")
        self.model_reply = model_reply
        self.captured: List[Dict[str, Any]] = []   # {"path", "content_type", "body": bytes}
        self.model_requests = 0
        tee = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                if path in OTLP_PATHS:
                    tee.captured.append({"path": path, "content_type": self.headers.get("content-type", ""),
                                         "content_encoding": self.headers.get("content-encoding", ""),
                                         "body": body})
                    status, data = tee._forward(path, body, self.headers)
                    ctype = "application/json"
                elif tee.model_reply is not None:
                    tee.model_requests += 1
                    status, data, ctype = 200, json.dumps(tee.model_reply).encode(), "application/json"
                else:
                    status, data, ctype = 404, b"{}", "application/json"
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _forward(self, path: str, body: bytes, headers: Any):
        request = urllib.request.Request(self.receiver_url + path, data=body, method="POST")
        for name in ("content-type", "content-encoding"):
            if headers.get(name):
                request.add_header(name, headers[name])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except OSError:
            return 502, b"{}"

    def __enter__(self) -> "Tee":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


class ReceiverProcess:
    """``briefcase receive`` on an ephemeral port, writing records to a JSONL file."""

    def __init__(self, sandbox: Sandbox, env: Dict[str, str]) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.sink = sandbox.root / "records.jsonl"
        self.log = sandbox.root / "receiver.log"
        self._env = env
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> bool:
        argv = briefcase_cli("receive", "--port", str(self.port), "--to", str(self.sink))
        self._logfile = open(self.log, "w")
        self._proc = subprocess.Popen(argv, env=self._env, stdout=self._logfile, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL)
        return wait_for(lambda: self.health() is not None, 20)

    def health(self) -> Optional[Dict[str, Any]]:
        return http_get_json(self.url + "/healthz")

    def records(self) -> List[Dict[str, Any]]:
        try:
            lines = self.sink.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._logfile.close()

    def __enter__(self) -> "ReceiverProcess":
        if not self.start():
            self.stop()
            tail = self.log.read_text()[-500:] if self.log.exists() else ""
            raise RuntimeError(f"briefcase receive did not come up on {self.url}: {tail}")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def save_payloads(captured: List[Dict[str, Any]], directory: Path) -> List[Path]:
    """Write captured OTLP/JSON bodies as ``NN-logs.json`` etc. (JSON bodies only)."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for index, item in enumerate(captured):
        if "json" not in item["content_type"] or item.get("content_encoding"):
            continue
        try:
            data = json.loads(item["body"].decode("utf-8"))
        except ValueError:
            continue
        path = directory / f"{index:02d}-{item['path'].strip('/').split('/')[-1]}.json"
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
