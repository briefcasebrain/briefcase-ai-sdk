"""Background lifecycle for ``briefcase-ai receive``: ``--detach``, ``--stop``, ``--status``.

State lives under ``~/.briefcase/receiver/`` (``BRIEFCASE_HOME`` overrides the
root): a JSON pidfile, the log, and by default the JSONL the records go to.
The token is passed to the child through the environment, never argv, and is
not written to the pidfile.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from briefcase.receiver.server import DEFAULT_HOST, DEFAULT_PORT, SERVICE

TOKEN_ENV = "BRIEFCASE_RECEIVER_TOKEN"


def state_dir() -> Path:
    override = os.environ.get("BRIEFCASE_HOME")
    root = Path(override).expanduser() if override else Path.home() / ".briefcase"
    return root / "receiver"


def pidfile() -> Path:
    return state_dir() / "receiver.pid"


def default_sink() -> Path:
    return state_dir() / "records.jsonl"


def log_path() -> Path:
    return state_dir() / "receiver.log"


def salt_path() -> Path:
    return state_dir() / "email-hash.key"


def probe(endpoint: str, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    """``/healthz`` of a briefcase receiver at ``endpoint``; ``None`` if absent."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(endpoint.rstrip("/") + "/healthz", timeout=timeout) as response:
            data = json.loads(response.read(65536).decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) and data.get("service") == SERVICE else None


def _local_endpoint(host: str, port: int) -> str:
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"


def read_pidfile() -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(pidfile().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None


def _alive(pid: int) -> bool:
    if os.name != "posix":  # os.kill(pid, 0) terminates the process on Windows
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def status() -> Dict[str, Any]:
    info = read_pidfile()
    if info is None:
        return {"running": False}
    endpoint = _local_endpoint(info.get("host", DEFAULT_HOST), info.get("port", DEFAULT_PORT))
    health = probe(endpoint) if _alive(info["pid"]) else None
    return {"running": health is not None, "endpoint": endpoint, "health": health, **info}


def detach(args: List[str], host: str, port: int, to: str, token: Optional[str],
           wait: float = 5.0) -> Dict[str, Any]:
    """Start ``briefcase-ai receive <args>`` in the background and wait until it
    answers ``/healthz``. Raises ``RuntimeError`` with the log tail if it does not."""
    current = status()
    if current.get("running"):
        raise RuntimeError(f"a receiver is already running (pid {current['pid']}, {current['endpoint']})")
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ)
    if token:
        env[TOKEN_ENV] = token
    fd = os.open(str(log_path()), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        popen_kwargs: Dict[str, Any] = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        else:  # pragma: no cover - Windows
            popen_kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        child = subprocess.Popen(
            [sys.executable, "-m", "briefcase.cli", "receive", *args],
            stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, env=env, **popen_kwargs,
        )
    finally:
        os.close(fd)
    endpoint = _local_endpoint(host, port)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        if probe(endpoint, timeout=0.5) is not None:
            info = {"pid": child.pid, "host": host, "port": port, "to": to,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            tmp = pidfile().with_suffix(".tmp")
            tmp.write_text(json.dumps(info), encoding="utf-8")
            os.replace(tmp, pidfile())
            return dict(info, endpoint=endpoint)
        time.sleep(0.1)
    if child.poll() is None:
        child.terminate()
    tail = ""
    try:
        tail = "\n".join(log_path().read_text(encoding="utf-8", errors="replace").splitlines()[-5:])
    except OSError:
        pass
    raise RuntimeError(f"the receiver did not come up on {endpoint}" + (f":\n{tail}" if tail else ""))


def stop(wait: float = 5.0) -> Optional[Dict[str, Any]]:
    """Stop the detached receiver. Returns its pidfile info, or ``None`` when
    nothing was running. The pid is signalled only while the recorded port
    still answers as a briefcase receiver, so a recycled pid is never killed."""
    current = status()
    info = read_pidfile()
    if info is None:
        return None
    if current.get("running"):
        try:
            os.kill(info["pid"], signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and _alive(info["pid"]):
            try:  # reap it when it is our own child, or it stays a zombie that looks alive
                os.waitpid(info["pid"], os.WNOHANG)
            except (ChildProcessError, OSError, AttributeError):
                pass
            time.sleep(0.05)
    try:
        pidfile().unlink()
    except OSError:
        pass
    return info if current.get("running") else None
