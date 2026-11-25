"""Levels, the report model and the small helpers every driver shares."""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCS_ONLY = "docs-only"
CONFIG_ACCEPTED = "config-accepted"
TELEMETRY_VERIFIED = "telemetry-verified"
LEVELS = (DOCS_ONLY, CONFIG_ACCEPTED, TELEMETRY_VERIFIED)  # ordered, lowest first

LEVEL_MEANING = {
    DOCS_ONLY: "Built from the vendor's documentation. No real install of the tool was exercised "
               "(or what we write could not be checked by the tool).",
    CONFIG_ACCEPTED: "A real install loaded the config `briefcase-ai connect` wrote and the tool's own "
                     "commands reported it (or did not reject it). No telemetry was captured.",
    TELEMETRY_VERIFIED: "The real tool emitted telemetry that `briefcase-ai receive` turned into "
                        "decision records.",
}


def level_rank(level: Optional[str]) -> int:
    return LEVELS.index(level) if level in LEVELS else -1


def today() -> str:
    return _dt.date.today().isoformat()


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ToolResult:
    tool: str
    level: str = DOCS_ONLY
    version: Optional[str] = None
    date: str = field(default_factory=today)
    status: str = "ok"            # ok | failed | skipped
    note: str = ""
    install: str = ""             # the command used and where it came from
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, ok, " ".join(detail.split())[:200]))
        return ok

    def as_dict(self) -> Dict[str, Any]:
        return {"tool": self.tool, "level": self.level, "version": self.version, "date": self.date,
                "status": self.status, "note": self.note, "install": self.install,
                "checks": [c.as_dict() for c in self.checks]}


# -- report ----------------------------------------------------------------------

def new_report() -> Dict[str, Any]:
    return {"schema_version": 1, "generated": today(), "levels": list(LEVELS), "tools": {}}


def merge_results(report: Dict[str, Any], results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold per-tool results into ``report``. A tool that was skipped or failed keeps its
    previously recorded level and gains a ``last_attempt`` entry, so one bad night does not
    erase what was once verified."""
    tools = report.setdefault("tools", {})
    for result in results:
        entry = tools.get(result["tool"], {})
        if result["status"] in ("ok", "skipped"):
            tools[result["tool"]] = {k: result[k] for k in ("level", "version", "date", "note", "install")}
            tools[result["tool"]]["checks"] = result["checks"]
        else:
            entry = dict(entry)
            entry.setdefault("level", DOCS_ONLY)
            entry["last_attempt"] = {k: result.get(k) for k in ("status", "date", "note", "version")}
            tools[result["tool"]] = entry
    report["generated"] = today()
    return report


def level_changes(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """Human-readable lines for every tool whose level changed or whose run failed."""
    lines = []
    for tool, entry in sorted(new.get("tools", {}).items()):
        before = old.get("tools", {}).get(tool, {}).get("level")
        after = entry.get("level")
        if before and before != after:
            arrow = "DOWNGRADED" if level_rank(after) < level_rank(before) else "upgraded"
            lines.append(f"{tool}: {arrow} {before} -> {after}")
        attempt = entry.get("last_attempt")
        if attempt and attempt.get("status") == "failed":
            lines.append(f"{tool}: run failed: {attempt.get('note')}")
    return lines


# -- process helpers ---------------------------------------------------------------

# Identity and session variables of the environment the harness itself runs in. A tool under
# test must not inherit them: they would end up in captured payloads and could authenticate.
_SCRUB_ENV_PREFIXES = ("CLAUDE", "ANTHROPIC", "OPENAI", "CODEX", "GEMINI", "GOOGLE_", "GITHUB_TOKEN",
                       "GH_TOKEN", "COPILOT", "OTEL_", "AI_AGENT", "SESSION_INGRESS")


def clean_env(home: Path, extra: Optional[Dict[str, str]] = None, path_prepend: Sequence[Path] = ()) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB_ENV_PREFIXES)}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["BRIEFCASE_HOME"] = str(home / ".briefcase")
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["PATH"] = os.pathsep.join([*(str(p) for p in path_prepend), env.get("PATH", "")])
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), env.get("PYTHONPATH", "")]).strip(os.pathsep)
    env["CI"] = "1"
    env.update(extra or {})
    return env


@dataclass
class Run:
    argv: List[str]
    rc: int
    out: str          # stdout + stderr
    stdout: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


def run(argv: Sequence[str], env: Dict[str, str], cwd: Optional[Path] = None, timeout: float = 120,
        stdin_devnull: bool = True) -> Run:
    try:
        proc = subprocess.run(list(argv), env=env, cwd=str(cwd) if cwd else None, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL if stdin_devnull else None)
        return Run(list(argv), proc.returncode, (proc.stdout or "") + (proc.stderr or ""), proc.stdout or "")
    except subprocess.TimeoutExpired as exc:
        return Run(list(argv), 124, f"timed out after {timeout}s: {exc.stdout or ''}")
    except OSError as exc:
        return Run(list(argv), 127, str(exc))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def http_get_json(url: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


class Sandbox:
    """A temp HOME, project dir and install prefix, removed on exit."""

    def __init__(self, keep: bool = False) -> None:
        self.keep = keep
        self.root = Path(tempfile.mkdtemp(prefix="briefcase-conformance-"))
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.prefix = self.root / "prefix"
        for directory in (self.home, self.project, self.prefix):
            directory.mkdir()

    def env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        return clean_env(self.home, extra, path_prepend=[self.prefix / "bin"])

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        if not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)


def briefcase_cli(*args: str) -> List[str]:
    return [sys.executable, "-m", "briefcase.cli", *args]


def npm_install(package: str, sandbox: Sandbox, env: Dict[str, str], timeout: float = 400) -> Run:
    """``npm install -g <package> --prefix <sandbox prefix>``, the form every vendor's install docs use,
    redirected to an isolated prefix."""
    return run(["npm", "install", "-g", "--prefix", str(sandbox.prefix), package], env, timeout=timeout)
