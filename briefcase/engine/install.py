"""``briefcase engine install``: fetch the engine with an account licence token.

The flow, each step idempotent so an interrupted install can simply be re-run:

1. ``POST <platform>/v1/engine/credentials`` with the licence token as the bearer. The
   platform checks the account is entitled and answers with short-lived credentials, the
   release (its ``oci-bai`` assets and their sha256) and the image digests, all read from the
   release manifest that oci-jj's release workflow publishes. See the platform's
   ``ENGINE_API.md`` for the contract.
2. Download the ``oci-bai`` asset for this OS/arch into ``~/.briefcase/engine/<version>/bin``
   (resuming a partial ``.part`` file), check its sha256, and its cosign signature when the
   release carries one.
3. ``docker login --password-stdin`` with the short-lived credential, ``docker pull`` each
   image by digest, ``docker logout`` (always, also on failure). The credential is never
   written anywhere but docker's own credential store, and only until the logout.
4. Write the single-box compose file pinned to those digests, generate per-install secrets
   once, and record the install in ``~/.briefcase/engine/installed.json``.

Everything that talks to the network or runs a program goes through injectable seams
(``opener`` for HTTP, ``runner`` for subprocesses) so the tests run offline against a fake
platform and a fake ``docker``.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from briefcase import engine as _engine
from briefcase.engine import compose as _compose

USER_AGENT = "briefcase-engine-installer"
CREDENTIALS_PATH = "/v1/engine/credentials"


class InstallError(RuntimeError):
    """A failed install step: what happened plus the command or action that fixes it."""

    def __init__(self, message: str, fix: Optional[str] = None, code: int = 1) -> None:
        super().__init__(message)
        self.fix = fix
        self.code = code


# --------------------------------------------------------------------------- host


def host_target() -> Dict[str, str]:
    """``{"os": "linux"|"darwin"|..., "arch": "x86_64"|"aarch64"|...}`` for asset selection."""
    system = sys.platform
    os_name = "linux" if system.startswith("linux") else "darwin" if system == "darwin" else \
        "windows" if system.startswith(("win", "cygwin")) else system
    machine = (_platform.machine() or "").lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return {"os": os_name, "arch": arch}


# --------------------------------------------------------------------------- platform


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8", "replace") or "{}")
    except (ValueError, OSError):
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or "")
    return str(err or "")


def request_credentials(platform_url: str, token: str, version: Optional[str], target: Mapping[str, str],
                        opener: Callable = urllib.request.urlopen, timeout: float = 30.0) -> Dict[str, Any]:
    """Call the platform. Maps its answers onto install errors a user can act on."""
    url = platform_url.rstrip("/") + CREDENTIALS_PATH
    body = {"os": target["os"], "arch": target["arch"]}
    if version:
        body["version"] = version
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _read_error(exc)
        settings = platform_url.rstrip("/") + "/settings/api-keys"
        if exc.code == 401:
            if "expired" in detail.lower():
                raise InstallError(f"the licence token has expired ({detail})",
                                   fix=f"create a new engine licence token at {settings}") from exc
            raise InstallError(f"the platform did not accept the licence token ({detail or 'unauthorized'})",
                               fix=f"check BRIEFCASE_LICENSE_TOKEN, or create a token at {settings}") from exc
        if exc.code == 403:
            raise InstallError(
                "your account isn't entitled to the engine. "
                + (detail or "Contact your Briefcase account owner or Briefcase support to enable it."),
                fix=None) from exc
        if exc.code == 404:
            raise InstallError(f"no engine release matches ({detail or 'not found'})",
                               fix="briefcase engine install   # without --version, for the latest") from exc
        if exc.code == 429:
            wait = exc.headers.get("Retry-After") if exc.headers else None
            raise InstallError(f"too many install attempts; retry in {wait or 'a minute'} seconds",
                               fix=None) from exc
        if exc.code == 503:
            raise InstallError(f"the platform cannot issue engine credentials right now: {detail}",
                               fix=None) from exc
        raise InstallError(f"the platform answered HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise InstallError(f"cannot reach the platform at {platform_url} ({exc.reason})",
                           fix="check --platform-url / BRIEFCASE_PLATFORM_URL") from exc
    _check_credentials(data)
    return data


def _check_credentials(data: Any) -> None:
    if not isinstance(data, dict):
        raise InstallError("the platform's answer is not a JSON object")
    missing = [k for k in ("registry", "username", "password", "expires_at", "release", "images") if k not in data]
    if missing:
        raise InstallError(f"the platform's answer lacks {', '.join(missing)}")
    for name, ref in (data.get("images") or {}).items():
        if not isinstance(ref, str) or "@sha256:" not in ref:
            raise InstallError(f"image {name!r} is not pinned by digest ({ref!r}); refusing to install")
    for asset in (data.get("release") or {}).get("assets") or []:
        if len(str(asset.get("sha256") or "")) != 64:
            raise InstallError(f"asset {asset.get('name')!r} has no sha256; refusing to install")


def pick_asset(release: Mapping[str, Any], target: Mapping[str, str]) -> Dict[str, Any]:
    assets = [a for a in release.get("assets") or [] if a.get("os") == target["os"] and a.get("arch") == target["arch"]]
    if not assets:
        have = sorted({f"{a.get('os')}/{a.get('arch')}" for a in release.get("assets") or []})
        raise InstallError(
            f"engine {release.get('version') or release.get('tag')} has no oci-bai build for "
            f"{target['os']}/{target['arch']} (available: {', '.join(have) or 'none'})",
            fix="run the engine on a supported OS/architecture")
    return assets[0]


# --------------------------------------------------------------------------- download


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _default_fetch(url: str, headers: Mapping[str, str], timeout: float = 120.0):
    """GET without following redirects (the caller follows them, without the token)."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers=dict(headers))
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: Path, sha256: str, token: Optional[str] = None,
             fetch: Callable = _default_fetch) -> Path:
    """Download ``url`` to ``dest``, resuming ``dest.part``; verify sha256. The token is sent
    to the first host only: GitHub answers an asset request with a redirect to a signed URL,
    which must be fetched without it."""
    if dest.exists() and sha256_file(dest) == sha256:
        return dest
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    offset = part.stat().st_size if part.exists() else 0
    headers = {"Accept": "application/octet-stream", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    current = url
    for _ in range(5):
        hdrs = dict(headers)
        if offset:
            hdrs["Range"] = f"bytes={offset}-"
        resp = fetch(current, hdrs)
        status = getattr(resp, "status", None) or getattr(resp, "code", 200)
        if status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                raise InstallError(f"download of {dest.name} redirected without a Location")
            current = urllib.parse.urljoin(current, location)
            headers.pop("Authorization", None)
            continue
        mode = "ab" if (offset and status == 206) else "wb"
        with open(part, mode) as handle:
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                handle.write(block)
        break
    else:
        raise InstallError(f"too many redirects downloading {dest.name}")
    actual = sha256_file(part)
    if actual != sha256:
        part.unlink()
        raise InstallError(f"{dest.name}: sha256 mismatch (expected {sha256}, got {actual}); the partial "
                           "download was removed", fix="briefcase engine install   # retry")
    os.replace(part, dest)
    return dest


def extract_binary(archive: Path, bin_dir: Path, name: str = "oci-bai") -> Path:
    """The ``oci-bai`` executable from a ``.tar.gz`` asset (or the asset itself)."""
    bin_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    target = bin_dir / name
    if archive.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile() and os.path.basename(m.name) == name]
            if not members:
                raise InstallError(f"{archive.name} does not contain {name}")
            source = tar.extractfile(members[0])
            assert source is not None
            with open(target.with_suffix(".tmp"), "wb") as out:
                shutil.copyfileobj(source, out)
    else:
        shutil.copyfile(archive, target.with_suffix(".tmp"))
    os.replace(target.with_suffix(".tmp"), target)
    target.chmod(0o755)
    return target


def verify_signature(path: Path, asset: Mapping[str, Any], cosign_info: Mapping[str, Any], token: Optional[str],
                     work: Path, runner=subprocess.run, fetch: Callable = _default_fetch,
                     which: Callable = shutil.which) -> str:
    """``"verified"`` when a cosign bundle checked out, ``"unsigned"`` when the release has none.

    oci-jj's release workflow does not sign anything yet (read 2026-11-22); ENGINE_API.md on the
    platform lists the steps to add. Once it does, a release that declares a bundle MUST verify:
    a missing ``cosign`` or a failed check stops the install.
    """
    bundle_url = asset.get("bundle_api_url")
    if not bundle_url:
        return "unsigned"
    if which("cosign") is None:
        raise InstallError("this release is signed, but `cosign` is not installed to check the signature",
                           fix="install cosign (https://docs.sigstore.dev/cosign/system_config/installation/)")
    bundle_path = work / (path.name + ".sigstore.json")
    if asset.get("bundle_sha256"):
        bundle = download(bundle_url, bundle_path, asset["bundle_sha256"], token, fetch)
    else:
        bundle = _fetch_unverified(bundle_url, bundle_path, token, fetch)
    argv = ["cosign", "verify-blob", "--bundle", str(bundle)]
    if cosign_info.get("public_key"):
        key_file = work / "cosign.pub"
        key_file.write_text(str(cosign_info["public_key"]), encoding="utf-8")
        argv += ["--key", str(key_file)]
    else:
        identity = cosign_info.get("certificate_identity")
        issuer = cosign_info.get("certificate_oidc_issuer")
        if not identity or not issuer:
            raise InstallError("the release is signed but the platform sent no signer identity to check it against")
        argv += ["--certificate-identity", str(identity), "--certificate-oidc-issuer", str(issuer)]
    result = runner(argv + [str(path)], capture_output=True, text=True)
    if getattr(result, "returncode", 1) != 0:
        raise InstallError(f"signature check failed for {path.name}: "
                           f"{(getattr(result, 'stderr', '') or '').strip()[:300]}")
    return "verified"


def _fetch_unverified(url: str, dest: Path, token: Optional[str], fetch: Callable) -> Path:
    headers = {"Accept": "application/octet-stream", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = fetch(url, headers)
    status = getattr(resp, "status", None) or getattr(resp, "code", 200)
    if status in (301, 302, 303, 307, 308):
        resp = fetch(urllib.parse.urljoin(url, resp.headers.get("Location")), {"User-Agent": USER_AGENT})
    dest.write_bytes(resp.read())
    return dest


# --------------------------------------------------------------------------- docker


class Docker:
    """The ``docker`` calls the installer makes, through an injectable runner."""

    def __init__(self, runner=subprocess.run, binary: str = "docker") -> None:
        self.runner = runner
        self.binary = binary

    def _run(self, argv: List[str], **kw):
        try:
            return self.runner([self.binary, *argv], **kw)
        except FileNotFoundError:
            return None

    def available(self) -> Optional[str]:
        """The server version, or ``None`` when docker or its daemon is missing."""
        res = self._run(["version", "--format", "{{.Server.Version}}"], capture_output=True, text=True)
        if res is None or getattr(res, "returncode", 1) != 0:
            return None
        return (getattr(res, "stdout", "") or "").strip() or "unknown"

    def has_image(self, ref: str) -> bool:
        res = self._run(["image", "inspect", "--format", "{{.Id}}", ref], capture_output=True, text=True)
        return res is not None and getattr(res, "returncode", 1) == 0

    def login(self, registry: str, username: str, password: str) -> None:
        res = self._run(["login", registry, "--username", username, "--password-stdin"],
                        input=password, capture_output=True, text=True)
        if res is None or getattr(res, "returncode", 1) != 0:
            err = (getattr(res, "stderr", "") or "").strip()[:300] if res is not None else "docker not found"
            raise InstallError(f"docker login {registry} failed: {err}",
                               fix="briefcase engine install   # the credential is short-lived; retry")

    def logout(self, registry: str) -> None:
        self._run(["logout", registry], capture_output=True, text=True)

    def pull(self, ref: str) -> None:
        res = self._run(["pull", ref], capture_output=True, text=True)
        if res is None or getattr(res, "returncode", 1) != 0:
            err = (getattr(res, "stderr", "") or "").strip()[:300] if res is not None else "docker not found"
            raise InstallError(f"docker pull {ref} failed: {err}", fix="briefcase engine install   # resumes")

    def remove_image(self, ref: str) -> None:
        self._run(["image", "rm", ref], capture_output=True, text=True)


# --------------------------------------------------------------------------- plan + install


@dataclass
class InstallOptions:
    token: str
    platform_url: str
    version: Optional[str] = None
    tls: bool = False
    dry_run: bool = False
    skip_images: bool = False
    env: Optional[Mapping[str, str]] = None


@dataclass
class Plan:
    version: str
    tag: str
    target: Dict[str, str]
    asset: Dict[str, Any]
    images: Dict[str, str]
    registry: str
    install_dir: Path
    expires_at: str
    signed: bool
    steps: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "tag": self.tag, "target": self.target,
                "asset": {k: self.asset.get(k) for k in ("name", "sha256", "os", "arch")},
                "images": self.images, "registry": self.registry, "install_dir": str(self.install_dir),
                "credential_expires_at": self.expires_at, "signed": self.signed, "steps": self.steps}


def make_plan(creds: Mapping[str, Any], target: Mapping[str, str], env: Optional[Mapping[str, str]] = None,
              skip_images: bool = False) -> Plan:
    release = creds["release"]
    version = str(release.get("version") or release.get("tag") or "").lstrip("v")
    if not version:
        raise InstallError("the platform's answer names no release version")
    asset = pick_asset(release, target)
    install_dir = _engine.engine_dir(env) / version
    images = dict(creds["images"])
    plan = Plan(version=version, tag=str(release.get("tag") or f"v{version}"), target=dict(target),
                asset=dict(asset), images=images, registry=str(creds["registry"]), install_dir=install_dir,
                expires_at=str(creds["expires_at"]), signed=bool(asset.get("bundle_api_url")))
    plan.steps.append(f"download {asset['name']} -> {install_dir / 'bin' / 'oci-bai'} "
                      f"(sha256 {asset['sha256'][:12]}...)")
    plan.steps.append("check its cosign signature" if plan.signed else
                      "check its sha256 only (this release is not signed)")
    if not skip_images:
        plan.steps.append(f"docker login {plan.registry} (short-lived credential, expires {plan.expires_at})")
        plan.steps.extend(f"docker pull {ref}" for ref in images.values())
        plan.steps.append(f"docker logout {plan.registry}")
    plan.steps.append(f"write {install_dir / 'docker-compose.yml'} (images pinned by digest)")
    plan.steps.append(f"generate secrets once in {_engine.secrets_path(env)} (mode 0600)")
    plan.steps.append(f"record the install in {_engine.installed_path(env)}")
    return plan


def save_state(state: Dict[str, Any], env: Optional[Mapping[str, str]] = None) -> None:
    path = _engine.installed_path(env)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def install(opts: InstallOptions, *, creds: Optional[Mapping[str, Any]] = None, runner=subprocess.run,
            opener: Callable = urllib.request.urlopen, fetch: Callable = _default_fetch,
            which: Callable = shutil.which, log: Callable[[str], Any] = print) -> Dict[str, Any]:
    """Run the install; returns the install record. ``creds`` skips the platform call (the CLI
    fetches them first to show the plan)."""
    env = opts.env
    target = host_target()
    docker = Docker(runner)
    if not opts.skip_images and docker.available() is None:
        raise InstallError("Docker is not installed or its daemon is not running",
                           fix="install Docker (https://docs.docker.com/get-docker/), start it, then re-run")
    if creds is None:
        creds = request_credentials(opts.platform_url, opts.token, opts.version, target, opener)
    plan = make_plan(creds, target, env, opts.skip_images)
    install_dir = plan.install_dir
    release = creds["release"]
    token = release.get("download_token") or None

    archive = download(plan.asset["api_url"], install_dir / "download" / plan.asset["name"],
                       plan.asset["sha256"], token, fetch)
    log(f"downloaded {plan.asset['name']} (sha256 ok)")
    signature = verify_signature(archive, plan.asset, creds.get("cosign") or {}, token,
                                 install_dir / "download", runner, fetch, which)
    if signature == "unsigned":
        log("warning: this engine release is not signed; only its sha256 was checked")
    binary = extract_binary(archive, install_dir / "bin")

    pulled: List[str] = []
    if not opts.skip_images:
        missing = [ref for ref in plan.images.values() if not docker.has_image(ref)]
        if missing:
            docker.login(plan.registry, str(creds["username"]), str(creds["password"]))
            try:
                for ref in missing:
                    docker.pull(ref)
                    pulled.append(ref)
                    log(f"pulled {ref}")
            finally:
                docker.logout(plan.registry)
        else:
            log("all images already present")

    _compose.ensure_secrets(_engine.secrets_path(env))
    tls_info: Dict[str, str] = {}
    if opts.tls:
        tls_info = _compose.make_tls(_engine.engine_dir(env) / "tls", runner)
    compose_file = _compose.write(install_dir, plan.images, plan.version, tls=opts.tls)

    record = {
        "version": plan.version,
        "tag": plan.tag,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform_url": opts.platform_url.rstrip("/"),
        "binary": str(binary),
        "asset": {"name": plan.asset["name"], "sha256": plan.asset["sha256"]},
        "signature": signature,
        "images": plan.images,
        "registry": plan.registry,
        "compose": str(compose_file),
        "env_file": str(_engine.secrets_path(env)),
        "grpc_addr": ("https://127.0.0.1:50051" if opts.tls else "http://127.0.0.1:50051"),
        "gateway": _engine.DEFAULT_GATEWAY,
        "tenant": _compose.TENANT,
        "tls": bool(opts.tls),
        "tls_ca": tls_info.get("ca"),
    }
    state = _engine.load_state(env)
    state.setdefault("schema", 1)
    state.setdefault("installs", {})[plan.version] = record
    state["current"] = plan.version
    save_state(state, env)
    return record
