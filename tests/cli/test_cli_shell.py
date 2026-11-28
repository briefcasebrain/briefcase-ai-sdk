"""``briefcase-ai status``, ``features``, the guided setup and ``completion``, in a temp HOME."""

import io
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from briefcase.cli import _discovery, _ux, completion_commands, setup_commands, status_commands
from briefcase.cli.__main__ import build_parser, main
from tests.cli.parser_tree import files_under

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths")


def fake_module(name, group="enforce", status=None, commands=()):
    module = types.SimpleNamespace(register=lambda sub: None)
    if status is not None:
        module.status = status
    return _discovery.CommandModule(name=name, module=module, group=group, commands=list(commands))


def connect_claude(fake_tool):
    """Connect a stand-in Claude Code for real (files and journal in the temp HOME)."""
    fake_tool("claude", "2.1.330 (Claude Code)")
    assert main(["connect", "--yes"]) == 0


# --- _ux --------------------------------------------------------------------------

def test_next_and_fail_helpers(capsys):
    _ux.print_next("briefcase-ai connect")
    _ux.print_next("briefcase-ai receive --detach", "nothing is listening")
    assert _ux.fail("no receiver is running", fix="briefcase-ai receive --detach") == 1
    assert _ux.fail("bad flag", code=2) == 2
    assert _ux.print_json({"a": 1}) == 0
    captured = capsys.readouterr()
    assert captured.out == ("Next: briefcase-ai connect\n"
                            "Next: briefcase-ai receive --detach  # nothing is listening\n"
                            '{\n  "a": 1\n}\n')
    assert captured.err == ("error: no receiver is running\nFix: briefcase-ai receive --detach\n"
                            "error: bad flag\n")


def test_colour_only_on_a_terminal_and_never_with_no_color(monkeypatch):
    tty = types.SimpleNamespace(isatty=lambda: True)
    pipe = types.SimpleNamespace(isatty=lambda: False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert _ux.style("x", "green", tty) == "\033[32mx\033[0m"
    assert _ux.style("x", "green", pipe) == "x"
    assert _ux.style("x", "no-such-style", tty) == "x"
    monkeypatch.setenv("NO_COLOR", "1")
    assert _ux.style("x", "green", tty) == "x" and not _ux.use_color(tty)
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert not _ux.use_color(tty)


def test_interactive_needs_a_terminal_and_no_opt_out(monkeypatch):
    monkeypatch.delenv("BRIEFCASE_NO_INPUT", raising=False)
    _ux.set_no_input(False)
    monkeypatch.setattr(_ux, "is_tty", lambda stream=None: True)
    assert _ux.is_interactive()
    monkeypatch.setenv("BRIEFCASE_NO_INPUT", "1")
    assert not _ux.is_interactive()
    monkeypatch.setenv("BRIEFCASE_NO_INPUT", "0")
    assert _ux.is_interactive()
    _ux.set_no_input(True)
    assert not _ux.is_interactive()
    _ux.set_no_input(False)
    monkeypatch.setattr(_ux, "is_tty", lambda stream=None: False)
    assert not _ux.is_interactive() and _ux.confirm("write?") is None and _ux.confirm("write?", assume_yes=True)


def test_choose_accepts_number_key_or_prefix(monkeypatch, capsys):
    options = [("coding-tools", "A"), ("app", "B"), ("skip", "C")]
    for typed, expected in (("2\n", "app"), ("skip\n", "skip"), ("c\n", "coding-tools"), ("x\n9\n1\n", "coding-tools"),
                            ("", None), ("x\ny\nz\n", None)):
        monkeypatch.setattr(sys, "stdin", io.StringIO(typed))
        assert _ux.choose("Pick", options) == expected, typed
    capsys.readouterr()


# --- status -----------------------------------------------------------------------

@pytest.mark.parametrize("options", [["--json"], ["--all", "--json"]])
def test_status_json_shape_on_a_fresh_machine(shell_home, capsys, options):
    assert main(["status", *options]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == ""
    assert set(report) == {"schema_version", "version", "home", "items", "summary", "next", "problems"}
    assert report["schema_version"] == 1 and report["home"] == str(shell_home / ".briefcase")
    items = report["items"]
    # Contract: the reserved core ids come first, in order; packages may add hook rows
    # after them (e.g. eval_commands' datasets row), so the list is not pinned.
    assert [item["id"] for item in items[:4]] == ["connect", "receive", "mcp", "records"]
    extras = items[4:]
    for item in items:
        # `data` is optional on hook rows (docs/features/README.md); core rows always have it.
        assert {"id", "title", "group", "state", "detail", "next", "source"} <= set(item) <= {
            "id", "title", "group", "state", "detail", "next", "source", "data"}
        assert item["state"] == ("on" if item["id"] == "pricing-data" else "off")
    assert all(item["source"] == "core" for item in items[:4])
    ids = [item["id"] for item in items]
    assert {"drift-baseline", "replay-recorded", "pricing-data"} <= set(ids)
    assert report["problems"] == []
    assert len(set(ids)) == len(ids)
    for item in extras:
        assert item["source"] != "core" and item["id"] not in status_commands.CORE_IDS
        assert item["title"] and item["detail"] and item["group"] in _discovery.GROUP_KEYS
        assert item["next"] is None or item["next"].startswith("briefcase-ai ")
    assert report["summary"] == {"on": 1, "off": len(items) - 1, "attention": 0}
    assert report["next"] == {"command": "briefcase-ai exec -- python your_app.py",
                              "reason": "no coding tool found here; capture a Python app instead"}
    assert items[3]["data"]["sdk"] == {"kind": "console", "target": "stderr", "kept": False,
                                       "problem": None}


def test_status_screen_ends_with_exactly_one_next_line(shell_home, capsys):
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    lines = out.rstrip("\n").splitlines()
    assert lines[0].startswith("briefcase-ai status  (briefcase-ai ")
    assert "Connect" in lines and "Capture" in lines
    assert re.fullmatch(r"1 on · \d+ off · 0 need attention", lines[-2])
    assert lines[-1].startswith("Next: briefcase-ai exec -- python your_app.py  # ")
    assert out.count("Next:") == 1 and "\033[" not in out


def test_status_follows_connect_then_receiver_then_mcp(shell_home, fake_tool, monkeypatch, capsys):
    fake_tool("claude", "2.1.330 (Claude Code)")
    report = status_commands.collect([])
    assert report["items"][0]["detail"] == "none connected; detected on this machine: Claude Code"
    assert report["next"]["command"] == "briefcase-ai connect"

    assert main(["connect", "--yes", "--no-mcp"]) == 0
    capsys.readouterr()
    report = status_commands.collect([])
    states = {item["id"]: item["state"] for item in report["items"]}
    assert states == {"connect": "on", "receive": "off", "mcp": "off", "records": "off"}
    assert report["items"][0]["data"]["connected"] == ["claude-code"]
    assert report["next"] == {"command": "briefcase-ai receive --detach",
                              "reason": "tools are connected but nothing is listening"}

    # A receiver answering on the default endpoint (started in the foreground: no pidfile).
    from briefcase.receiver import daemon

    health = {"service": "briefcase-receiver", "protobuf": False, "redaction": "on",
              "stats": {"requests": 3, "records": 2, "rejected": 1}}
    monkeypatch.setattr(daemon, "probe", lambda endpoint, timeout=1.0: health)
    report = status_commands.collect([])
    receive = report["items"][1]
    assert receive["state"] == "on" and receive["data"]["stats"] == {"requests": 3, "records": 2, "rejected": 1}
    assert "3 requests, 2 records, 1 rejected" in receive["detail"]
    assert report["items"][3]["state"] == "off"          # a foreground receiver prints to the console
    assert report["next"]["command"] == "briefcase-ai mcp install"

    assert main(["mcp", "install", "--yes"]) == 0
    capsys.readouterr()
    report = status_commands.collect([])
    assert {item["id"]: item["state"] for item in report["items"]}["mcp"] == "on"
    assert report["next"]["command"] == "briefcase-ai features"


def test_status_flags_a_tool_whose_settings_were_removed(shell_home, fake_tool, capsys):
    connect_claude(fake_tool)
    (shell_home / ".claude" / "settings.json").write_text("{}")
    report = status_commands.collect([])
    assert report["items"][0]["state"] == "attention"
    assert report["items"][0]["data"]["drifted"] == ["claude-code"]
    assert report["next"]["command"] == "briefcase-ai connect claude-code"


def test_status_reports_where_python_records_go(shell_home, tmp_path):
    target = tmp_path / "out" / "runs.jsonl"
    target.parent.mkdir()
    target.write_text('{"a": 1}\n')
    item = status_commands.records_item({"data": {}}, {"BRIEFCASE_EXPORTER": str(target)})
    assert item["state"] == "on" and item["data"]["sdk"]["kind"] == "file"
    assert item["data"]["sdk"]["file"]["bytes"] == 9 and "runs.jsonl (9 B)" in item["detail"]

    item = status_commands.records_item({"data": {}}, {"BRIEFCASE_EXPORTER": "https://collector.example.com/v1/d"})
    assert item["state"] == "on" and item["data"]["sdk"]["kind"] == "https"
    assert "collector.example.com" in item["detail"]

    item = status_commands.records_item({"data": {}}, {"BRIEFCASE_EXPORTER": "carrier-pigeon"})
    assert item["state"] == "attention" and "carrier-pigeon" in item["detail"]
    report_next = status_commands.choose_next([item])
    assert report_next["command"] == "briefcase-ai features exporters"

    item = status_commands.records_item({"data": {"running": True, "to": str(target)}}, {})
    assert item["state"] == "on" and item["data"]["receiver"]["receiving"] is True
    assert item["detail"].startswith("coding tools -> ")
    assert "Python apps -> the console (stderr), not kept" in item["detail"]


def test_status_uses_the_sdk_environment_check_when_it_exists(shell_home, monkeypatch):
    from briefcase import _observe

    monkeypatch.setattr(_observe, "check_environment", lambda env: "BRIEFCASE_EXPORTER is\nwrong", raising=False)
    monkeypatch.setattr(_observe, "_exporter_from_env", lambda env: pytest.fail("an exporter was built"))
    assert status_commands.sdk_destination({}) == {"kind": "unknown", "target": None, "kept": False,
                                                   "problem": "BRIEFCASE_EXPORTER is wrong"}
    monkeypatch.setattr(_observe, "check_environment", lambda env: None, raising=False)
    monkeypatch.setattr(_observe, "_exporter_from_env", lambda env: _observe.ConsoleExporter())
    assert status_commands.sdk_destination({})["kind"] == "console"


def test_status_hooks_add_rows_and_drive_next(shell_home, capsys):
    modules = [
        fake_module("policy_commands", "enforce", lambda: {
            "id": "policy", "state": "off", "detail": "no policy file", "next": "briefcase-ai policy init"}),
        fake_module("budget_commands", "enforce", lambda: [
            {"id": "budget", "title": "Budget", "state": "attention", "detail": "92% of 20 USD used",
             "next": "briefcase-ai budget show", "data": {"used": 18.4}},
            {"id": "budget-alerts", "state": "on", "detail": "e-mail", "next": None}]),
        fake_module("verify_commands", "verify", lambda: {"id": "verify", "state": "on", "detail": "chain intact"}),
        fake_module("raises_commands", "review", lambda: 1 / 0),
        fake_module("wrong_commands", "review", lambda: {"id": "x", "state": "sideways"}),
        fake_module("reserved_commands", "review", lambda: {"id": "connect", "state": "on", "detail": "nope"}),
        fake_module("silent_commands", "review", lambda: None),
        fake_module("nohook_commands", "review"),
        _discovery.CommandModule(name="unimportable_commands", error="import failed: X"),
    ]
    report = status_commands.collect(modules)
    ids = [item["id"] for item in report["items"]]
    assert ids == ["connect", "receive", "mcp", "records", "policy", "budget", "budget-alerts", "verify"]
    budget = report["items"][5]
    assert budget == {"id": "budget", "title": "Budget", "state": "attention", "detail": "92% of 20 USD used",
                      "next": "briefcase-ai budget show", "data": {"used": 18.4}, "group": "enforce",
                      "source": "budget_commands"}
    assert report["items"][4]["title"] == "policy" and report["items"][7]["next"] is None
    assert report["summary"] == {"on": 2, "off": 5, "attention": 1}
    # An item needing attention outranks one that is merely off.
    assert report["next"] == {"command": "briefcase-ai budget show", "reason": "Budget needs attention"}
    assert len(report["problems"]) == 3
    assert "raises_commands.status() raised ZeroDivisionError" in report["problems"][0]

    status_commands.render(report)
    out = capsys.readouterr().out
    assert "\nEnforce\n" in out and "\nVerify\n" in out and "attention Budget" in out
    assert out.count("Next:") == 1

    quiet = [modules[0]]
    report = status_commands.collect(quiet)
    assert report["next"]["command"] == "briefcase-ai exec -- python your_app.py"   # core steps come first
    connected = [dict(item, state="on", next=None) for item in report["items"][:4]] + report["items"][4:]
    assert status_commands.choose_next(connected) == {"command": "briefcase-ai policy init", "reason": "policy is off"}


def test_status_survives_an_unreadable_piece(shell_home, monkeypatch):
    monkeypatch.setattr(status_commands, "connect_rows", lambda: 1 / 0)
    report = status_commands.collect([])
    assert report["items"][0]["state"] == "attention" and "could not be checked" in report["items"][0]["detail"]
    assert [item["id"] for item in report["items"]] == ["connect", "receive", "mcp", "records"]


# --- features ---------------------------------------------------------------------

def test_features_catalog_text(shell_home, capsys):
    assert main(["features"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    from briefcase.features import load_catalog

    catalog = load_catalog()
    assert lines[0] == f"briefcase-ai features · {len(catalog.features)} features · 5 groups"
    for verb, items in catalog.groups():
        assert f"{verb.capitalize()} ({len(items)})" in lines
    feature = catalog.get("connect")
    index = lines.index(f"  {feature.title}  [{feature.status}]  id: connect")
    assert lines[index + 1].strip() == feature.one_line and lines[index + 2].strip() == "$ briefcase-ai connect"
    assert lines[-1].startswith("Next: briefcase-ai features <id>") and out.count("Next:") == 1


def test_features_filter_and_json_shape(shell_home, capsys):
    assert main(["features", "--verb", "enforce"]) == 0
    out = capsys.readouterr().out
    assert "· 1 group" in out.splitlines()[0] and "Connect (" not in out

    assert main(["features", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"schema_version", "count", "group_count", "groups"}
    assert data["group_count"] == 5 and data["count"] == sum(group["count"] for group in data["groups"])
    feature = data["groups"][0]["features"][0]
    assert {"id", "title", "verb", "one_line", "command", "docs_slug", "status", "requires_extra",
            "verified_against", "docs_file", "docs_url"} <= set(feature)

    assert main(["features", "--verb", "verify", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [group["verb"] for group in data["groups"]] == ["verify"] and data["count"] == data["groups"][0]["count"]


def test_one_feature_shows_docs_and_whether_it_is_active_here(shell_home, fake_tool, capsys):
    assert main(["features", "connect"]) == 0
    out = capsys.readouterr().out
    assert "Active here:   no (none connected" in out
    assert "Docs:          docs/integrations/README.md" in out
    assert "https://github.com/briefcasebrain/briefcase-ai-sdk/blob/main/docs/integrations/README.md" in out
    assert out.rstrip().splitlines()[-1] == "Next: briefcase-ai connect"

    connect_claude(fake_tool)
    capsys.readouterr()
    assert main(["features", "connect", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == "connect" and data["active"]["state"] == "on" and data["active"]["status_id"] == "connect"
    assert main(["features", "mcp-server", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["active"] == {
        "state": "on", "detail": "registered with Claude Code", "status_id": "mcp"}

    assert main(["features", "capture-decorator"]) == 0
    out = capsys.readouterr().out
    assert "not tracked by `briefcase-ai status`" in out and "In Python:" in out and "@briefcase.capture" in out
    assert "Start with:" not in out


def test_unknown_feature_suggests_the_closest_id(shell_home, capsys):
    assert main(["features", "recieve"]) == 2
    assert capsys.readouterr().err == "error: no feature with id 'recieve'\nFix: briefcase-ai features receive\n"


# --- guided setup -----------------------------------------------------------------

@pytest.fixture
def at_terminal(shell_home, monkeypatch):
    """Pretend a person is at a terminal; ``type_in("1\\ny\\n")`` scripts what they type."""
    monkeypatch.setattr(_ux, "is_tty", lambda stream=None: True)
    monkeypatch.setenv("NO_COLOR", "1")                      # plain text, so output can be compared

    def type_in(text):
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    return type_in


def setup_record(home):
    return json.loads((home / ".briefcase" / "setup.json").read_text())


def test_bare_briefcase_without_a_terminal_prints_help_and_status_and_exits_zero(shell_home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))       # a prompt would hit end of input
    assert main([]) == 0
    out = capsys.readouterr().out
    assert out.index("Start here:") < out.index("Connect:") < out.index("briefcase-ai status  (") < out.index("Next:")
    assert out.count("Next:") == 1 and "What are you setting up?" not in out
    assert files_under(shell_home) == []                      # not remembered: nobody answered


def test_no_input_flag_and_env_never_prompt_even_on_a_terminal(at_terminal, shell_home, monkeypatch, capsys):
    at_terminal("")
    assert main(["--no-input"]) == 0
    assert "What are you setting up?" not in capsys.readouterr().out
    monkeypatch.setenv("BRIEFCASE_NO_INPUT", "1")
    assert main([]) == 0 and main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "What are you setting up?" not in out and "needs a terminal" in out
    assert files_under(shell_home) == []


def test_setup_skip_is_remembered_and_the_guide_is_not_repeated(at_terminal, shell_home, capsys):
    at_terminal("4\n")
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "What are you setting up?" in out and "Skipped." in out and out.count("Next:") == 1
    record = setup_record(shell_home)
    assert record["outcome"] == "skipped" and record["persona"] is None and record["version"] == 1
    assert files_under(shell_home) == [".briefcase/setup.json"]

    at_terminal("")                                           # a second prompt would fail on EOF
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "What are you setting up?" not in out and "briefcase-ai status  (" in out and "Start here:" in out

    at_terminal("s\n")                                        # `briefcase-ai setup` always runs the guide
    assert main(["setup"]) == 0
    assert "What are you setting up?" in capsys.readouterr().out


def test_setup_interrupted_is_not_remembered(at_terminal, shell_home, capsys):
    at_terminal("")                                           # end of input at the question
    assert main([]) == 1
    assert "Setup was not finished" in capsys.readouterr().out
    assert files_under(shell_home) == []
    at_terminal("4\n")
    assert main([]) == 0 and "What are you setting up?" in capsys.readouterr().out


def test_setup_coding_tools_connects_then_starts_the_receiver(at_terminal, shell_home, fake_tool, monkeypatch, capsys):
    from briefcase.cli import receive_commands

    fake_tool("claude", "2.1.330 (Claude Code)")
    started = []

    def start_detached(args, out=print):
        started.append(vars(args))
        out("receiver started (stand-in)")
        return 0

    monkeypatch.setattr(receive_commands, "start_detached", start_detached)
    at_terminal("1\ny\ny\n")
    assert main([]) == 0
    out = capsys.readouterr().out
    assert out.index("Step 1 of 2 · Connect your coding tools") < out.index("== Claude Code ==")
    assert out.index("Write 1 file(s) and run 1 command(s)?") < out.index("Step 2 of 2 · Start the receiver")
    assert "receiver started (stand-in)" in out and out.count("Next:") == 1
    settings = json.loads((shell_home / ".claude" / "settings.json").read_text())
    assert settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"
    assert "OTEL_LOG_USER_PROMPTS" not in settings["env"]     # content stays off
    assert started == [{"port": 4318, "host": "127.0.0.1", "to": None, "keep_content": False,
                        "metrics": False, "token": None, "max_body_mb": 8.0}]
    assert setup_record(shell_home)["persona"] == "coding-tools"
    # The status screen at the end reflects what was just done.
    # (column width depends on the longest row title, which hook rows can extend)
    assert re.search(r"\bon +Coding tools +Claude Code connected", out)


def test_setup_coding_tools_writes_nothing_when_the_user_says_no(at_terminal, shell_home, fake_tool, monkeypatch,
                                                                 capsys):
    from briefcase.cli import receive_commands

    fake_tool("claude", "2.1.330 (Claude Code)")
    monkeypatch.setattr(receive_commands, "start_detached", lambda *a, **k: pytest.fail("receiver started"))
    at_terminal("1\nn\n")
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "Aborted; nothing was changed." in out and "No tool is connected" in out
    assert files_under(shell_home) == [".briefcase/setup.json"]
    assert setup_record(shell_home)["outcome"] == "finished"

    # Connected, but the receiver is declined: nothing is started.
    at_terminal("1\ny\nn\n")
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "Start it when you are ready: briefcase-ai receive --detach" in out
    assert (shell_home / ".claude" / "settings.json").exists()


def test_setup_coding_tools_with_no_tool_installed(at_terminal, shell_home, capsys):
    at_terminal("1\n")
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "No supported coding tool was detected" in out and "briefcase-ai connect list" in out
    assert files_under(shell_home) == [".briefcase/setup.json"]


def test_setup_app_path_offers_a_dry_run(at_terminal, shell_home, monkeypatch, capsys):
    from briefcase.cli import exec_commands

    calls = []

    def run(command, **options):
        calls.append((command, options))
        return 0

    monkeypatch.setattr(exec_commands, "run", run)
    at_terminal("2\nn\n")
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "briefcase-ai exec -- python your_app.py" in out and "Step 2 of 2 · Keep the records" in out
    assert calls == [] and out.count("Next:") == 1

    at_terminal("2\n\n")                                      # Enter accepts the default: run it
    assert main(["setup"]) == 0
    assert calls == [([sys.executable, "-c", setup_commands.DEMO_PROGRAM],
                      {"to": "console", "replace_process": False})]
    assert "decision record for greet('world')" in capsys.readouterr().out
    assert setup_record(shell_home)["persona"] == "app"
    assert files_under(shell_home) == [".briefcase/setup.json"]


def test_setup_app_dry_run_for_real(at_terminal, shell_home, monkeypatch, capfd):
    """The dry run starts a real child interpreter through ``briefcase-ai exec``."""
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    at_terminal("2\ny\n")
    assert main(["setup"]) == 0
    captured = capfd.readouterr()
    records = [json.loads(line) for line in captured.err.splitlines() if line.startswith("{")]
    assert len(records) == 1 and records[0]["function_name"] == "greet"
    assert records[0]["outputs"] == {"result": "'hello world'"}
    # Apple's Python writes bytecode caches under ~/Library/Caches; they are not the SDK's files.
    written = [f for f in files_under(shell_home) if not f.startswith("Library/Caches/com.apple.python")]
    assert written == [".briefcase/setup.json"]


def test_setup_team_path_offers_real_steps_when_policy_is_discovered(at_terminal, shell_home, capsys):
    at_terminal("3\nn\nn\n")                      # team; decline the rollout pack and the policy
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "Step 1 of 4 · Collect records in one place" in out
    managed = out.split("Roll out to every developer machine", 1)[1].split("Step 3", 1)[0]
    assert "briefcase-ai connect --managed --out ./briefcase-rollout" in managed and "Coming" not in managed
    policy = out.split("Decide what coding tools may run", 1)[1].split("Step 4", 1)[0]
    assert "briefcase-ai policy init" in policy and "Coming" not in policy
    assert "Coming" not in out                 # everything is discovered, "Cap spend" included
    assert "briefcase-ai budget set daily --usd 20" in out.split("Cap spend", 1)[1]
    assert setup_record(shell_home)["persona"] == "team"
    assert files_under(shell_home) == [".briefcase/setup.json"]          # declined: nothing written


def test_setup_team_path_marks_missing_commands_as_coming(shell_home, capsys):
    setup_commands.path_team([fake_module("other_commands", "enforce", commands=["other"])])
    out = capsys.readouterr().out
    assert out.count("Coming: there is no command for this in the version installed here.") == 3
    assert "Today:  briefcase-ai connect --dry-run" in out
    assert "Today:  briefcase-ai features controls-gateway" in out
    assert files_under(shell_home) == []


def test_setup_team_path_writes_the_rollout_pack_only_after_a_yes(
        at_terminal, shell_home, tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    modules = [fake_module("policy_commands", "enforce", commands=["policy"])]
    at_terminal("n\nn\n")
    setup_commands.path_team(modules)
    assert not (work / "briefcase-rollout").exists()
    at_terminal("y\nn\n")
    setup_commands.path_team(modules)
    out = capsys.readouterr().out
    assert "Wrote" in out and (work / "briefcase-rollout" / "manifest.json").is_file()
    assert files_under(shell_home) == []


def test_setup_team_path_policy_step_calls_policy_init_with_its_own_confirmation(
        at_terminal, shell_home, monkeypatch, capsys):
    modules = [fake_module("policy_commands", "enforce", commands=["policy"])]
    at_terminal("n\ny\nn\n")              # skip rollout; yes to policy; no to its write prompt
    setup_commands.path_team(modules)
    out = capsys.readouterr().out
    assert "Aborted; nothing was changed." in out
    assert files_under(shell_home) == []
    at_terminal("n\ny\ny\n")
    setup_commands.path_team(modules)
    assert (shell_home / ".briefcase" / "policy.toml").is_file()


# --- completion -------------------------------------------------------------------

@pytest.mark.parametrize("shell", completion_commands.SHELLS)
def test_completion_covers_every_command(shell_home, shell, capsys):
    assert main(["completion", shell]) == 0
    script = capsys.readouterr().out
    parser = build_parser()
    for command in parser.subparsers.choices:
        assert command in script
    for word in ("capture-content", "detach", "submit", "enforce", "no-input"):
        assert word in script
    assert files_under(shell_home) == []


def test_completion_tree_matches_the_parser():
    nodes = {node.key: node for node in completion_commands.tree(build_parser())}
    assert {"briefcase-ai", "briefcase-ai run submit", "briefcase-ai mcp link", "briefcase-ai features"} <= set(nodes)
    root = nodes["briefcase-ai"]
    assert {name for name, _ in root.subcommands} == set(build_parser().subparsers.choices)
    connect = nodes["briefcase-ai connect"]
    scope = next(option for option in connect.options if "--scope" in option.flags)
    assert scope.choices == ["user", "project"] and scope.takes_value
    yes = next(option for option in connect.options if "--yes" in option.flags)
    assert yes.flags == ["--yes", "-y"] and not yes.takes_value
    assert nodes["briefcase-ai completion"].positional_choices == ["bash", "zsh", "fish"]
    assert all("'" not in help_ and '"' not in help_ for _, help_ in root.subcommands)


def test_bash_completion_is_valid_and_completes(shell_home, tmp_path):
    bash = shutil.which("bash", path="/usr/bin:/bin:/usr/local/bin")
    if not bash:
        pytest.skip("bash is not installed")
    script = tmp_path / "briefcase.bash"
    script.write_text(completion_commands.script("bash"))
    assert subprocess.run([bash, "-n", str(script)], capture_output=True, text=True).returncode == 0

    def complete(*words):
        program = (f'source "{script}"; COMP_WORDS=({" ".join(words)}); COMP_CWORD=$(( ${{#COMP_WORDS[@]}} - 1 )); '
                   '_briefcase_ai; printf "%s\\n" "${COMPREPLY[@]}"')
        result = subprocess.run([bash, "--norc", "--noprofile", "-c", program], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.split()

    assert set(complete("briefcase-ai", "st")) == {"status"}
    assert set(complete("briefcase-ai", "run", "s")) == {"submit", "stop"}
    assert complete("briefcase-ai", "connect", "--sc") == ["--scope"]
    assert complete("briefcase-ai", "connect", "--scope", '""') == ["user", "project"]
    assert complete("briefcase-ai", "completion", "z") == ["zsh"]
    assert complete("briefcase-ai", "connect", "--endpoint", '""') == []      # a free value: fall back to files
    assert complete("briefcase-ai", "exec", "--", "pyth") == []               # the rest is another program


@pytest.mark.parametrize("shell,check", [("zsh", ["-n"]), ("fish", ["--no-execute"])])
def test_zsh_and_fish_completion_parse_when_the_shell_is_installed(shell, check, tmp_path):
    binary = shutil.which(shell, path="/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
    if not binary:
        pytest.skip(f"{shell} is not installed")
    script = tmp_path / f"briefcase.{shell}"
    script.write_text(completion_commands.script(shell))
    result = subprocess.run([binary, *check, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_saved_app_choice_controls_text_only(shell_home, fake_tool, capsys, monkeypatch):
    fake_tool("cursor", "1.0")
    assert main(["status", "--json"]) == 0
    baseline = json.loads(capsys.readouterr().out)
    setup_commands.mark_done("app", "finished")
    for args in (["status", "--json"], ["status", "--all", "--json"]):
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out) == baseline
    assert main(["status", "--all"]) == 0
    assert "Coding tools" in capsys.readouterr().out
    assert main([]) == 0
    assert capsys.readouterr().out.rstrip().endswith("Next: briefcase-ai features capture-decorator")
    for dest, detail in [("console", "stderr"), ("memory", "until the process exits"),
                         ("runs.jsonl", "Configured to append to"), ("bad-destination", "Attention:"),
                         ("https://127.0.0.1:1", "Configured to send to https://127.0.0.1:1")]:
        monkeypatch.setenv("BRIEFCASE_EXPORTER", dest)
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert out.count("Next:") == 1 and detail in out
        assert "Cursor" not in out and "saved" not in out
        command = out.split("Next: briefcase-ai ")[1].strip().split()
        assert main(command) == 0
        capsys.readouterr()


def test_otlp_app_status_describes_only_configuration(shell_home, monkeypatch, capsys):
    setup_commands.mark_done("app", "finished")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Configured to send to http://127.0.0.1:1" in out
    assert "saved" not in out and "delivered" not in out


def test_setup_choice_changes_and_invalid_state(shell_home, capsys):
    for persona, heading, feature in [("team", "Team rollout", "managed-rollout"),
                                      ("coding-tools", "Coding tools", "connect")]:
        setup_commands.mark_done(persona, "finished")
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert out.startswith(heading) and out.rstrip().endswith(f"Next: briefcase-ai features {feature}")
    for value in ["{", "[]", '{"version":1,"outcome":"finished","persona":[]}',
                  '{"version":1,"outcome":"skipped","persona":"app"}']:
        setup_commands.setup_path().write_text(value)
        assert main(["status"]) == 0
        assert capsys.readouterr().out.startswith("briefcase-ai status")
