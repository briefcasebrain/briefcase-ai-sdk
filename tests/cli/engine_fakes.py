"""Offline fakes for the engine installer tests: a fake platform (credentials endpoint plus
release-asset host, with GitHub's redirect-to-signed-URL behaviour) and a fake ``docker`` on PATH
that records every call. Nothing here touches the network beyond 127.0.0.1."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIGESTS = {
    "server": "ghcr.io/briefcasebrain/oci-jj-server@sha256:" + "1" * 64,
    "gateway": "ghcr.io/briefcasebrain/oci-jj-gateway@sha256:" + "2" * 64,
    "workers": "ghcr.io/briefcasebrain/oci-jj-workers@sha256:" + "3" * 64,
    "postgres": "ghcr.io/briefcasebrain/oci-jj-postgres@sha256:" + "4" * 64,
    "reconstructor": "ghcr.io/briefcasebrain/oci-jj-reconstructor@sha256:" + "5" * 64,
}
DOWNLOAD_TOKEN = "ghs_short_lived_download_token"
REGISTRY_PASSWORD = "bce_short_lived_registry_password"


def make_asset() -> bytes:
    """A tar.gz holding an executable ``oci-bai`` shell script."""
    buf = io.BytesIO()
    script = b"#!/bin/sh\necho oci-bai fake \"$@\"\n"
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("oci-bai-0.2.0/oci-bai")
        info.size = len(script)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(script))
    return buf.getvalue()


class FakePlatform:
    """``POST /v1/engine/credentials`` plus ``GET /repos/.../assets/1`` (302 to ``/signed/1``)."""

    def __init__(self, target=None):
        self.asset = make_asset()
        self.sha256 = hashlib.sha256(self.asset).hexdigest()
        self.requests = []
        self.target = target or {}
        self.wrong_sha = False
        self.honour_range = True
        platform = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                auth = self.headers.get("Authorization", "")
                platform.requests.append(("POST", self.path, auth, body))
                token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
                if self.path != "/v1/engine/credentials":
                    return self._json(404, {"error": {"code": "not_found", "message": "no route"}})
                if token == "expired":
                    return self._json(401, {"error": {"code": "unauthorized", "message": "Licence token has expired"}})
                if token == "noent":
                    return self._json(403, {"error": {"code": "not_entitled",
                                                      "message": "This account is not entitled to the engine"}})
                if token == "slow":
                    return self._json(429, {"error": {"code": "rate_limited", "message": "slow down"}},
                                      {"Retry-After": "42"})
                if token == "noconfig":
                    return self._json(503, {"error": {"code": "engine_unavailable",
                                                      "message": "ENGINE_GITHUB_* is not configured"}})
                if token != "good":
                    return self._json(401, {"error": {"code": "unauthorized", "message": "Invalid licence token"}})
                base = f"http://127.0.0.1:{platform.port}"
                os_name = platform.target.get("os", body.get("os"))
                arch = platform.target.get("arch", body.get("arch"))
                return self._json(200, {
                    "registry": "ghcr.io",
                    "username": "x-access-token",
                    "password": REGISTRY_PASSWORD,
                    "expires_at": "2026-11-22T13:00:00Z",
                    "release": {
                        "tag": "v0.2.0", "version": "0.2.0", "download_token": DOWNLOAD_TOKEN,
                        "assets": [{"name": "oci-bai-0.2.0-linux-x86_64.tar.gz", "os": os_name, "arch": arch,
                                    "api_url": f"{base}/repos/briefcasebrain/oci-jj/releases/assets/1",
                                    "sha256": "0" * 64 if platform.wrong_sha else platform.sha256}],
                    },
                    "images": dict(DIGESTS),
                    "cosign": None,
                })

            def do_GET(self):
                auth = self.headers.get("Authorization", "")
                platform.requests.append(("GET", self.path, auth, dict(self.headers)))
                if self.path == "/repos/briefcasebrain/oci-jj/releases/assets/1":
                    if auth != f"Bearer {DOWNLOAD_TOKEN}" or self.headers.get("Accept") != "application/octet-stream":
                        return self._json(404, {"message": "Not Found"})
                    self.send_response(302)
                    self.send_header("Location", "/signed/1?X-Amz-Signature=abc")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/signed/1"):
                    if auth:  # a signed URL must not receive the GitHub token
                        return self._json(400, {"message": "Only one auth mechanism allowed"})
                    data = platform.asset
                    start = 0
                    rng = self.headers.get("Range")
                    if rng and platform.honour_range:
                        start = int(rng.split("=")[1].split("-")[0])
                        self.send_response(206)
                    else:
                        self.send_response(200)
                    chunk = data[start:]
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    self.wfile.write(chunk)
                    return
                return self._json(404, {"message": "Not Found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def downloads(self):
        return [r for r in self.requests if r[0] == "GET" and r[1].startswith("/signed/")]


FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_DOCKER_LOG"]
args = sys.argv[1:]
import hashlib
if args[:1] == ["login"]:
    stdin = sys.stdin.read()
elif args[:1] == ["compose"] and "exec" in args:
    stdin = "sha256:" + hashlib.sha256(sys.stdin.buffer.read()).hexdigest()
else:
    stdin = ""
with open(log, "a") as f:
    f.write(json.dumps({"argv": args, "stdin": stdin}) + "\n")
have = os.environ.get("FAKE_DOCKER_HAVE", "")
if args[:1] == ["version"]:
    if os.environ.get("FAKE_DOCKER_DOWN"):
        sys.stderr.write("Cannot connect to the Docker daemon\n"); sys.exit(1)
    print("27.3.1"); sys.exit(0)
if args[:2] == ["image", "inspect"]:
    sys.exit(0 if have == "all" or args[-1] in have.split(",") else 1)
if args[:1] == ["login"]:
    if os.environ.get("FAKE_DOCKER_LOGIN_FAIL"):
        sys.stderr.write("unauthorized: authentication required\n"); sys.exit(1)
    print("Login Succeeded"); sys.exit(0)
if args[:1] == ["pull"]:
    sys.exit(0)
if args[:1] == ["compose"]:
    if "version" in args:
        print("2.29.7"); sys.exit(0)
    if "exec" in args and os.environ.get("FAKE_DOCKER_EXEC_FAIL"):
        sys.stderr.write("service \"minio\" is not running\n"); sys.exit(1)
    if "ps" in args:
        if "--quiet" in args:
            print(os.environ.get("FAKE_DOCKER_RUNNING", "")); sys.exit(0)
        print(json.dumps({"Service": "oci-jj-server", "State": "running", "Health": "healthy"}))
        sys.exit(0)
    sys.exit(0)
sys.exit(0)
"""


def install_fake_docker(bin_dir: Path, log: Path, monkeypatch) -> Path:
    import sys

    path = bin_dir / "docker"
    path.write_text(FAKE_DOCKER.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
    path.chmod(0o755)
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return path


def docker_calls(log: Path):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]
