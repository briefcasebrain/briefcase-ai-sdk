"""State and Docker identity checks for safe retirement of legacy engine installations."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

from briefcase import engine as _engine

LOCAL_PREFIX = "localhost/briefcase-engine"
REQUIRED_IMAGES = ("server", "gateway", "workers", "postgres")


class InstallError(RuntimeError):
    """A failed install step: what happened plus the command or action that fixes it."""

    def __init__(self, message: str, fix: Optional[str] = None, code: int = 1) -> None:
        super().__init__(message)
        self.fix = fix
        self.code = code


class Docker:
    """Docker inspection and removal for legacy records, through an injectable runner."""

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

    def image_id(self, ref: str, strict: bool = False) -> Optional[str]:
        res = self._run(["image", "inspect", "--format", "{{.Id}}", ref], capture_output=True, text=True)
        if res is None or getattr(res, "returncode", 1) != 0:
            if strict and "no such image:" not in self._err(res).lower():
                raise InstallError(f"docker could not inspect {ref}")
            return None
        image = (getattr(res, "stdout", "") or "").strip()
        if strict and not image:
            raise InstallError(f"docker returned no image ID for {ref}")
        return image or None

    def remove_image(self, ref: str) -> None:
        res = self._run(["image", "rm", ref], capture_output=True, text=True)
        if res is None or getattr(res, "returncode", 1) != 0:
            raise InstallError(f"docker could not remove {ref}")


# --------------------------------------------------------------------------- options + state


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


class DaemonIdentity(str):
    """``"<context> <endpoint>"`` (plus `` id=<the daemon's own id>`` when it answered) for the Docker daemon that was
    verified, with what makes ``docker`` talk to exactly that daemon whatever the context is repointed to afterwards:
    ``pin_env`` (the endpoint and the certificates it uses) and ``pin_args`` (global flags placed before the
    subcommand). It compares and is stored as the plain string."""

    pin_env: Dict[str, str]
    pin_args: List[str]

    def __new__(cls, text: str, pin_env: Optional[Mapping[str, str]] = None,
                pin_args: Optional[List[str]] = None) -> "DaemonIdentity":
        self = super().__new__(cls, text)
        self.pin_env = dict(pin_env or {})
        self.pin_args = list(pin_args or [])
        return self


_INSPECT_FORMAT = ("{{.Endpoints.docker.Host}}\t{{.Storage.TLSPath}}\t{{.Endpoints.docker.SkipTLSVerify}}\t"
                   '{{range index .TLSMaterial "docker"}}{{.}},{{end}}')
_TLS_VARIABLES = ("DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_TLS")
# TLS without verification and without certificate files, which is what the CLI does for a context that only skips
# verification (Docker 29.8.2: DOCKER_TLS=1 and a bare --tls fail on the missing ca.pem; these flags do not).
_TLS_NO_CERTIFICATES = ["--tls", "--tlscacert=", "--tlscert=", "--tlskey="]


def _split_id(identity: str) -> Tuple[str, Optional[str]]:
    base, _, daemon_id = identity.partition(" id=")
    return base, daemon_id or None


def daemon_matches(recorded: str, here: str) -> bool:
    """Is ``here`` the daemon ``recorded`` names? ``here`` must have stated its id (an endpoint alone proves nothing: a
    tunnel can be pointed at another daemon); the record matches by that id when it has one, and a record from before
    ids existed matches by endpoint."""
    recorded_base, recorded_id = _split_id(recorded)
    here_base, here_id = _split_id(here)
    if here_id is None:
        return False
    return recorded_base == here_base and (recorded_id is None or recorded_id == here_id)


def same_daemon_id(shown: Optional[str], now: Optional[str]) -> bool:
    """Is ``now`` the daemon that was ``shown`` (same endpoint and the same stated id)? Both must have one."""
    if shown is None or now is None:
        return False
    shown_id, now_id = _split_id(shown)[1], _split_id(now)[1]
    return shown_id is not None and shown_id == now_id and _split_id(shown)[0] == _split_id(now)[0]


def has_id(identity: Optional[str]) -> bool:
    return identity is not None and _split_id(identity)[1] is not None


def _daemon_id(runner, env: Mapping[str, str], pin_env: Mapping[str, str], pin_args: List[str]) -> Optional[str]:
    """The id the daemon at the pinned endpoint reports about itself (not part of the context, so a different daemon
    behind the same address differs here); ``None`` when it does not answer."""
    run_env = {k: v for k, v in env.items() if k not in ("DOCKER_CONTEXT", "DOCKER_HOST", *_TLS_VARIABLES)}
    run_env.update(pin_env)
    try:
        res = runner(["docker", *pin_args, "info", "--format", "{{.ID}}"], capture_output=True, text=True,
                     env=run_env, timeout=20)
    except (OSError, subprocess.SubprocessError, TypeError):
        return None
    if getattr(res, "returncode", 1) != 0:
        return None
    return (getattr(res, "stdout", "") or "").strip() or None


def docker_identity(runner=subprocess.run, env: Optional[Mapping[str, str]] = None) -> Optional[DaemonIdentity]:
    """Which Docker daemon ``docker`` talks to: the active context's name and endpoint (``DOCKER_HOST`` wins), and
    the daemon's own id. The volumes an install created live on that daemon; a probe made against another one proves
    nothing about them. ``None`` when it cannot be established."""
    env = os.environ if env is None else env
    try:
        # Docker 29's actual precedence (checked against the installed CLI): DOCKER_HOST over DOCKER_CONTEXT over the
        # active context. With DOCKER_HOST set, `docker context show` itself reports the default context.
        pin_args: List[str] = []
        if env.get("DOCKER_HOST"):
            host = env["DOCKER_HOST"]
            name = runner(["docker", "context", "show"], capture_output=True, text=True)
            context = (name.stdout or "").strip() if getattr(name, "returncode", 1) == 0 else ""
            context = context or "default"
            # The caller's own TLS variables are part of what identifies this daemon; they stay with the pin.
            pin = {"DOCKER_HOST": host}
            pin.update({v: env[v] for v in _TLS_VARIABLES if v in env})
        else:
            if env.get("DOCKER_CONTEXT"):
                context = env["DOCKER_CONTEXT"]
            else:
                name = runner(["docker", "context", "show"], capture_output=True, text=True)
                if getattr(name, "returncode", 1) != 0:
                    return None
                context = (name.stdout or "").strip() or "default"
            info = runner(["docker", "context", "inspect", context, "--format", _INSPECT_FORMAT],
                          capture_output=True, text=True)  # one read: the endpoint and the certificates it uses
            if getattr(info, "returncode", 1) != 0:
                return None
            host, _, rest = (info.stdout or "").strip("\r\n").partition("\t")
            tls_path, _, rest = rest.partition("\t")
            skip_verify, _, material = rest.partition("\t")
            host, tls_path = host.strip(), tls_path.strip()
            if not host:
                return None
            pin = {"DOCKER_HOST": host}
            skip = skip_verify.strip() == "true"
            files = {name.strip() for name in material.split(",") if name.strip()}
            if files:
                if tls_path.startswith("<"):  # "<IN MEMORY>" (the default context): the caller's variables are it
                    pin.update({v: env[v] for v in _TLS_VARIABLES if v in env})
                else:
                    pin["DOCKER_CERT_PATH"] = tls_path + "/docker"
                    pin["DOCKER_TLS" if skip else "DOCKER_TLS_VERIFY"] = "1"
                    # The CLI reads ca.pem, cert.pem and key.pem from that directory unless told otherwise; a file the
                    # context does not have is switched off (no CA: the system roots, as `--context` does).
                    pin_args = [flag for name, flag in (("ca.pem", "--tlscacert="), ("cert.pem", "--tlscert="),
                                                        ("key.pem", "--tlskey=")) if name not in files]
            elif skip:
                pin_args = list(_TLS_NO_CERTIFICATES)
        base = f"{context} {host}"
        daemon_id = _daemon_id(runner, env, pin, pin_args)
        return DaemonIdentity(base + (f" id={daemon_id}" if daemon_id else ""), pin, pin_args)
    except (OSError, ValueError):
        return None
