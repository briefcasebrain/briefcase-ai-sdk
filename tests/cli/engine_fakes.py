"""Offline fakes for the engine installer tests.

* :class:`SigningKey`: a throwaway ECDSA P-256 key that signs exactly like
  ``cosign sign-blob --output-signature`` (base64 of a DER ECDSA signature over SHA-256 of the
  file). Uses ``cryptography`` when importable, else the ``openssl`` CLI, so the suite also runs
  where ``cryptography`` is absent.
* :func:`build_release`: a signed release: ``oci-bai`` archive(s), an OCI image layout bundle
  (valid enough for a real ``docker load``), manifest v2 and every ``.sig``.
* :class:`FakePlatform`: ``POST /v1/engine/install`` (install codes are single use),
  ``GET /v1/engine/index``, and presigned-URL style downloads under ``/s3/...``.
* A fake ``docker`` / ``skopeo`` / ``xattr`` / ``cosign`` on PATH that record every call.

Nothing here touches the network beyond 127.0.0.1.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

VERSION = "0.4.0"
IMAGE_NAMES = ("server", "gateway", "workers", "postgres", "reconstructor")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
# Found once at import: the CLI tests run with PATH reduced to a fake bin directory.
OPENSSL = shutil.which("openssl", path=os.pathsep.join([os.environ.get("PATH", ""), "/usr/bin", "/bin"]))


# --------------------------------------------------------------------------- signing


def _have_crypto() -> bool:
    try:
        import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
    except ImportError:
        return False
    return True


class SigningKey:
    def __init__(self, workdir: Path) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        self.workdir = workdir
        if _have_crypto():
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec

            self._private = ec.generate_private_key(ec.SECP256R1())
            self.public_pem = self._private.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        else:
            openssl = OPENSSL
            if openssl is None:
                import pytest

                pytest.skip("needs `cryptography` or `openssl` to make a test signing key")
            self._private = None
            self._key_file = workdir / "private.pem"
            subprocess.run([openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                            "-out", str(self._key_file)], check=True, capture_output=True)
            self.public_pem = subprocess.run([openssl, "ec", "-in", str(self._key_file), "-pubout"],
                                             check=True, capture_output=True).stdout
        der = base64.b64decode(b"".join(self.public_pem.split(b"\n")[1:-2]))
        self.key_id = "sha256:" + hashlib.sha256(der).hexdigest()

    def sign(self, data: bytes) -> bytes:
        """What ``cosign sign-blob --output-signature`` writes: base64(DER ECDSA(SHA-256(data)))."""
        if self._private is not None:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec

            der = self._private.sign(data, ec.ECDSA(hashes.SHA256()))
        else:
            with tempfile.NamedTemporaryFile(dir=self.workdir, delete=False) as handle:
                handle.write(data)
            der = subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(self._key_file), handle.name],
                                 check=True, capture_output=True).stdout
            os.unlink(handle.name)
        return base64.b64encode(der)

    def pin(self, keys_dir: Path) -> Path:
        keys_dir.mkdir(parents=True, exist_ok=True)
        path = keys_dir / f"release-{self.key_id[7:23]}.pub"
        path.write_bytes(self.public_pem)
        return path


# --------------------------------------------------------------------------- artifacts


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tar(members: Dict[str, bytes], gz: bool = False, mode: int = 0o644) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_binary_archive(version: str = VERSION, os_name: str = "linux", arch: str = "x86_64") -> bytes:
    script = f"#!/bin/sh\necho oci-bai {version} {os_name}/{arch} \"$@\"\n".encode()
    return _tar({f"oci-bai-{version}/oci-bai": script}, gz=True, mode=0o755)


def make_oci_bundle(names=IMAGE_NAMES, version: str = VERSION, arch: str = "amd64",
                    index_wrapped=("postgres",), docker_compat: bool = True) -> (bytes, Dict[str, Dict[str, str]]):
    """A minimal but valid OCI image layout tar: one single-file layer per image. ``index_wrapped``
    images are referenced through a one-platform image index (as multi-arch builds are).
    ``docker_compat`` adds the ``manifest.json`` that ``docker save`` (25+) writes next to the
    OCI layout: Docker's classic image store refuses an archive without it (seen with Docker
    29.10 + vfs: "invalid archive: does not contain a manifest.json").
    Returns ``(tar bytes, {name: {"digest", "config", "manifest"}})``."""
    blobs: Dict[str, bytes] = {}
    index_entries = []
    compat = []
    info: Dict[str, Dict[str, str]] = {}

    def blob(data: bytes) -> str:
        digest = "sha256:" + _sha(data)
        blobs[f"blobs/sha256/{digest[7:]}"] = data
        return digest

    for name in names:
        layer = _tar({f"briefcase-engine/{name}.txt": f"{name} {version}\n".encode()})
        layer_digest = blob(layer)
        config = json.dumps({"architecture": arch, "os": "linux",
                             "config": {"Cmd": ["/bin/true"]},
                             "rootfs": {"type": "layers", "diff_ids": [layer_digest]}},
                            sort_keys=True).encode()
        config_digest = blob(config)
        manifest = json.dumps({"schemaVersion": 2, "mediaType": OCI_MANIFEST,
                               "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                          "digest": config_digest, "size": len(config)},
                               "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar",
                                           "digest": layer_digest, "size": len(layer)}]},
                              sort_keys=True).encode()
        manifest_digest = blob(manifest)
        top = {"mediaType": OCI_MANIFEST, "digest": manifest_digest, "size": len(manifest)}
        if name in index_wrapped:
            index = json.dumps({"schemaVersion": 2, "mediaType": OCI_INDEX,
                                "manifests": [dict(top, platform={"os": "linux", "architecture": arch})]},
                               sort_keys=True).encode()
            top = {"mediaType": OCI_INDEX, "digest": blob(index), "size": len(index)}
        # As oci-jj's release tooling names them (scripts/release/engine_release.py `bundle`, 6d1db6a).
        top["annotations"] = {"org.opencontainers.image.ref.name": f"briefcase-engine/{name}:{version}",
                              "io.containerd.image.name": f"docker.io/briefcase-engine/{name}:{version}"}
        index_entries.append(top)
        compat.append({"Config": f"blobs/sha256/{config_digest[7:]}",
                       "RepoTags": [f"briefcase-engine/{name}:{version}"],
                       "Layers": [f"blobs/sha256/{layer_digest[7:]}"]})
        info[name] = {"digest": top["digest"], "config": config_digest, "manifest": manifest_digest}
    members = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
               "index.json": json.dumps({"schemaVersion": 2, "mediaType": OCI_INDEX,
                                         "manifests": index_entries}).encode()}
    if docker_compat:
        members["manifest.json"] = json.dumps(compat).encode()
    members.update(blobs)
    return _tar(members), info


def make_docker_archive(names=IMAGE_NAMES, version: str = VERSION) -> (bytes, Dict[str, str]):
    """A legacy ``docker save`` tarball (manifest.json only). Returns ``(bytes, {name: config id})``."""
    members: Dict[str, bytes] = {}
    entries, ids = [], {}
    for name in names:
        layer = _tar({f"{name}.txt": name.encode()})
        config = json.dumps({"architecture": "amd64", "os": "linux",
                             "rootfs": {"type": "layers", "diff_ids": ["sha256:" + _sha(layer)]}}).encode()
        cfg = _sha(config)
        members[f"{cfg}.json"] = config
        members[f"{_sha(layer)}/layer.tar"] = layer
        entries.append({"Config": f"{cfg}.json", "RepoTags": [f"briefcase-engine/{name}:{version}"],
                        "Layers": [f"{_sha(layer)}/layer.tar"]})
        ids[name] = "sha256:" + cfg
    members["manifest.json"] = json.dumps(entries).encode()
    return _tar(members), ids


class Release:
    """Files keyed by name (bytes), the manifest dict and image info, written with :meth:`write`."""

    def __init__(self, files: Dict[str, bytes], manifest: dict, images: Dict[str, Dict[str, str]]):
        self.files = files
        self.manifest = manifest
        self.images = images

    @property
    def version(self) -> str:
        return self.manifest["version"]

    def write(self, directory: Path, only: Optional[List[str]] = None) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in self.files.items():
            if only is None or name in only:
                (directory / name).write_bytes(data)
        return directory


# oci-jj 6d1db6a releases linux binaries only (darwin cannot be cross-compiled there).
LINUX_TARGETS = (("linux", "x86_64"), ("linux", "aarch64"))


def build_release(key: SigningKey, version: str = VERSION, *, targets=LINUX_TARGETS,
                  bundle_arch: str = "x86_64", schema_version: int = 2, min_sdk: str = "4.0.0",
                  key_id: Optional[str] = None, manifest_signer: Optional[SigningKey] = None,
                  extra_images: Optional[Dict[str, Dict[str, str]]] = None,
                  image_names=IMAGE_NAMES) -> Release:
    files: Dict[str, bytes] = {}
    binaries = []
    for os_name, arch in targets:
        name = f"oci-bai-{version}-{os_name}-{arch}.tar.gz"
        data = make_binary_archive(version, os_name, arch)
        files[name], files[name + ".sig"] = data, key.sign(data)
        binaries.append({"os": os_name, "arch": arch, "file": name, "sha256": _sha(data), "size": len(data),
                         "signature": name + ".sig"})
    oci_arch = {"x86_64": "amd64", "aarch64": "arm64"}[bundle_arch]
    bundle, info = make_oci_bundle(names=image_names, version=version, arch=oci_arch)
    bname = f"engine-images-{version}-{bundle_arch}.tar"
    files[bname], files[bname + ".sig"] = bundle, key.sign(bundle)
    images = {name: {"repository": f"briefcase-engine/{name}", "digest": i["digest"]} for name, i in info.items()}
    images.update(extra_images or {})
    manifest = {
        "schema_version": schema_version,
        "version": version,
        "released_at": "2026-11-23T00:00:00Z",
        "key_id": key_id or key.key_id,
        "min_sdk_version": min_sdk,
        "binaries": binaries,
        "images": images,
        "bundles": [{"arch": bundle_arch, "file": bname, "sha256": _sha(bundle), "size": len(bundle),
                     "signature": bname + ".sig"}],
        "notes": "Test release.\n\nMore notes.",
    }
    raw = json.dumps(manifest, indent=2).encode()
    files["engine-manifest.json"] = raw
    files["engine-manifest.json.sig"] = (manifest_signer or key).sign(raw)
    return Release(files, manifest, info)


def build_index(key: SigningKey, versions: List[str]) -> Dict[str, bytes]:
    """``releases/index.json`` as the release operator publishes it: ONE object, the index text and its signature
    (base64 DER ECDSA P-256 over the SHA-256 of the text's UTF-8 bytes) side by side. No ``index.json.sig``."""
    payload = json.dumps({"schema_version": 1, "key_id": key.key_id, "updated_at": "2026-11-23T00:00:00Z",
                          "versions": [{"version": v, "released_at": "2026-11-23T00:00:00Z", "summary": f"v{v}",
                                        "prerelease": "-" in v, "min_sdk_version": "4.0.0",
                                        "manifest": f"releases/{v}/engine-manifest.json"}
                                       for v in versions]}, indent=2) + "\n"
    envelope = {"schema_version": 2, "key_id": key.key_id, "payload": payload,
                "signature": key.sign(payload.encode("utf-8")).decode("ascii")}
    return {"index.json": json.dumps(envelope, indent=2).encode()}


# --------------------------------------------------------------------------- platform


class FakePlatform:
    """The platform's engine API plus an S3-like presigned file host.

    Install codes: ``bci_good``/``bci_good2``/... are valid once; ``bci_expired``, ``bci_noent``.
    Licence tokens: ``bcl_good``; ``bcl_expired``, ``bcl_noent``, ``bcl_slow``, ``bcl_noconfig``.
    """

    def __init__(self, release: Release, index: Optional[Dict[str, bytes]] = None):
        self.release = release
        self.index = index or {}
        self.requests: List[tuple] = []
        self.used_codes = set()
        self.expired_paths = set()
        self.overrides: Dict[str, bytes] = {}
        self.omit_binary_sig = False
        self.omit_bundle = False
        self.omit_bundle_sig = False
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

            def _err(self, status, code, message, headers=None):
                return self._json(status, {"error": {"code": code, "message": message}}, headers)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                auth = self.headers.get("Authorization", "")
                platform.requests.append(("POST", self.path, auth, body))
                if self.path != "/v1/engine/install":
                    return self._err(404, "not_found", "no route")
                code = body.get("code")
                token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
                if code:
                    if code == "bci_noent":
                        return self._err(403, "not_entitled", "This account is not entitled to the engine")
                    if code == "bci_expired":
                        return self._err(401, "code_expired", "Install code has expired")
                    if code in platform.used_codes:
                        return self._err(401, "code_used", "Install code was already used")
                    if not code.startswith("bci_good"):
                        return self._err(401, "invalid_code", "Unknown install code")
                    platform.used_codes.add(code)
                else:
                    if token == "bcl_expired":
                        return self._err(401, "unauthorized", "Licence token has expired")
                    if token == "bcl_noent":
                        return self._err(403, "not_entitled", "This account is not entitled to the engine")
                    if token == "bcl_slow":
                        return self._err(429, "rate_limited", "slow down", {"Retry-After": "42"})
                    if token == "bcl_noconfig":
                        return self._err(503, "engine_unavailable", "ENGINE_RELEASES_BUCKET is not configured")
                    if token != "bcl_good":
                        return self._err(401, "unauthorized", "Invalid licence token")
                rel = platform.release
                binary = next((b for b in rel.manifest["binaries"]
                               if b["os"] == body.get("os") and b["arch"] == body.get("arch")), None)
                bundle = next((b for b in rel.manifest["bundles"] if b["arch"] == body.get("arch")), None)
                answer = {
                    "version": rel.version,
                    "manifest_url": platform.presigned("engine-manifest.json"),
                    "manifest_sig_url": platform.presigned("engine-manifest.json.sig"),
                }
                if binary:
                    answer["binary"] = {"url": platform.presigned(binary["file"])}
                    if not platform.omit_binary_sig:
                        answer["binary"]["signature_url"] = platform.presigned(binary["signature"])
                if bundle:
                    answer["bundle"] = {"url": platform.presigned(bundle["file"]),
                                        "signature_url": platform.presigned(bundle["signature"]),
                                        "sha256": bundle["sha256"], "size": bundle["size"]}
                    if platform.omit_bundle:
                        del answer["bundle"]
                    elif platform.omit_bundle_sig:
                        del answer["bundle"]["signature_url"]
                return self._json(200, answer)

            def do_GET(self):
                auth = self.headers.get("Authorization", "")
                platform.requests.append(("GET", self.path, auth, dict(self.headers)))
                if self.path == "/v1/engine/index":
                    if auth != "Bearer bcl_good":
                        return self._err(401, "unauthorized", "Invalid licence token")
                    published = platform.overrides.get("index.json") or platform.index.get("index.json") or b"{}"
                    return self._json(200, {"index_url": platform.presigned("index.json"),
                                            "key_id": json.loads(published).get("key_id"),
                                            "urls_expire_at": "2999-01-01T00:00:00Z"})
                if self.path.startswith("/s3/releases/"):
                    if auth:
                        return self._err(400, "InvalidArgument", "Only one auth mechanism allowed")
                    name = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
                    if name in platform.expired_paths:
                        return self._err(403, "AccessDenied", "Request has expired")
                    data = platform.overrides.get(name) or rel_file(platform, name)
                    if data is None:
                        return self._err(404, "NoSuchKey", "The specified key does not exist.")
                    start = 0
                    rng = self.headers.get("Range")
                    if rng:
                        start = int(rng.split("=")[1].split("-")[0])
                        self.send_response(206)
                    else:
                        self.send_response(200)
                    chunk = data[start:]
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    self.wfile.write(chunk)
                    return
                return self._err(404, "not_found", "Not Found")

        def rel_file(p, name):
            return p.release.files.get(name) or p.index.get(name)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def presigned(self, name: str) -> str:
        return (f"{self.url}/s3/releases/{self.release.version}/{name}"
                "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=300&X-Amz-Signature=abc123")

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]

    def downloads(self):
        return [r for r in self.requests if r[0] == "GET" and r[1].startswith("/s3/")]


# --------------------------------------------------------------------------- fake programs


FAKE_TOOLS = r'''#!/usr/bin/env python3
import hashlib, json, os, sys, tarfile
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
pin_context = None
tls_flags = []
while args[:1] and (args[0] == "--tls" or args[0].startswith(("--tlscacert=", "--tlscert=", "--tlskey="))):
    tls_flags.append(args[0]); args = args[1:]  # global flags the SDK puts before the subcommand
if args[:1] == ["--context"]:  # the global flag outranks DOCKER_HOST and DOCKER_CONTEXT
    pin_context, args = args[1], args[2:]
log = os.environ["FAKE_DOCKER_LOG"]
state_path = os.environ["FAKE_DOCKER_STATE"]
try:
    state = json.load(open(state_path))
except (OSError, ValueError):
    state = {"refs": {}, "ids": []}

def save():
    json.dump(state, open(state_path, "w"))

def compose_orphans():
    """COMPOSE_REMOVE_ORPHANS as Compose resolves it: the process environment (which this subprocess really
    inherited, so a filtered variable is absent here) and, only when that is unset, the --env-file."""
    if args[:1] != ["compose"]:
        return None
    if "COMPOSE_REMOVE_ORPHANS" in os.environ:
        return os.environ["COMPOSE_REMOVE_ORPHANS"]
    if "--env-file" in args:
        try:
            for line in open(args[args.index("--env-file") + 1]).read().splitlines():
                if line.startswith("COMPOSE_REMOVE_ORPHANS="):
                    return line.split("=", 1)[1]
        except OSError:
            pass
    return None

def record(stdin=""):
    with open(log, "a") as f:
        f.write(json.dumps({"tool": tool, "argv": args, "stdin": stdin,
                            "token": os.environ.get("OCI_JJ_AUTH_TOKEN"),
                            "context": pin_context or os.environ.get("DOCKER_CONTEXT"),
                            "host": os.environ.get("DOCKER_HOST"),
                            "tls_verify": os.environ.get("DOCKER_TLS_VERIFY"),
                            "tls": os.environ.get("DOCKER_TLS"),
                            "cert_path": os.environ.get("DOCKER_CERT_PATH"),
                            "tls_flags": tls_flags,
                            "orphans": compose_orphans()}) + "\n")

def blob(tar, names, digest):
    return json.loads(tar.extractfile(names["blobs/sha256/" + digest[7:]]).read())

def oci_images(path):
    """[(descriptor, config id)] for the top-level index of an OCI layout tar."""
    out = []
    with tarfile.open(path) as tar:
        names = {m.name.lstrip("./"): m for m in tar.getmembers()}
        if "index.json" not in names:
            entries = json.loads(tar.extractfile(names["manifest.json"]).read())
            return [({"annotations": {"io.containerd.image.name": e["RepoTags"][0]}},
                     "sha256:" + e["Config"].split(".")[0].split("/")[-1]) for e in entries]
        index = json.loads(tar.extractfile(names["index.json"]).read())
        for desc in index["manifests"]:
            doc = blob(tar, names, desc["digest"])
            if "manifests" in doc:
                doc = blob(tar, names, doc["manifests"][0]["digest"])
            out.append((desc, doc["config"]["digest"]))
    return out

def resolve(ref):
    if ref in state["refs"]:
        return state["refs"][ref]
    if ref in state["ids"]:
        return ref
    return None

if tool == "xattr":
    record()
    if args[:1] == ["-p"]:
        sys.exit(0 if os.environ.get("FAKE_QUARANTINE") else 1)
    sys.exit(0)

if tool == "skopeo":
    record()
    src, dst = args[-2], args[-1]
    _, path, ref = src.split(":", 2)
    name = dst.split(":", 1)[1]
    images = oci_images(path)
    if ref.startswith("@"):
        desc, cfg = images[int(ref[1:])]
    else:
        desc, cfg = next((d, c) for d, c in images
                         if (d.get("annotations") or {}).get("org.opencontainers.image.ref.name") == ref)
    state["ids"].append(cfg); state["refs"][name] = cfg; save()
    sys.exit(0)

if tool == "cosign":
    # Implements `cosign verify-blob --key K --signature S [--insecure-ignore-tlog=true] FILE`
    # with openssl, so the fallback path is exercised for real.
    record()
    import base64, subprocess, tempfile
    key = args[args.index("--key") + 1]
    sig = base64.b64decode(open(args[args.index("--signature") + 1], "rb").read())
    with tempfile.NamedTemporaryFile(delete=False) as h:
        h.write(sig)
    res = subprocess.run(["openssl", "dgst", "-sha256", "-verify", key, "-signature", h.name, args[-1]],
                         capture_output=True)
    os.unlink(h.name)
    sys.exit(0 if res.returncode == 0 else 1)

# docker
if args[:1] == ["compose"] and "exec" in args:
    stdin = "sha256:" + hashlib.sha256(sys.stdin.buffer.read()).hexdigest()
else:
    stdin = ""
record(stdin)
for spec in (os.environ.get("FAKE_DOCKER_FAIL") or "").split(","):
    parts = spec.split()
    if parts and args[:1] == parts[:1] and all(p in args for p in parts[1:]):
        sys.stderr.write("simulated failure: " + spec + "\n"); sys.exit(1)
if args[:1] == ["info"]:  # `docker info --format ...`: the daemon's own id, or its clock
    fmt = args[args.index("--format") + 1] if "--format" in args else ""
    daemon_id = os.environ.get("FAKE_DOCKER_ID", "fake-daemon-id")
    print("2026-01-01T00:00:00Z" if "SystemTime" in fmt else daemon_id); sys.exit(0)
if args[:1] == ["events"]:  # a stream of JSON events; FAKE_DOCKER_EVENTS=silent|stall_after_ready shapes delivery
    import time
    mode = os.environ.get("FAKE_DOCKER_EVENTS", "")
    sent, ready = 0, False
    while True:
        try:
            events = json.load(open(state_path)).get("events", [])
        except (OSError, ValueError):
            events = []
        while sent < len(events):
            event, sent = events[sent], sent + 1
            if mode == "silent" or (mode == "stall_after_ready" and ready):
                continue
            print(json.dumps(event)); sys.stdout.flush()
            ready = ready or event["Type"] == "volume"  # the first canary came through
        time.sleep(0.05)
if args[:1] == ["version"]:
    if os.environ.get("FAKE_DOCKER_DOWN"):
        sys.stderr.write("Cannot connect to the Docker daemon\n"); sys.exit(1)
    print("27.3.1"); sys.exit(0)
if args[:2] == ["image", "inspect"]:
    found = resolve(args[-1])
    if found is None:
        sys.stderr.write("Error: No such image: %s\n" % args[-1]); sys.exit(1)
    print(found); sys.exit(0)
if args[:2] == ["image", "rm"]:
    state["refs"].pop(args[-1], None); save(); sys.exit(0)
if args[:1] == ["tag"]:
    found = resolve(args[1])
    if found is None:
        sys.stderr.write("Error: No such image: %s\n" % args[1]); sys.exit(1)
    state["refs"][args[2]] = found; save(); sys.exit(0)
if args[:1] == ["load"]:
    if os.environ.get("FAKE_DOCKER_LOAD_FAIL"):
        sys.stderr.write("unsupported archive format\n"); sys.exit(1)
    for desc, cfg in oci_images(args[-1]):
        image_id = desc["digest"] if os.environ.get("FAKE_DOCKER_CONTAINERD") else cfg
        state["ids"].append(image_id)
        name = (desc.get("annotations") or {}).get("io.containerd.image.name")
        if name:
            state["refs"][name] = image_id
            print("Loaded image: " + name)
        else:
            print("Loaded image ID: " + image_id)
    save(); sys.exit(0)
def containers_now():
    """[service, image id, compose file] of every running container of the project. Seeded from
    FAKE_DOCKER_CONTAINERS (a JSON list of such triples) or, failing that, one server container per image id of
    FAKE_DOCKER_SERVERS; `compose up` starts one container per service of the compose file and `down` stops all."""
    servers_now()
    raw = os.environ.get("FAKE_DOCKER_CONTAINERS")
    if raw and state.get("containers_src") != raw:
        state["containers_src"], state["containers"] = raw, json.loads(raw)
        save()
    if "containers" in state:
        return state["containers"]
    return [["oci-jj-server", i, ""] for i in state["servers"]]


def servers_now():
    """Image ids of the running server container(s). Seeded from FAKE_DOCKER_SERVERS (re-seeded when it changes);
    `compose up` starts the server of the compose file's release (the image id its tag resolves to), and
    `compose down` stops it."""
    raw = os.environ.get("FAKE_DOCKER_SERVERS") or "[]"
    if state.get("servers_src") != raw:
        state["servers_src"], state["servers"] = raw, json.loads(raw)
        save()
    return state["servers"]

if args[:2] == ["context", "show"]:
    # Docker 29: --context, then DOCKER_HOST (which makes `context show` report "default"), then DOCKER_CONTEXT.
    print(pin_context or ("default" if os.environ.get("DOCKER_HOST") else None)
          or os.environ.get("DOCKER_CONTEXT") or os.environ.get("FAKE_DOCKER_CONTEXT", "default")); sys.exit(0)
if args[:2] == ["context", "inspect"]:
    host = os.environ.get("FAKE_DOCKER_HOST", "unix:///var/run/docker.sock")
    if os.environ.get("FAKE_DOCKER_TLSPATH"):  # a context that carries client certificates (or only skips verification)
        host += "\t" + os.environ["FAKE_DOCKER_TLSPATH"] + "\t" + os.environ.get("FAKE_DOCKER_SKIPTLS", "false") \
            + "\t" + ("" if os.environ.get("FAKE_DOCKER_NOCERTS")
                       else os.environ.get("FAKE_DOCKER_TLSFILES", "ca.pem,cert.pem,key.pem") + ",")
    print(host); sys.exit(0)
if args[:1] == ["ps"] and any("label=com.docker.compose.project=" in a for a in args):
    # A row may carry a fourth item, "stopped" (listed only by `ps -a`), and a fifth, its environment list.
    listed = [(i, row) for i, row in enumerate(containers_now())
              if not (len(row) > 3 and row[3] == "gone")
              and ("-a" in args or not (len(row) > 3 and row[3] == "stopped"))]
    for i, _ in listed:
        print("c%d" % i)
    if "-a" in args and os.environ.get("FAKE_DOCKER_ADD_REPLICA") and not state.get("replica_added"):
        # Another Compose client adds a replica of a service right after this listing.
        rows = containers_now()
        extra = list(next(r for r in rows if r[0] == os.environ["FAKE_DOCKER_ADD_REPLICA"]))
        state["containers"] = rows + [extra[:3]]  # a running extra replica
        state["replica_added"] = True
        save()
    sys.exit(0)
def container_env(row):
    """What `docker inspect` shows as the container's environment: the row's own fifth item, else the environment of its
    service in the Compose file it was created from (as Compose copies it)."""
    import re
    if len(row) > 4:
        return row[4]
    path = (row[2] or "").split(",")[0]
    try:
        text = open(path).read()
    except OSError:
        return []
    block = re.search(r"^  %s:\n((?:    .*\n|\n)*)" % re.escape(row[0]), text, re.M)
    env = re.search(r"^    environment:\n((?:      .*\n)*)", block.group(1), re.M) if block else None
    return [m.group(1) + "=" + m.group(2).strip().strip('"') for m in
            re.finditer(r"^      (\w+): (.*)$", env.group(1), re.M)] if env else []

if args[:1] == ["inspect"]:
    # Only what the template asks for is printed, in its order; a field the fake does not know fails the call.
    c = containers_now()[int(args[-1][1:])]
    fmt = args[args.index("--format") + 1] if "--format" in args else ""
    out = []
    for part in fmt.split("\t"):
        if part == "{{.Image}}":
            out.append(c[1])
        elif "com.docker.compose.service" in part:
            out.append(c[0])
        elif "config_files" in part:
            out.append(c[2])
        elif part == "{{json .Config.Env}}":
            out.append(json.dumps(container_env(c)))
        elif "com.docker.compose.oneoff" in part:
            out.append("True" if len(c) > 5 and c[5] else "False")
        else:
            sys.stderr.write("fake docker: unsupported inspect template field %r\n" % part); sys.exit(1)
    print("\t".join(out)); sys.exit(0)
if args[:1] == ["ps"]:
    # `docker ps --filter label=com.docker.compose.service=oci-jj-server ...`: FAKE_DOCKER_PS holds
    # "project<TAB>container" lines, as Docker prints them with the Compose labels in the format.
    rows = [r for r in (os.environ.get("FAKE_DOCKER_PS") or "").split(";") if r]
    if any("com.docker.compose.service=oci-jj-server" in a for a in args):
        for row in rows:
            print(row)
    sys.exit(0)
def volumes_now():
    """The volumes that exist. Seeded from FAKE_DOCKER_VOLUMES ({name: {label: value}}) and re-seeded when that
    variable changes; `docker volume rm` removes from it, so a purge can be checked afterwards."""
    raw = os.environ.get("FAKE_DOCKER_VOLUMES") or "{}"
    if state.get("volumes_src") != raw:
        state["volumes_src"], state["volumes"] = raw, json.loads(raw)
        save()
    return state["volumes"]

if args[:2] == ["volume", "ls"]:
    # filtered by `label=KEY=VALUE`.
    wanted = [a.split("=", 1)[1] for a in args if a.startswith("label=")]
    for name, labels in volumes_now().items():
        if all(w.split("=", 1)[0] in labels and labels[w.split("=", 1)[0]] == w.split("=", 1)[1] for w in wanted):
            print(name)
    sys.exit(0)
def add_event(kind, action, name, project=None, ident=None):
    """An event as Docker 29.8.2 prints it: a volume is `Actor.ID`, a container is the `name` attribute."""
    if kind == "volume":
        actor = {"ID": name, "Attributes": {"driver": "local"}}
    else:
        actor = {"ID": ident or "c-" + name, "Attributes": {"name": name, "com.docker.compose.project": project or ""}}
    state.setdefault("events", []).append({"Type": kind, "Action": action, "Actor": actor})
    save()

if args[:2] == ["volume", "create"]:
    labels = {a.split("=", 1)[0]: a.split("=", 1)[1] for i, a in enumerate(args) if i and args[i - 1] == "--label"}
    named = [a for i, a in enumerate(args[2:], 2) if not a.startswith("--") and args[i - 1] != "--label"]
    name = named[0] if named else hashlib.sha256(os.urandom(8)).hexdigest()  # the daemon assigns an anonymous name
    if os.environ.get("FAKE_DOCKER_CANARY_FOREIGN") and "ai.briefcase.watch" in labels:
        labels = {"ai.briefcase.watch": "someone-else"}  # a volume that is not the one this SDK made
    volumes_now()[name] = labels
    add_event("volume", "create", name); save()
    print(name); sys.exit(0)
if args[:2] == ["volume", "rm"]:
    name = args[-1]
    failing = (os.environ.get("FAKE_DOCKER_VOLUME_RM_FAIL") or "").split(",")
    if name not in volumes_now():
        sys.stderr.write("Error: No such volume: %s\n" % name); sys.exit(1)
    if name in failing or "*" in failing:
        sys.stderr.write("Error: remove %s: volume is in use\n" % name); sys.exit(1)
    del state["volumes"][name]; save()
    add_event("volume", "destroy", name)
    sys.exit(0)
if args[:2] == ["volume", "inspect"]:
    name = args[-1]
    if name not in volumes_now():
        sys.stderr.write("Error: No such volume: %s\n" % name); sys.exit(1)
    import re as _re
    wanted = _re.search(r'index \.Labels "([^"]+)"', " ".join(args))
    print(volumes_now()[name].get(wanted.group(1) if wanted else "ai.briefcase.engine", ""))
    sys.exit(0)
if args[:1] == ["run"]:
    sys.exit(0)
if args[:1] == ["compose"]:
    if "version" in args:
        print("2.29.7"); sys.exit(0)
    if "exec" in args and os.environ.get("FAKE_DOCKER_EXEC_FAIL"):
        sys.stderr.write("service \"minio\" is not running\n"); sys.exit(1)
    if "logs" in args:
        service = args[-1]
        if service != "logs" and not service.startswith("-") and not service.isdigit():
            compose_text = open(args[args.index("-f") + 1]).read() if "-f" in args else ""
            if compose_text and ("\n  %s:\n" % service) not in compose_text:
                sys.stderr.write("no such service: %s\n" % service); sys.exit(1)
    if "up" in args and os.environ.get("FAKE_DOCKER_COMPOSE_UP_FAIL"):
        sys.stderr.write("service failed to start\n"); sys.exit(1)
    if "config" in args and "--services" in args:
        import re as _re
        text = open(args[args.index("-f") + 1]).read().split("\nvolumes:\n")[0]  # the top-level volumes key
        for name in _re.findall(r"^  (\S+):\n", text, _re.M):
            print(name)
        sys.exit(0)
    if "-f" in args and "start" in args:
        servers_now()  # `compose start` only starts what exists: nothing is created or removed
        state["containers"] = [r[:3] for r in containers_now()]
        save()
        sys.exit(0)
    if "-f" in args and "up" in args and "--no-recreate" in args:
        rows, seen, removed = containers_now(), set(), []
        for r in rows:  # Compose removes the replicas a service has beyond one before it looks at --no-recreate
            if r[0] in seen:
                removed.append(r[0])
            seen.add(r[0])
        state["removed"] = state.get("removed", []) + removed
        save()
    if "-f" in args and "up" in args and os.environ.get("FAKE_DOCKER_DESTROY_ON_UP"):
        # Compose's reconcile scaled down a replica another client created while this `up` was running.
        add_event("container", "destroy", os.environ["FAKE_DOCKER_DESTROY_ON_UP"], "briefcase-engine")
    if "-f" in args and ("up" in args or "down" in args):
        import re as _re
        servers_now()
        if "down" in args:
            state["servers"] = []
            state["containers"] = []
        else:
            path = args[args.index("-f") + 1]
            text = open(path).read()
            started = []
            for service, ref in _re.findall(r"^  (\S+):\n(?:    .*\n)*?    image: (\S+)", text, _re.M):
                # Compose creates every service; an image the installer did not load (minio, registry) gets an id
                started.append([service, state["refs"].get(ref) or "sha256:" + hashlib.sha256(ref.encode()).hexdigest(),
                                path])
            for i, row in enumerate(started):  # the daemon announces every container Compose creates
                add_event("container", "create", "briefcase-engine-%s-1" % row[0], "briefcase-engine", "c%d" % i)
            vanish = os.environ.get("FAKE_DOCKER_VANISH_ON_UP")
            if vanish:  # removed, and the destroy event was dropped before it reached the subscriber
                started = [row + ["gone"] if row[0] == vanish else row for row in started]  # keeps the others' ids
            state["containers"] = started
            state["servers"] = [c[1] for c in started if c[0] == "oci-jj-server"]
        save()
    if "ps" in args:
        if "--quiet" in args:
            print(os.environ.get("FAKE_DOCKER_RUNNING", "")); sys.exit(0)
        import re as _re
        text = ""
        if "-f" in args and os.environ.get("FAKE_DOCKER_PS_ALL"):  # one row per service, else just the server
            text = open(args[args.index("-f") + 1]).read().split("\nvolumes:\n")[0]
        for name in _re.findall(r"^  (\S+):\n", text, _re.M) or ["oci-jj-server"]:
            print(json.dumps({"Service": name, "State": "running", "Health": "healthy"}))
        sys.exit(0)
    sys.exit(0)
sys.exit(0)
'''


def install_fake_tools(bin_dir: Path, log: Path, monkeypatch, tools=("docker",)) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = FAKE_TOOLS.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1)
    for tool in tools:
        path = bin_dir / tool
        path.write_text(script)
        path.chmod(0o755)
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(log.with_suffix(".state.json")))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return bin_dir


def calls(log: Path, tool: str = "docker"):
    if not log.exists():
        return []
    return [c for c in (json.loads(line) for line in log.read_text().splitlines()) if c["tool"] == tool]


def docker_state(log: Path) -> dict:
    path = log.with_suffix(".state.json")
    return json.loads(path.read_text()) if path.exists() else {"refs": {}, "ids": []}


def install_fake_docker(bin_dir: Path, log: Path, monkeypatch) -> Path:
    return install_fake_tools(bin_dir, log, monkeypatch, ("docker",)) / "docker"


def docker_calls(log: Path):
    return calls(log, "docker")
