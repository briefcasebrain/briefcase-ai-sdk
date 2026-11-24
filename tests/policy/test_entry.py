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
if %r:
    import os
    os.close(2)  # stderr cannot carry anything
import importlib
module_name, _, func = %r.partition(":")
sys.exit(getattr(importlib.import_module(module_name), func)())
"""


def console_target():
    text = (ROOT / "pyproject.toml").read_text()
    return re.search(r'^briefcase-ai = "([^"]+)"', text, re.M).group(1)


def run_console(argv, blocked, home, stdin, close_stderr=False):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT),
           "BRIEFCASE_SYSTEM_POLICY": os.environ["BRIEFCASE_SYSTEM_POLICY"]}
    script = PRELUDE % (blocked, argv, close_stderr, console_target())
    done = subprocess.run([sys.executable, "-c", script], input=stdin, capture_output=True, text=True, env=env,
                          timeout=60)
    from tests.policy import test_hook_invariants as invariants

    invariants.LAST_STDERR = done.stderr  # the decoder needs it: Codex ignores exit 2 without a reason
    return done


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


# --- round 16: any binding flag makes the hook strict on every path, with or without --enforce -------------------

BINDING = [["--fail-closed"], ["--require-policy-sha256=" + "0" * 64], ["--require-policy-sha256", "0" * 64]]
ENFORCE = [["--enforce"], []]


@pytest.mark.parametrize("enforce", ENFORCE, ids=["enforce", "no_enforce"])
@pytest.mark.parametrize("flags", BINDING, ids=["flag", "hash_eq", "hash_sep"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_binding_flag_makes_a_hook_strict_with_or_without_enforce(home, project, tool, flags, enforce):
    build_state(home, project, "valid_denying")
    done = run_console(["hook", tool, *enforce, *flags], ["briefcase.policy.hook"], home,
                       json.dumps(payload(tool, project)))
    assert denies(tool, done.returncode, done.stdout), (done.returncode, done.stdout, done.stderr)


@pytest.mark.parametrize("enforce", ENFORCE, ids=["enforce", "no_enforce"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_hook_that_binds_nothing_is_never_blocked_by_a_failure(home, project, tool, enforce):
    done = run_console(["hook", tool, *enforce], ["briefcase.policy.hook"], home, json.dumps(payload(tool, project)))
    assert done.returncode == 0 and not denies(tool, done.returncode, done.stdout or "{}"), done


@pytest.mark.parametrize("blocked", [["runpy"], ["briefcase.policy.hook"], []])
@pytest.mark.parametrize("flags", BINDING, ids=["flag", "hash_eq", "hash_sep"])
@pytest.mark.parametrize("tool", TOOLS)
def test_the_denial_reaches_stdout_when_stderr_cannot_carry_it(home, project, tool, flags, blocked):
    """The entry's outer fallback and the launcher share one delivery routine: the reason on stderr with exit 2, else
    the JSON denial on stdout with exit 0."""
    build_state(home, project, "valid_denying")
    done = run_console(["hook", tool, "--enforce", *flags], blocked, home, json.dumps(payload(tool, project)),
                       close_stderr=True)
    assert denies(tool, done.returncode, done.stdout), (done.returncode, done.stdout, done.stderr)


def test_the_entry_package_is_declared_for_the_wheel_and_the_console_script():
    """Without the maturin declaration the wheel would name a module it does not contain."""
    text = (ROOT / "pyproject.toml").read_text()
    assert console_target() == "briefcase_entry:main"
    assert re.search(r'^python-packages = \[[^\]]*"briefcase_entry"', text, re.M)
    assert (ROOT / "briefcase_entry" / "__init__.py").is_file()


# --- round 17: every dispatch site consults optargs.enforcing; the entry carries its own fixed denial ---------------

def test_every_function_that_routes_a_hook_invocation_consults_enforcing():
    """Grep-driven: any function that calls run_cursor/run_enforce, or compares an argument list with "--enforce",
    must itself consult enforcing() (the one rule); a new dispatch site cannot skip it. `dispatch` is the one function
    that may call the two runners."""
    import ast

    sites, callers = {}, {}
    for path in list((ROOT / "briefcase").rglob("*.py")) + [ROOT / "briefcase_entry" / "__init__.py"]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            text = ast.get_source_segment(path.read_text(), node) or ""
            names = {n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(node)
                     if isinstance(n, (ast.Attribute, ast.Name))}
            compares = any(isinstance(c, ast.Constant) and c.value == "--enforce" for c in ast.walk(node)
                           if isinstance(c, ast.Constant)) and any(isinstance(n, ast.Compare) for n in ast.walk(node))
            routes = names & {"run_cursor", "run_enforce"}
            if routes or compares:
                sites[f"{path.relative_to(ROOT)}::{node.name}"] = "enforcing" in text or "enforcing_for" in text
            if routes:
                callers[f"{path.relative_to(ROOT)}::{node.name}"] = routes
    assert sites, "the scan found no dispatch site at all: the test is not looking at the right places"
    # These write or recognise the text of a hook command (and `connect --enforce`); they route nothing.
    exempt = {"briefcase/policy/presets.py::self_command", "briefcase/policy/install.py::hook_command",
              "briefcase/policy/install.py::_command_in"}
    missing = [name for name, consults in sites.items() if not consults and name not in exempt]
    assert not missing, f"these routing functions do not consult enforcing(): {missing}"
    # run_cursor (the forwarder) and run_enforce may only be called from dispatch, which consults enforcing.
    called_outside = [name for name in callers if not name.endswith("::dispatch")]
    assert not called_outside, called_outside


@pytest.mark.parametrize("flags", [["--require-policy-sha256=" + "0" * 64], ["--fail-closed"]], ids=["hash", "flag"])
def test_in_process_cursor_without_enforce_and_a_binding_flag_is_enforced(home, project, monkeypatch, flags):
    import io

    from briefcase.cli.__main__ import main as cli_main

    build_state(home, project, "valid_denying")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload("cursor", project))))
    monkeypatch.setattr(sys, "stdout", out)
    code = cli_main(["hook", "cursor", *flags])  # no --enforce
    assert denies("cursor", code, out.getvalue()), (code, out.getvalue())


@pytest.mark.parametrize("flags", [["--require-policy-sha256=" + "0" * 64], ["--fail-closed"]], ids=["hash", "flag"])
def test_in_process_cursor_import_failure_without_enforce_and_a_binding_flag_still_blocks(
        home, project, monkeypatch, flags):
    import io

    import briefcase.cli as cli_pkg
    from briefcase.cli.__main__ import main as cli_main

    build_state(home, project, "valid_denying")
    monkeypatch.setitem(sys.modules, "briefcase.cli.hook_commands", None)
    monkeypatch.delattr(cli_pkg, "hook_commands", raising=False)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload("cursor", project))))
    monkeypatch.setattr(sys, "stdout", out)
    code = cli_main(["hook", "cursor", *flags])
    assert denies("cursor", code, out.getvalue()), (code, out.getvalue())


def run_with_stub_briefcase(tmp_path, argv, home, stdin, close_stderr=False):
    """The entry on a cold interpreter where `briefcase` is a bare package with no policy/hook_main.py."""
    stub = tmp_path / "stub"
    (stub / "briefcase").mkdir(parents=True, exist_ok=True)
    (stub / "briefcase" / "__init__.py").write_text("")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": f"{stub}{os.pathsep}{ROOT}"}
    script = PRELUDE % ([], argv, close_stderr, "briefcase_entry:main")
    return subprocess.run([sys.executable, "-c", script], input=stdin, capture_output=True, text=True, env=env,
                          timeout=60)


@pytest.mark.parametrize("flags", BINDING, ids=["flag", "hash_eq", "hash_sep"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_missing_launcher_still_delivers_the_denial_text(home, project, tmp_path, tool, flags):
    done = run_with_stub_briefcase(tmp_path, ["hook", tool, "--enforce", *flags], home,
                                   json.dumps(payload(tool, project)))
    assert done.returncode == 2 and "Blocked by briefcase-ai policy" in done.stderr, done
    closed = run_with_stub_briefcase(tmp_path, ["hook", tool, "--enforce", *flags], home,
                                     json.dumps(payload(tool, project)), close_stderr=True)
    assert denies(tool, closed.returncode, closed.stdout), closed


def test_the_entrys_fixed_denial_is_byte_for_byte_the_launchers():
    import importlib.util

    import briefcase_entry

    path = ROOT / "briefcase" / "policy" / "hook_main.py"
    spec = importlib.util.spec_from_file_location("_hook_main_for_test", path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    assert (briefcase_entry._REASON, briefcase_entry._DENY, briefcase_entry._DENY_CURSOR) == (
        launcher._REASON, launcher._DENY, launcher._DENY_CURSOR)
