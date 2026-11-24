"""The ``briefcase-ai`` console script decides whether argv is a hook invocation before it imports anything of this
SDK, so a hook gives its verdict however broken the rest of the package is. Run on a cold interpreter: a fresh
python process whose import system refuses the named modules."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.policy.test_hook_invariants import TOOLS, build_state, denies, payload

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX paths")

ROOT = Path(__file__).resolve().parents[2]
BLOCKED = ["briefcase.cli.engine", "briefcase.cli.state", "briefcase._logging", "briefcase.decorators",
           "briefcase.cli", "briefcase.policy.hook_commands"]
LEADING = [[], ["--debug"], ["--no-input", "--debug"], ["--server", "host:1", "--repo=r"], ["--deb"], ["--dry-run"]]

PRELUDE = """
import sys
BLOCKED = %r
class Block:
    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in BLOCKED):
            raise ImportError("blocked for the test: " + name)
sys.meta_path.insert(0, Block())
sys.argv = ["briefcase-ai"] + %r
import importlib
module_name, _, func = %r.partition(":")
sys.exit(getattr(importlib.import_module(module_name), func)())
"""


def console_target():
    text = (ROOT / "pyproject.toml").read_text()
    return re.search(r'^briefcase-ai = "([^"]+)"', text, re.M).group(1)


def run_console(argv, blocked, home, stdin):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT),
           "BRIEFCASE_SYSTEM_POLICY": os.environ["BRIEFCASE_SYSTEM_POLICY"]}
    script = PRELUDE % (blocked, argv, console_target())
    return subprocess.run([sys.executable, "-c", script], input=stdin, capture_output=True, text=True, env=env,
                          timeout=60)


@pytest.mark.parametrize("leading", LEADING, ids=lambda v: "-".join(v) or "none")
@pytest.mark.parametrize("blocked", BLOCKED)
@pytest.mark.parametrize("tool", TOOLS)
def test_a_hook_invocation_gives_its_verdict_whatever_else_fails_to_import(home, project, tool, blocked, leading):
    build_state(home, project, "valid_denying")
    done = run_console([*leading, "hook", tool, "--require-policy-sha256=" + "0" * 64, "--enforce"], [blocked], home,
                       json.dumps(payload(tool, project)))
    assert denies(tool, done.returncode, done.stdout), (done.returncode, done.stdout, done.stderr)


@pytest.mark.parametrize("tool", TOOLS)
def test_a_hook_that_binds_nothing_still_fails_open_on_a_broken_package(home, project, tool):
    done = run_console(["hook", tool, "--enforce"], ["briefcase.cli.engine", "briefcase.cli.state"], home,
                       json.dumps(payload(tool, project)))
    assert done.returncode == 0 and not denies(tool, done.returncode, done.stdout or "{}"), done


def test_the_console_script_still_runs_ordinary_commands(home):
    done = run_console(["--version"], [], home, "")
    assert done.returncode == 0 and "briefcase" in done.stdout.lower(), done
