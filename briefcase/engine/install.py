"""``briefcase-ai engine install``: get the engine from the gated portal, verify it, install it.

Three ways in, one trust model (:mod:`briefcase.engine.trust`): everything installed is checked
against the signed release manifest, and the manifest against a release key pinned in this SDK.

Online, with a single-use install code from the portal (``--code bci_...``) or a licence token
for automation (``--token bcl_...``):

1. ``POST <platform>/v1/engine/install``. The platform checks the account is entitled, redeems
   the code (single use, 15 minutes) and answers with presigned download URLs (5 minutes) for
   the manifest, its signature, this machine's ``oci-bai`` build and its image bundle.
2. Download the manifest and its signature, verify them (pinned key, schema 2,
   ``min_sdk_version``), then download the binary and check its size, sha256 and, when the
   platform sends its ``.sig`` URL, its own signature.
3. Download the image bundle, check its size, sha256 and signature, then install it exactly as
   the offline path does (below).
4. Write the compose stack, generate per-install secrets once, record the install.

Offline (``--bundle FILE --manifest FILE``): verify the manifest and the bundle locally,
``docker load`` it (``skopeo`` as a fallback), check every loaded image is the one the manifest
pins, tag each as ``localhost/briefcase-engine/<name>:<version>``, write the stack. No network.
Both ways share :func:`_load_images`.

A release without an ``oci-bai`` build for this OS/architecture (macOS builds may be absent) is
still installed: the commands that run ``oci-bai`` then run it from the engine's server image
with ``docker run`` (see :mod:`briefcase.cli.engine`).

Network, subprocesses and ``which`` are injectable seams so the tests run offline against a
fake platform and a fake ``docker``.
"""
from __future__ import annotations

import contextlib
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

from briefcase import engine as _engine
from briefcase.engine import bundle as _bundle
from briefcase.engine import compose as _compose
from briefcase.engine import trust as _trust

USER_AGENT = "briefcase-engine-installer"
DEFAULT_PLATFORM = "https://observe.briefcasebrain.io"
INSTALL_PATH = "/v1/engine/install"
INDEX_PATH = "/v1/engine/index"
LOCAL_PREFIX = "localhost/briefcase-engine"
REQUIRED_IMAGES = ("server", "gateway", "workers", "postgres")
CLIENT_IMAGE = "server"
QUARANTINE = "com.apple.quarantine"


class InstallError(RuntimeError):
    """A failed install step: what happened plus the command or action that fixes it."""

    def __init__(self, message: str, fix: Optional[str] = None, code: int = 1) -> None:
        super().__init__(message)
        self.fix = fix
        self.code = code


def portal(platform_url: str) -> str:
    """The portal page where entitled users get the engine (and others request access)."""
    return platform_url.rstrip("/") + "/engine"


def platform_url(explicit: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    previous = _engine.current_install(env) or {}
    url = explicit or env.get(_engine.ENV_PLATFORM) or previous.get("platform_url") or DEFAULT_PLATFORM
    return url.rstrip("/")


# --------------------------------------------------------------------------- host


def host_target() -> Dict[str, str]:
    """``{"os": "linux"|"darwin"|..., "arch": "x86_64"|"aarch64"|...}`` for binary selection."""
    system = sys.platform
    os_name = "linux" if system.startswith("linux") else "darwin" if system == "darwin" else \
        "windows" if system.startswith(("win", "cygwin")) else system
    machine = (_platform.machine() or "").lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return {"os": os_name, "arch": arch}


# --------------------------------------------------------------------------- platform API


def _read_error(exc: urllib.error.HTTPError) -> Tuple[str, str]:
    try:
        body = json.loads(exc.read().decode("utf-8", "replace") or "{}")
    except (ValueError, OSError):
        return "", ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or ""), str(err.get("message") or "")
    return "", str(err or "")


def _check_url(url: str, what: str) -> str:
    scheme = urllib.parse.urlsplit(str(url)).scheme
    if scheme not in ("https", "http"):
        raise InstallError(f"the platform sent a {what} URL that is not http(s): {str(url)[:80]!r}")
    return str(url)


def request_install(platform: str, *, code: Optional[str] = None, token: Optional[str] = None,
                    version: Optional[str] = None, target: Mapping[str, str],
                    opener: Callable = urllib.request.urlopen, timeout: float = 30.0) -> Dict[str, Any]:
    """``POST /v1/engine/install``. Exactly one of ``code`` (single-use, sent in the body) or
    ``token`` (licence token, sent as the bearer) is given."""
    body: Dict[str, Any] = {"os": target["os"], "arch": target["arch"]}
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    if code:
        body["code"] = code
    else:
        headers["Authorization"] = f"Bearer {token}"
        if version:
            body["version"] = version
    req = urllib.request.Request(platform + INSTALL_PATH, data=json.dumps(body).encode(), method="POST",
                                 headers=headers)
    page = portal(platform)
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_code, detail = _read_error(exc)
        detail_s = f" ({detail})" if detail else ""
        if exc.code == 403 or err_code == "not_entitled":
            raise InstallError(f"your account isn't entitled to the engine{detail_s}",
                               fix=f"Request access at {page}") from exc
        if exc.code in (401, 410) and code:
            reason = {"code_expired": "has expired", "code_used": "was already used"}.get(err_code, "was not accepted")
            raise InstallError(f"the install code {reason}; install codes work once, for 15 minutes",
                               fix=f"Get a new install code at {page}") from exc
        if exc.code == 401:
            settings = page
            if "expired" in (err_code + detail).lower():
                raise InstallError(f"the licence token has expired{detail_s}",
                                   fix=f"create a new engine licence token at {settings}") from exc
            raise InstallError(f"the platform did not accept the licence token{detail_s}",
                               fix=f"check --token / BRIEFCASE_LICENSE_TOKEN, or create one at {settings}") from exc
        if exc.code == 404:
            raise InstallError(f"no engine release matches{detail_s}",
                               fix=f"pick a version at {page}" if code else
                               "briefcase-ai engine install --token ...   # without --version, for the newest") from exc
        if exc.code == 429:
            wait = exc.headers.get("Retry-After") if exc.headers else None
            raise InstallError(f"too many install attempts; retry in {wait or '60'} seconds"
                               + (" (the code was not used)" if code else ""), fix=None) from exc
        if exc.code == 503:
            raise InstallError(f"the platform cannot serve engine downloads right now{detail_s}",
                               fix="ask the platform's operator to finish the engine setup "
                                   "(release storage settings)") from exc
        raise InstallError(f"the platform answered HTTP {exc.code}{detail_s}",
                           fix=f"try again later, or get the engine at {page}") from exc
    except urllib.error.URLError as exc:
        raise InstallError(f"cannot reach the platform at {platform} ({exc.reason})",
                           fix="check --platform-url / BRIEFCASE_PLATFORM_URL and your network") from exc
    try:
        check_response(data)
    except InstallError as exc:
        if code:  # the code was redeemed; a retry with it would be refused
            raise InstallError(str(exc), fix=f"Get a new install code at {page}; if this repeats, contact "
                                             "Briefcase support") from exc
        raise
    return data


def check_response(data: Any) -> None:
    if not isinstance(data, dict):
        raise InstallError("the platform's answer is not a JSON object")
    missing = [k for k in ("version", "manifest_url", "manifest_sig_url") if not data.get(k)]
    if missing:
        raise InstallError(f"the platform's answer lacks {', '.join(missing)}")
    for key in ("manifest_url", "manifest_sig_url"):
        _check_url(data[key], key)
    for key in ("binary", "bundle"):
        part = data.get(key)
        if part is not None:
            if not isinstance(part, dict) or not part.get("url"):
                raise InstallError(f"the platform's answer has a malformed {key!r}")
            _check_url(part["url"], key)
            if part.get("signature_url"):
                _check_url(part["signature_url"], key + " signature")


# --------------------------------------------------------------------------- download


def _open(url: str, headers: Mapping[str, str], timeout: float = 120.0):
    return urllib.request.urlopen(urllib.request.Request(url, headers=dict(headers)), timeout=timeout)


def _http_error(exc: urllib.error.HTTPError, what: str) -> InstallError:
    if exc.code in (401, 403):
        return InstallError(f"the download link for {what} was refused (HTTP {exc.code}); links from the "
                            "platform expire after 5 minutes")
    return InstallError(f"downloading {what} failed (HTTP {exc.code})")


def fetch_bytes(url: str, what: str, limit: int, fetch: Callable = _open) -> bytes:
    """A small file (manifest, signature) into memory. Presigned URLs get no auth header."""
    try:
        resp = fetch(_check_url(url, what), {"User-Agent": USER_AGENT})
        data = resp.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, what) from exc
    except urllib.error.URLError as exc:
        raise InstallError(f"downloading {what} failed ({exc.reason})") from exc
    if len(data) > limit:
        raise InstallError(f"{what} is larger than {limit} bytes; refusing it")
    return data


def download(url: str, dest: Path, entry: Mapping[str, Any], fetch: Callable = _open) -> Path:
    """Download to ``dest`` (resuming ``dest.part``). Reuses a complete file whose size and sha256
    already match ``entry``; the caller verifies the result."""
    if dest.exists() and dest.stat().st_size == entry["size"] and _trust.sha256_file(dest) == entry["sha256"]:
        return dest
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    offset = part.stat().st_size if part.exists() else 0
    if offset >= entry["size"]:
        part.unlink()
        offset = 0
    headers = {"Accept": "application/octet-stream", "User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    try:
        resp = fetch(_check_url(url, dest.name), headers)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, dest.name) from exc
    except urllib.error.URLError as exc:
        raise InstallError(f"downloading {dest.name} failed ({exc.reason})") from exc
    status = getattr(resp, "status", None) or getattr(resp, "code", 200)
    mode = "ab" if (offset and status == 206) else "wb"
    written = offset if mode == "ab" else 0
    with open(part, mode) as handle:
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            written += len(block)
            if written > entry["size"]:
                break
            handle.write(block)
    os.replace(part, dest)
    return dest


def _verified(path: Path, entry: Mapping[str, Any], manifest: _trust.Manifest,
              signature: Optional[bytes], **kw) -> str:
    try:
        return _trust.check_file(path, entry, manifest, signature, **kw)
    except _trust.TrustError:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def extract_binary(archive: Path, bin_dir: Path, name: str = "oci-bai") -> Path:
    """The ``oci-bai`` executable from a verified ``.tar.gz`` (or the file itself)."""
    bin_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    target = bin_dir / name
    tmp = target.with_suffix(".tmp")
    if archive.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile() and os.path.basename(m.name) == name]
            if not members:
                raise InstallError(f"{archive.name} does not contain {name}")
            source = tar.extractfile(members[0])
            assert source is not None
            with open(tmp, "wb") as out:
                shutil.copyfileobj(source, out)
    else:
        shutil.copyfile(archive, tmp)
    os.replace(tmp, target)
    target.chmod(0o755)
    return target


def is_macos() -> bool:
    return sys.platform == "darwin"


def clear_quarantine(path: Path, runner=subprocess.run, which: Callable = shutil.which) -> bool:
    """macOS only, and only for a file that already passed verification: drop
    ``com.apple.quarantine`` so Gatekeeper does not block the unsigned, un-notarized build.
    Files the SDK downloads itself carry no such attribute; a copy made by a browser does."""
    if not is_macos() or which("xattr") is None:
        return False
    probe = runner(["xattr", "-p", QUARANTINE, str(path)], capture_output=True, text=True)
    if getattr(probe, "returncode", 1) != 0:
        return False
    runner(["xattr", "-d", QUARANTINE, str(path)], capture_output=True, text=True)
    return True


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

    @staticmethod
    def _err(res) -> str:
        return (getattr(res, "stderr", "") or "").strip()[:300] if res is not None else "docker not found"

    def available(self) -> Optional[str]:
        """The server version, or ``None`` when docker or its daemon is missing."""
        res = self._run(["version", "--format", "{{.Server.Version}}"], capture_output=True, text=True)
        if res is None or getattr(res, "returncode", 1) != 0:
            return None
        return (getattr(res, "stdout", "") or "").strip() or "unknown"

    def image_id(self, ref: str) -> Optional[str]:
        res = self._run(["image", "inspect", "--format", "{{.Id}}", ref], capture_output=True, text=True)
        if res is None or getattr(res, "returncode", 1) != 0:
            return None
        return (getattr(res, "stdout", "") or "").strip() or None

    def has_image(self, ref: str) -> bool:
        return self.image_id(ref) is not None

    def tag(self, source: str, target: str) -> None:
        res = self._run(["tag", source, target], capture_output=True, text=True)
        if res is None or getattr(res, "returncode", 1) != 0:
            raise InstallError(f"docker tag {source} {target} failed: {self._err(res)}")

    def load(self, path: Path) -> Optional[str]:
        """``None`` on success, else the error text."""
        res = self._run(["load", "-i", str(path)], capture_output=True, text=True)
        if res is None or getattr(res, "returncode", 1) != 0:
            return self._err(res) or "docker load failed"
        return None

    def untag(self, ref: str) -> None:
        """Remove one tag; never forces, so an image under another tag is kept."""
        self._run(["image", "rm", "--no-prune", ref], capture_output=True, text=True)

    def remove_image(self, ref: str) -> None:
        self._run(["image", "rm", ref], capture_output=True, text=True)


# --------------------------------------------------------------------------- options + state


@dataclass
class InstallOptions:
    platform_url: str = DEFAULT_PLATFORM
    code: Optional[str] = None
    token: Optional[str] = None
    version: Optional[str] = None
    bundle: Optional[Path] = None
    manifest: Optional[Path] = None
    signature: Optional[Path] = None
    binary: Optional[Path] = None
    tls: bool = False
    skip_images: bool = False
    env: Optional[Mapping[str, str]] = None
    # An upgrade records the new release but leaves the current one alone until the caller has restarted onto it
    # (see ``set_current``); a plain install is current at once.
    make_current: bool = True

    @property
    def mode(self) -> str:
        return "bundle" if self.bundle else "code" if self.code else "token"


def stable_ref(name: str, version: str) -> str:
    """The local name an engine image is tagged with. ``localhost/`` keeps it from ever
    resolving to Docker Hub; the compose file also sets ``pull_policy: never``."""
    return f"{LOCAL_PREFIX}/{name}:{version}"


def save_state(state: Dict[str, Any], env: Optional[Mapping[str, str]] = None) -> None:
    path = _engine.installed_path(env)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def plan_steps(opts: InstallOptions, manifest: Optional[_trust.Manifest] = None,
               target: Optional[Mapping[str, str]] = None) -> List[str]:
    """What the install will do, for the confirmation prompt and ``--dry-run``."""
    target = target or host_target()
    env = opts.env
    version = manifest.version if manifest else "<version>"
    install_dir = _engine.engine_dir(env) / version
    steps: List[str] = []
    if opts.mode == "bundle":
        steps.append(f"verify {opts.manifest} and {opts.bundle} against the pinned release key (no network)")
    else:
        what = "redeem the install code (single use)" if opts.mode == "code" else "authenticate with the licence token"
        steps.append(f"POST {opts.platform_url}{INSTALL_PATH}: {what}")
        steps.append("download engine-manifest.json + .sig and verify them against the pinned release key")
    binary = None
    if manifest is not None:
        try:
            binary = manifest.binary_for(target["os"], target["arch"])
        except _trust.TrustError:
            binary = None
    if manifest is None or binary is not None:
        name = binary["file"] if binary else "the oci-bai build for " + f"{target['os']}/{target['arch']}"
        steps.append(f"download {name}, check size, sha256 and signature -> {install_dir / 'bin' / 'oci-bai'}")
    else:
        steps.append(f"no oci-bai build for {target['os']}/{target['arch']}: run it from the server image")
    if not opts.skip_images:
        names = sorted(manifest.images) if manifest else ["<each engine image>"]
        if opts.mode != "bundle":
            steps.append("download the image bundle for " + target["arch"] + ", check its size, sha256 and signature")
        steps.append(f"docker load the bundle (skopeo if docker cannot), check every image ({', '.join(names)}) "
                     "is the one the manifest pins")
        steps.append(f"tag the images {LOCAL_PREFIX}/<name>:{version}")
    steps.append(f"write {install_dir / 'docker-compose.yml'}")
    steps.append(f"generate secrets once in {_engine.secrets_path(env)} (mode 0600)")
    steps.append(f"record the install in {_engine.installed_path(env)}")
    return steps


# --------------------------------------------------------------------------- online


@dataclass
class Prepared:
    response: Dict[str, Any]
    manifest: _trust.Manifest
    manifest_sig: bytes
    target: Dict[str, str] = field(default_factory=dict)


def prepare_online(opts: InstallOptions, *, response: Optional[Dict[str, Any]] = None,
                   opener: Callable = urllib.request.urlopen, fetch: Callable = _open,
                   verify_kw: Optional[Dict[str, Any]] = None) -> Prepared:
    """Call the platform (this redeems an install code) unless ``response`` is given, then
    download and verify the manifest."""
    keys = _trust.require_keys()
    target = host_target()
    if response is None:
        response = request_install(opts.platform_url, code=opts.code, token=opts.token, version=opts.version,
                                   target=target, opener=opener)
    raw = fetch_bytes(response["manifest_url"], "engine-manifest.json", _trust.MAX_MANIFEST_BYTES, fetch)
    sig = fetch_bytes(response["manifest_sig_url"], "engine-manifest.json.sig", _trust.MAX_SIGNATURE_BYTES, fetch)
    manifest = _trust.load_manifest(raw, sig, keys, **(verify_kw or {}))
    if manifest.version != str(response["version"]).lstrip("v"):
        raise _trust.TrustError(f"the platform offered engine {response['version']} but the signed manifest "
                                f"is for {manifest.version}", fix="do not install; report this to Briefcase support")
    return Prepared(response=response, manifest=manifest, manifest_sig=sig, target=target)


def complete_online(opts: InstallOptions, prepared: Prepared, **kwargs) -> Dict[str, Any]:
    with engine_lock(opts.env):
        return _complete_online(opts, prepared, **kwargs)


def _complete_online(opts: InstallOptions, prepared: Prepared, *, runner=subprocess.run, fetch: Callable = _open,
                     which: Callable = shutil.which, log: Callable[[str], Any] = print,
                     verify_kw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    env, manifest, response, target = opts.env, prepared.manifest, prepared.response, prepared.target
    verify_kw = verify_kw or {}
    docker = Docker(runner)
    install_dir = _engine.engine_dir(env) / manifest.version
    _save_manifest(install_dir, manifest, prepared.manifest_sig)
    log(f"manifest for engine {manifest.version} verified (key {manifest.key_id})")

    binary_path, binary_info = None, None
    entry = _binary_entry(manifest, target)
    if entry is not None:
        if not response.get("binary"):
            raise InstallError(f"the platform sent no download for {entry['file']}")
        archive = download(response["binary"]["url"], install_dir / "download" / entry["file"], entry, fetch)
        sig = None
        if response["binary"].get("signature_url"):
            sig = fetch_bytes(response["binary"]["signature_url"], entry["signature"],
                              _trust.MAX_SIGNATURE_BYTES, fetch)
        how = _verified(archive, entry, manifest, sig, **verify_kw)
        binary_path, binary_info = _install_binary(archive, install_dir, entry, how, runner, which, log)

    images: Dict[str, str] = {}
    ids: Dict[str, str] = {}
    method = None
    if not opts.skip_images:
        entry = _bundle_entry(manifest, target)
        part = response.get("bundle")
        if not part:
            raise InstallError(f"the platform sent no image bundle download for {target['arch']}")
        for key in ("sha256", "size"):
            if part.get(key) is not None and part[key] != entry[key]:
                raise _trust.TrustError(f"the platform's bundle {key} differs from the signed manifest's",
                                        fix="do not install; report this to Briefcase support")
        archive = download(part["url"], install_dir / "download" / entry["file"], entry, fetch)
        if not part.get("signature_url"):
            raise InstallError("the platform sent no signature for the image bundle; a release always has one, "
                               "so this answer is incomplete", fix="try again, or contact Briefcase support")
        sig = fetch_bytes(part["signature_url"], entry["signature"], _trust.MAX_SIGNATURE_BYTES, fetch)
        _verified(archive, entry, manifest, sig, **verify_kw)
        images, ids, method = _load_images(manifest, archive, docker, target, runner, which, log)
    return _finish(opts, manifest, install_dir, binary_path, binary_info, images, ids,
                   {"load_method": method}, runner, log, target)


# --------------------------------------------------------------------------- offline


def verify_offline(opts: InstallOptions, verify_kw: Optional[Dict[str, Any]] = None
                   ) -> Tuple[_trust.Manifest, bytes, Dict[str, Any], str]:
    """Verify the manifest and the bundle (no network): ``(manifest, sig, bundle entry, how)``."""
    verify_kw = verify_kw or {}
    keys = _trust.require_keys()
    assert opts.manifest is not None and opts.bundle is not None
    manifest_path = Path(opts.manifest)
    bundle_path = Path(opts.bundle)
    sig_path = Path(opts.signature) if opts.signature else manifest_path.with_name(manifest_path.name + ".sig")
    for path, what in ((manifest_path, "manifest"), (sig_path, "manifest signature"), (bundle_path, "bundle")):
        if not path.is_file():
            raise InstallError(f"{what} {path} does not exist",
                               fix="download it from the portal next to the others"
                                   + (" (or pass --signature FILE)" if what == "manifest signature" else ""),
                               code=2)
    manifest = _trust.load_manifest(manifest_path.read_bytes(), sig_path.read_bytes(), keys, **verify_kw)
    entry = _trust.manifest_entry_for(manifest, bundle_path)
    if entry is None or entry not in manifest.data["bundles"]:
        raise _trust.TrustError(f"{bundle_path.name} is not a bundle of engine {manifest.version} "
                                "(neither its name nor its sha256 is in the signed manifest)",
                                fix="download the bundle and the manifest of the same version from the portal")
    target = host_target()
    if entry["arch"] != target["arch"]:
        raise InstallError(f"{bundle_path.name} is for {entry['arch']}; this machine is {target['arch']}",
                           fix=f"download the {target['arch']} bundle from the portal")
    bsig = _find_sig(bundle_path, entry)
    how = _trust.check_file(bundle_path, entry, manifest, bsig, **verify_kw)
    return manifest, sig_path.read_bytes(), entry, how


def offline_install(opts: InstallOptions, *, runner=subprocess.run, which: Callable = shutil.which,
                    log: Callable[[str], Any] = print, verified=None,
                    verify_kw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    verify_kw = verify_kw or {}
    env = opts.env
    target = host_target()
    docker = Docker(runner)
    assert opts.manifest is not None and opts.bundle is not None
    manifest_path, bundle_path = Path(opts.manifest), Path(opts.bundle)
    if docker.available() is None:
        raise InstallError("Docker is not installed or its daemon is not running",
                           fix="install Docker (https://docs.docker.com/get-docker/), start it, then re-run")
    manifest, sig, entry, _how = verified or verify_offline(opts, verify_kw)
    install_dir = _engine.engine_dir(env) / manifest.version
    with engine_lock(env):
        _save_manifest(install_dir, manifest, sig)
        # Files are copied into a directory made for this run alone (0700) and verified there, so what
        # Docker loads is exactly what was checked even if the originals change or another install of
        # the same release runs at the same time. The copies are removed when this run ends.
        stage_dir = Path(tempfile.mkdtemp(prefix="stage-", dir=_private_dir(install_dir / "download")))
        try:
            staged_bundle, how = _stage_verified(bundle_path, stage_dir, entry, manifest,
                                                 _find_sig(bundle_path, entry), **verify_kw)
            log(f"manifest for engine {manifest.version} verified (key {manifest.key_id}); bundle "
                + ("signature verified" if how == "signature"
                   else "sha256 matches the signed manifest (no .sig next to it)"))
            binary_path, binary_info = None, None
            bentry = _binary_entry(manifest, target)
            if bentry is not None:
                # Next to the manifest or next to the original bundle, whichever has it.
                candidates = [Path(opts.binary)] if opts.binary else \
                    [manifest_path.with_name(bentry["file"]), bundle_path.with_name(bentry["file"])]
                found = next((p for p in candidates if p.is_file()), None)
                if found is None and opts.binary:
                    raise InstallError(f"{opts.binary} does not exist", code=2)
                if found is not None:
                    staged, bhow = _stage_verified(found, stage_dir, bentry, manifest, _find_sig(found, bentry),
                                                   **verify_kw)
                    binary_path, binary_info = _install_binary(staged, install_dir, bentry, bhow, runner, which,
                                                               log)
                else:
                    log(f"{bentry['file']} is not next to the manifest or the bundle; pass --binary FILE to "
                        "install the client binary")
            images, ids, method = _load_images(manifest, staged_bundle, docker, target, runner, which, log)
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)
        return _finish(opts, manifest, install_dir, binary_path, binary_info, images, ids,
                       {"load_method": method}, runner, log, target)


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


@contextlib.contextmanager
def engine_lock(env: Optional[Mapping[str, str]] = None):
    """One install at a time under the engine home, whatever the version or mode: held from before the
    manifest is written until installed.json and the secrets file are updated (``fcntl`` on POSIX,
    ``msvcrt`` on Windows)."""
    home = _engine.engine_dir(env)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(home / ".install.lock", "a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        else:  # Windows
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # waits up to 10 s, so loop
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _stage_verified(src: Path, stage_dir: Path, entry: Mapping[str, Any], manifest: _trust.Manifest,
                    signature: Optional[bytes], **verify_kw) -> Tuple[Path, str]:
    """Copy ``src`` into the private ``stage_dir`` and verify the copy: ``(copy, how)``. Refuses before copying
    when the disk cannot hold it, and never leaves a partial file behind."""
    size = os.path.getsize(src)
    free = shutil.disk_usage(stage_dir).free
    if free < size + (64 << 20):
        raise InstallError(f"not enough free disk space to stage {src.name}: it needs about {size >> 20} MiB "
                           f"and {free >> 20} MiB are free in {stage_dir}", fix="free some space, then re-run")
    dest = stage_dir / entry["file"]
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out, open(src, "rb") as source:
            shutil.copyfileobj(source, out, 1 << 20)
    except OSError as exc:
        try:
            dest.unlink()
        except OSError:
            pass
        raise InstallError(f"could not copy {src.name} into {stage_dir}: {exc}", fix="free some space, then re-run")
    return dest, _verified(dest, entry, manifest, signature, **verify_kw)


def _bundle_entry(manifest: _trust.Manifest, target: Mapping[str, str]) -> Dict[str, Any]:
    for entry in manifest.data["bundles"]:
        if entry["arch"] == target["arch"]:
            return entry
    raise InstallError(f"engine {manifest.version} has no image bundle for {target['arch']}",
                       fix="use --no-images for the client binary only, or install on a supported architecture")


def _load_images(manifest: _trust.Manifest, bundle_path: Path, docker: Docker, target: Mapping[str, str],
                 runner, which: Callable, log: Callable[[str], Any]
                 ) -> Tuple[Dict[str, str], Dict[str, str], str]:
    """Load a verified bundle into Docker and tag each image: ``(images, ids, method)``. The one
    image-install path, used by both the online and the offline install."""
    layout = _bundle.open_layout(bundle_path)
    expected = _bundle.expected_images(layout, manifest.images, target["arch"])
    stable = {item.name: stable_ref(item.name, manifest.version) for item in expected}
    method = _bundle.load(layout, expected, stable, docker, which=which, runner=runner, log=log)
    images: Dict[str, str] = {}
    ids: Dict[str, str] = {}
    for item in expected:
        if method == "skopeo":
            image_id = docker.image_id(stable[item.name])
            if image_id not in item.candidates:
                raise InstallError(f"image {item.name} loaded by skopeo has ID {image_id}, not one the "
                                   f"signed manifest allows ({', '.join(item.candidates)})")
        else:
            image_id = next((c for c in (docker.image_id(c) for c in item.candidates) if c), None)
            if image_id is None or image_id not in item.candidates:
                raise InstallError(f"after docker load, image {item.name} ({item.digest}) is not in Docker",
                                   fix="docker load the bundle by hand to see the error, then re-run")
            docker.tag(image_id, stable[item.name])
        images[item.name], ids[item.name] = stable[item.name], image_id
        if method == "docker load" and item.ref_name and "/" in item.ref_name and \
                not item.ref_name.startswith(LOCAL_PREFIX):
            # The bundle names images `briefcase-engine/<image>:<version>`, which reads as a Docker
            # Hub name; drop that tag (the image stays under its verified local tag).
            docker.untag(item.ref_name)
    if not all(item.digest_checked for item in expected):
        log("note: the bundle is a `docker save` archive without registry digests; images were matched by "
            "image ID (the bundle itself is covered by the signed manifest)")
    log(f"loaded {len(images)} images with {method}")
    return images, ids, method


# --------------------------------------------------------------------------- shared


def _find_sig(path: Path, entry: Mapping[str, Any]) -> Optional[bytes]:
    for candidate in (path.with_name(entry["signature"]), path.with_name(path.name + ".sig")):
        if candidate.is_file():
            return candidate.read_bytes()
    return None


def _binary_entry(manifest: _trust.Manifest, target: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    try:
        return manifest.binary_for(target["os"], target["arch"])
    except _trust.TrustError:
        return None


def _save_manifest(install_dir: Path, manifest: _trust.Manifest, sig: bytes) -> None:
    install_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (install_dir / "engine-manifest.json").write_bytes(manifest.raw)
    (install_dir / "engine-manifest.json.sig").write_bytes(sig)


def _install_binary(archive: Path, install_dir: Path, entry: Mapping[str, Any], how: str, runner, which,
                    log) -> Tuple[Path, Dict[str, Any]]:
    checked = "signature and sha256" if how == "signature" else "sha256 from the signed manifest"
    log(f"{entry['file']} verified ({checked})")
    binary = extract_binary(archive, install_dir / "bin")
    # Only now, after verification: a quarantined unsigned build would be blocked by Gatekeeper.
    if clear_quarantine(binary, runner, which):
        log(f"removed the macOS quarantine attribute from {binary}")
    return binary, {"file": entry["file"], "sha256": entry["sha256"], "verified_by": how}


def _finish(opts: InstallOptions, manifest: _trust.Manifest, install_dir: Path, binary: Optional[Path],
            binary_info: Optional[Dict[str, Any]], images: Dict[str, str], ids: Dict[str, str],
            extra: Dict[str, Any], runner, log, target: Mapping[str, str]) -> Dict[str, Any]:
    env = opts.env
    client: Dict[str, Any] = {"mode": "binary"}
    if binary is None:
        if not images:
            raise InstallError(
                f"engine {manifest.version} has no oci-bai build for {target['os']}/{target['arch']}, and "
                "--no-images leaves no server image to run it from",
                fix="briefcase-ai engine install   # without --no-images")
        client = {"mode": "container", "image": images[CLIENT_IMAGE], "image_id": ids[CLIENT_IMAGE]}
        log(f"no oci-bai build for {target['os']}/{target['arch']} in engine {manifest.version}: commands that "
            f"need it (env cohort, run attach, run submit) run it from {images[CLIENT_IMAGE]} with `docker run`")
    compose_file = None
    tls_info: Dict[str, str] = {}
    if images:
        missing = [n for n in REQUIRED_IMAGES if n not in images]
        if missing:
            raise InstallError(f"the signed manifest lacks the {', '.join(missing)} image(s)")
        _compose.ensure_secrets(_engine.secrets_path(env))
        if opts.tls:
            tls_info = _compose.make_tls(_engine.engine_dir(env) / "tls", runner)
        compose_images = {name.replace("-", "_"): ref for name, ref in images.items()}
        compose_file = _compose.write(install_dir, compose_images, manifest.version, tls=opts.tls,
                                      digests={name.replace("-", "_"): manifest.images[name]["digest"]
                                               for name in images})
    record = {
        "version": manifest.version,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": opts.mode,
        "platform_url": None if opts.mode == "bundle" else opts.platform_url,
        "key_id": manifest.key_id,
        "key_file": manifest.key.label,
        "manifest": str(install_dir / "engine-manifest.json"),
        "binary": str(binary) if binary else None,
        "binary_file": binary_info,
        "client": client,
        "images": images,
        "image_digests": {name: manifest.images[name]["digest"] for name in images},
        "image_ids": ids,
        "compose": str(compose_file) if compose_file else None,
        "env_file": str(_engine.secrets_path(env)),
        "grpc_addr": ("https://127.0.0.1:50051" if opts.tls else "http://127.0.0.1:50051"),
        "gateway": _engine.DEFAULT_GATEWAY,
        "tenant": _compose.TENANT,
        "tls": bool(opts.tls),
        "tls_ca": tls_info.get("ca"),
        **extra,
    }
    state = _engine.load_state(env)
    state["schema"] = 2
    state.setdefault("installs", {})[manifest.version] = record
    if opts.make_current or not state.get("current"):
        state["current"] = manifest.version
    save_state(state, env)
    return record


def set_current(version: str, env: Optional[Mapping[str, str]] = None) -> None:
    """Make an installed release the current one (under the install lock)."""
    with engine_lock(env):
        state = _engine.load_state(env)
        if version in (state.get("installs") or {}) and state.get("current") != version:
            state["current"] = version
            save_state(state, env)


# --------------------------------------------------------------------------- newer versions


def latest_version(platform: str, token: str, *, opener: Callable = urllib.request.urlopen,
                   fetch: Callable = _open, timeout: float = 5.0, verify_kw: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    """The newest release from the platform's signed index (``GET /v1/engine/index`` with a licence token ->
    a presigned ``index_url`` of ONE object: the index text and its signature, see ``trust.open_index``).
    Raises on any failure."""
    keys = _trust.require_keys()
    req = urllib.request.Request(platform.rstrip("/") + INDEX_PATH, method="GET",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                          "User-Agent": USER_AGENT})
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise InstallError(f"the platform answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise InstallError(f"cannot reach the platform ({exc})") from exc
    raw = fetch_bytes(data.get("index_url", ""), "index.json", 2 * _trust.MAX_INDEX_PAYLOAD, fetch)
    index, key = _trust.open_index(raw, keys, **(verify_kw or {}))
    versions = [v for v in index.get("versions") or []
                if isinstance(v, dict) and v.get("version")]
    if not versions:
        raise InstallError("the release index lists no versions")
    # Newest first (oci-jj engine_release.py `index`); prereleases only when nothing else exists.
    newest = next((v for v in versions if not v.get("prerelease")), versions[0])
    return {"version": str(newest["version"]).lstrip("v"), "summary": newest.get("summary"),
            "key_id": key.key_id}


def newer(latest: str, current: str) -> bool:
    try:
        return _trust.parse_version(latest) > _trust.parse_version(current)
    except ValueError:
        return latest != current
