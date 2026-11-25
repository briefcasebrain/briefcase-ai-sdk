"""Command auto-discovery, grouped help, and the pre-refactor CLI surface."""

import argparse
import json
import sys
import textwrap
from pathlib import Path

import pytest

from briefcase.cli import _discovery
from briefcase.cli.__main__ import build_parser, main

from tests.cli.parser_tree import files_under, parser_tree

BASELINE = Path(__file__).parent / "fixtures" / "parser_tree_baseline.json"
SHELL_COMMANDS = {"setup", "status", "features", "completion"}

# Additive changes made by packages merged after the baseline was dumped. The
# baseline is a floor, not an exact snapshot: every old argument and default must
# survive unchanged, and anything new has to be listed here on purpose.
#   path -> (new flags/dests allowed, new subcommands allowed, defaults that may change)
ALLOWED_ADDITIONS = {
    # datasets package: versioned datasets (`dataset_versions.extend_dataset_parser`).
    "dataset": (set(), {"from-records", "show", "diff", "export", "import", "publish"}, set()),
    # `dataset list` gained --json and a handler (dataset_versions.cmd_list) that prints
    # the registered datasets exactly as before, then local versions
    # (see test_old_dataset_list_output_unchanged in test_cli_dataset.py).
    "dataset list": ({"--json"}, set(), {"func"}),
    # red-team hardening of exec.
    "exec": ({"--allow-uninstrumented", "--dedupe"}, set(), set()),
    # policy package: `--enforce [--preset]` also installs the enforcement hooks, and
    # `--managed --out DIR [--policy FILE] [--tools ...]` writes an admin rollout pack. All
    # optional, all off by default, so a bare `briefcase-ai connect` behaves as before.
    # `briefcase-ai hook claude-code|codex` are the enforcement hook entry points (stdin JSON,
    # always exit 0); `hook cursor` is unchanged apart from honouring `--enforce`.
    "hook": (set(), {"claude-code", "codex"}, set()),
    "connect": ({"--enforce", "--preset", "--managed", "--out", "--policy", "--tools",
                "--accept-codex-startup-risk"}, set(), set()),
    # plugins-mcp package: `doctor --plugins` reports installed briefcase plugins only.
    "doctor": ({"--plugins"}, set(), set()),
    # engine package: `run attach --result verdict.json` (AttachBench inline record) and
    # `secret list --json` (marks the one key the engine uses).
    "run": (set(), {"attach"}, set()),
    "secret list": ({"--json"}, set(), set()),
}


# --- the surface that existed before discovery ---------------------------------

def test_every_pre_existing_command_keeps_its_names_flags_and_handlers():
    """The baseline was dumped from the hand-wired parser before the refactor. Every
    command path in it must still exist with identical arguments, defaults and handler."""
    baseline = json.loads(BASELINE.read_text())
    current = parser_tree(build_parser())
    assert set(baseline) <= set(current), sorted(set(baseline) - set(current))
    for path, before in baseline.items():
        after = current[path]
        if path == "":
            # The root may gain global flags and commands, never lose one.
            for argument in before["arguments"]:
                assert argument in after["arguments"], argument
            assert set(before["subcommands"]) <= set(after["subcommands"])
            assert set(after["subcommands"]) - set(before["subcommands"]) >= SHELL_COMMANDS
            continue
        new_flags, new_subs, changed_defaults = ALLOWED_ADDITIONS.get(path, (set(), set(), set()))
        for argument in before["arguments"]:
            assert argument in after["arguments"], (path, argument["flags"] or argument["dest"])
        added = [a for a in after["arguments"] if a not in before["arguments"]]
        assert {(a["flags"] or [a["dest"]])[0] for a in added} == new_flags, path
        assert not any(a["required"] for a in added), f"{path}: new arguments must be optional"
        assert set(after["subcommands"]) - set(before["subcommands"]) == new_subs, path
        assert set(before["subcommands"]) <= set(after["subcommands"]), path
        assert after["add_help"] == before["add_help"], path
        for key, value in before["defaults"].items():
            if key not in changed_defaults:
                assert after["defaults"].get(key) == value, (path, key)


@pytest.mark.parametrize("argv", [
    ["dataset", "register", "xor", "--uri", "synthetic://xor"],
    ["dataset", "list"],
    ["secret", "set", "K=V"],
    ["secret", "list"],
    ["run", "submit", "demo", "--repository", "cand", "--dataset", "xor", "--checkpoint", "base",
     "--metric", "f1", "--mode", "hunt", "--secret", "T=1", "--env", "K=V", "--revision", "abc"],
    ["run", "list"], ["run", "inspect", "demo"], ["run", "logs", "demo", "-f"],
    ["run", "results", "demo"], ["run", "stop", "demo"], ["run", "delete", "demo"],
    ["doctor", "--strict"], ["doctor", "--connect"],
    ["connect", "claude-code", "--scope", "project", "--endpoint", "http://h:1", "--headers", "A=b",
     "--no-mcp", "--capture-content", "--dry-run", "--yes", "--force", "--start"],
    ["connect", "--all", "--undo"], ["connect", "list"], ["connect", "status"],
    ["receive", "--port", "4319", "--host", "127.0.0.1", "--to", "x.jsonl", "--keep-content",
     "--metrics", "--token", "t", "--max-body-mb", "2", "--detach"],
    ["receive", "--stop"], ["receive", "--status"],
    ["mcp", "install", "cursor", "--scope", "project", "--dry-run", "-y", "--force"],
    ["mcp", "link", "cursor"],
    ["hook", "cursor"],
    ["exec", "--to", "runs.jsonl", "--capture-content", "hash", "--skip", "langchain", "--no-replace",
     "--", "python", "app.py"],
    ["--server", "http://s", "--repo", "r", "--dry-run", "run", "list"],
])
def test_old_argv_still_parses_to_a_handler(argv):
    args = build_parser().parse_args(argv)
    assert callable(args.func)


def test_handlers_are_the_same_functions_as_before():
    from briefcase.cli import commands, connect_commands, doctor, exec_commands

    from briefcase.cli import dataset_versions

    parser = build_parser()
    # `dataset list` is deliberately served by the datasets package now; it prints the
    # old lines first (test_old_dataset_list_output_unchanged covers the behaviour).
    assert parser.parse_args(["dataset", "list"]).func is dataset_versions.cmd_list
    assert commands.cmd_dataset_list is not None
    assert parser.parse_args(["run", "logs", "x"]).func is commands.cmd_run_logs
    assert parser.parse_args(["doctor"]).func is doctor.cmd_doctor
    assert parser.parse_args(["connect"]).func is connect_commands.cmd_connect
    assert parser.parse_args(["exec", "--", "true"]).func is exec_commands.cmd_exec


# --- discovery ------------------------------------------------------------------

def test_every_commands_module_in_the_package_is_discovered_in_sorted_order():
    import briefcase.cli as package

    on_disk = sorted(p.stem for p in Path(package.__file__).parent.glob("*_commands.py"))
    assert _discovery.module_names() == on_disk
    modules = _discovery.discover()
    assert [entry.name for entry in modules] == on_disk
    assert all(entry.error is None for entry in modules), _discovery.warnings(modules)


@pytest.fixture
def plugin_package(tmp_path, monkeypatch):
    """A package holding one good, one unimportable, one register-less and one
    register-raising command module."""
    root = tmp_path / "fakecli"
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "alpha_commands.py").write_text(textwrap.dedent("""
        GROUP = "enforce"
        ORDER = 5

        def register(sub):
            sub.add_parser("alpha", help="Alpha command.").set_defaults(func=lambda *a: 0)
            sub.add_parser("alpha2", help="Second alpha.").set_defaults(func=lambda *a: 0)

        def status():
            return {"id": "alpha", "state": "on", "detail": "fine", "next": None}
    """))
    (root / "broken_commands.py").write_text("raise RuntimeError('boom at\\nimport')\n")
    (root / "empty_commands.py").write_text("GROUP = 'verify'\n")
    (root / "late_commands.py").write_text(textwrap.dedent("""
        GROUP = "not-a-group"

        def register(sub):
            sub.add_parser("late", help="Registered before the failure.")
            raise ValueError("register blew up")
    """))
    (root / "helper.py").write_text("raise RuntimeError('never imported: not a *_commands module')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib

    package = importlib.import_module("fakecli")
    yield package
    for name in [n for n in sys.modules if n == "fakecli" or n.startswith("fakecli.")]:
        del sys.modules[name]


def test_a_broken_module_is_isolated(plugin_package):
    modules = _discovery.discover(plugin_package)
    by_name = {entry.name: entry for entry in modules}
    assert list(by_name) == ["alpha_commands", "broken_commands", "empty_commands", "late_commands"]
    assert by_name["broken_commands"].module is None
    assert by_name["broken_commands"].error == "import failed: RuntimeError: boom at import"
    assert by_name["empty_commands"].error == "no register(subparsers) function"

    sub = argparse.ArgumentParser().add_subparsers()
    _discovery.register_all(sub, modules)
    assert by_name["alpha_commands"].commands == ["alpha", "alpha2"]
    assert by_name["alpha_commands"].group == "enforce" and by_name["alpha_commands"].order == 5
    assert by_name["late_commands"].error == "register failed: ValueError: register blew up"
    assert by_name["late_commands"].group == "other"          # unknown GROUP falls back
    assert "alpha" in sub.choices

    lines = _discovery.warnings(modules)
    assert len(lines) == 3 and all(line.startswith("warning: skipped briefcase/cli/") for line in lines)
    assert all("\n" not in line for line in lines)
    assert set(_discovery.command_index(modules)) == {"alpha", "alpha2", "late"}


def test_cli_survives_a_broken_module_and_reports_it_only_under_debug(shell_home, monkeypatch, capsys):
    real = _discovery.module_names

    def with_broken(package=None):
        return sorted(real(package) + ["zz_missing_commands"])

    monkeypatch.setattr(_discovery, "module_names", with_broken)
    assert main(["features", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["count"] > 0
    assert "zz_missing_commands" not in captured.err

    assert main(["--debug", "features", "--json"]) == 0
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith("warning: skipped briefcase/cli/zz_missing_commands.py (import failed: ")


# --- grouped help ---------------------------------------------------------------

def test_top_level_help_groups_commands_under_the_flow_verbs(capsys):
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--help"])
    assert exit_info.value.code == 0
    text = capsys.readouterr().out
    headings = ["Start here:", "Connect:", "Capture:", "Enforce:", "Verify:", "Review:", "Evaluate:"]
    positions = [text.index("\n" + heading) for heading in headings]
    assert positions == sorted(positions)

    def section(heading):
        body = text.split("\n" + heading, 1)[1]
        return body.split("\n\n", 1)[0]

    def listed(heading):
        return [line.split()[0] for line in section(heading).splitlines()[1:]
                if line.startswith("  ") and not line.startswith("   ") and not line.startswith("  (")]

    assert listed("Start here:") == ["setup", "status", "features", "doctor", "engine", "completion"]
    assert set(listed("Connect:")) == {"connect", "receive", "exec", "mcp", "hook", "plugins"}
    assert set(listed("Evaluate:")) == {"dataset", "secret", "run"}
    assert listed("Capture:") == ["env"] and "briefcase-ai features --verb capture" in section("Capture:")
    assert "Other" not in text
    assert "Global options:" in text and "--no-input" in text and "--debug" in text


def test_unknown_group_is_listed_under_other(plugin_package):
    from briefcase.cli.__main__ import _GroupedParser

    parser = _GroupedParser(prog="briefcase-ai")
    parser.subparsers = parser.add_subparsers(dest="command", parser_class=argparse.ArgumentParser)
    parser.modules = _discovery.register_all(parser.subparsers, _discovery.discover(plugin_package))
    text = parser.format_commands()
    enforce = text.split("Enforce:", 1)[1].split("\n\n", 1)[0]
    assert "alpha " in enforce and "Alpha command." in enforce
    assert "late" in text.split("\nOther", 1)[1]


def test_subcommand_help_is_plain_argparse(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["connect", "--help"])
    text = capsys.readouterr().out
    assert text.startswith("usage: briefcase-ai connect") and "Start here" not in text


def test_group_without_subcommand_prints_that_groups_help(shell_home, capsys):
    assert main(["run"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("usage: briefcase-ai run") and captured.out == ""


# --- nothing is created by commands that should not touch the store -----------------

@pytest.mark.parametrize("argv", [
    ["connect", "--dry-run", "--all"],
    ["connect", "list"],
    ["mcp", "link", "cursor"],
    ["status"],
    ["status", "--json"],
    ["features"],
    ["features", "connect"],
    ["completion", "bash"],
    ["--no-input"],
    [],
])
def test_store_free_commands_create_nothing(shell_home, argv, capsys):
    assert main(argv) == 0
    assert files_under(shell_home) == []
    assert not (shell_home / ".briefcase").exists()


def test_hook_cursor_bypasses_discovery(shell_home, monkeypatch):
    import io

    monkeypatch.setattr(_discovery, "discover", lambda *a, **k: pytest.fail("discovery ran in the hook path"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert main(["hook", "cursor", "--endpoint", "http://127.0.0.1:9", "--timeout", "0.05"]) == 0
    assert files_under(shell_home) == []


def test_unexpected_handler_error_is_one_line_unless_debug(shell_home, monkeypatch, capsys):
    from briefcase.cli import features_commands

    def explode(*args, **kwargs):
        raise RuntimeError("something\nbroke")

    monkeypatch.setattr(features_commands, "load_catalog", explode)
    assert main(["features"]) == 1
    err = capsys.readouterr().err
    assert err == "error: RuntimeError: something broke\nFix: briefcase-ai --debug features   # shows the full traceback\n"  # noqa: E501
    with pytest.raises(RuntimeError):
        main(["--debug", "features"])
