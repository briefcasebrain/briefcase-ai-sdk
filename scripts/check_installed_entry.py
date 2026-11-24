#!/usr/bin/env python3
"""Check the *installed* ``briefcase-ai`` console script from outside the checkout.

Run after ``pip install`` of a built wheel (and again for a wheel built from the sdist): the console script must
resolve to ``briefcase_entry`` shipped in site-packages, must run ordinary commands, and a hook invocation that binds
the hook to a policy hash it cannot satisfy must be blocked in every spelling (leading global option, with and without
``--enforce``) with a reason on stderr and exit 2, or a JSON denial on stdout and exit 0.

    python scripts/check_installed_entry.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

EVENT = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
CURSOR_EVENT = {"hook_event_name": "beforeShellExecution", "command": "ls"}


def blocked(tool, done):
    if done.returncode == 2:
        return bool(done.stderr.strip())  # Codex ignores a bare exit 2
    if done.returncode != 0 or not done.stdout.strip():
        return False
    out = json.loads(done.stdout)
    return out.get("permission") == "deny" if tool == "cursor" else (
        out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")


def main() -> int:
    exe = shutil.which("briefcase-ai")
    if exe is None:
        print("FAIL: no `briefcase-ai` console script on PATH")
        return 1
    failures = []
    with tempfile.TemporaryDirectory() as home:  # outside the checkout, with an empty HOME and no PYTHONPATH
        env = {"HOME": home, "USERPROFILE": home, "PATH": os.environ.get("PATH", "")}
        entry = subprocess.run([sys.executable, "-c", "import briefcase_entry; print(briefcase_entry.__file__)"],
                               cwd=home, env=env, capture_output=True, text=True)
        where = entry.stdout.strip()
        if entry.returncode != 0 or "site-packages" not in where.replace("\\", "/") and "dist-packages" not in where:
            failures.append(f"briefcase_entry is not installed in site-packages ({where or entry.stderr.strip()})")
        version = subprocess.run([exe, "--version"], cwd=home, env=env, capture_output=True, text=True)
        if version.returncode != 0 or "briefcase" not in version.stdout.lower():
            failures.append(f"`briefcase-ai --version` failed: {version.returncode} {version.stderr.strip()}")
        binding = "--require-policy-sha256=" + "0" * 64
        for tool in ("claude-code", "codex", "cursor"):
            event = json.dumps(CURSOR_EVENT if tool == "cursor" else EVENT)
            for argv in ([exe, "hook", tool, "--enforce", binding], [exe, "--debug", "hook", tool, binding]):
                done = subprocess.run(argv, input=event, cwd=home, env=env, capture_output=True, text=True)
                if not blocked(tool, done):
                    failures.append(f"{' '.join(argv[1:])}: not blocked (exit {done.returncode}, "
                                    f"stdout {done.stdout.strip()[:80]!r}, stderr {done.stderr.strip()[:80]!r})")
    for line in failures:
        print("FAIL:", line)
    if not failures:
        print("installed console script OK:", exe)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
