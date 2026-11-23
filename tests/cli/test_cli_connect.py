"""CLI surface for connect / receive / mcp / hook / doctor --connect, in a temp HOME."""

import json
import os
import sys

import pytest

from briefcase.cli import connect_commands, mcp_commands, receive_commands
from briefcase.cli.__main__ import build_parser, main
from briefcase.connect import edits, engine
from briefcase.connect import mcp as payloads

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for directory in (home, tmp_path / "project", tmp_path / "bin"):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv("BRIEFCASE_HOME", raising=False)
    monkeypatch.delenv("BRIEFCASE_RECEIVER_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path / "project")
    # No test may start a real background receiver or find one on :4318.
    from briefcase.receiver import daemon
    monkeypatch.setattr(daemon, "probe", lambda endpoint, timeout=1.0: None)
    return home


def fake_bin(tmp_path, name, output):
    path = tmp_path / "bin" / name
    path.write_text(f"#!/bin/sh\necho '{output}'\n")
    os.chmod(path, 0o755)


def files(home):
    return sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file())


# --- parser -------------------------------------------------------------------

def test_parser_accepts_the_integration_surface():
    parser = build_parser()
    args = parser.parse_args(["connect"])
    assert args.tools == [] and args.scope == "user" and args.mcp is True and args.endpoint is None
    assert not args.capture_content and not args.connect_dry_run and not args.undo and not args.yes
    args = parser.parse_args(["connect", "claude-code", "cursor", "--scope", "project", "--endpoint", "http://h:1",
                              "--headers", "A=b", "--headers", "C=d", "--no-mcp", "--capture-content",
                              "--dry-run", "--yes"])
    assert args.tools == ["claude-code", "cursor"] and args.headers == ["A=b", "C=d"] and args.mcp is False
    assert args.connect_dry_run and args.capture_content and args.yes and args.scope == "project"
    assert parser.parse_args(["connect", "--all", "--undo"]).undo
    for words in (["connect", "list"], ["connect", "status"]):
        assert parser.parse_args(words).func is connect_commands.cmd_connect

    args = parser.parse_args(["receive"])
    assert (args.port, args.host, args.to, args.keep_content, args.token) == (4318, "127.0.0.1", None, False, None)
    args = parser.parse_args(["receive", "--port", "0", "--host", "0.0.0.0", "--to", "out.jsonl",
                              "--keep-content", "--token", "t", "--metrics", "--detach"])
    assert args.detach and args.metrics and args.to == "out.jsonl"
    assert parser.parse_args(["receive", "--stop"]).stop and parser.parse_args(["receive", "--status"]).status
    with pytest.raises(SystemExit):
        parser.parse_args(["receive", "--stop", "--detach"])

    assert parser.parse_args(["mcp", "install", "cursor", "--scope", "project"]).func is mcp_commands.cmd_mcp_install
    assert parser.parse_args(["mcp", "link", "cursor"]).tool == "cursor"
    assert parser.parse_args(["doctor", "--connect"]).connect is True
    assert parser.parse_args(["doctor"]).connect is False
    assert parser.parse_args(["hook", "cursor"]).func is not None


def test_existing_commands_still_parse():
    parser = build_parser()
    assert parser.parse_args(["run", "list"]).func is not None
    assert parser.parse_args(["--dry-run", "run", "submit", "n", "--repository", "r", "--dataset", "d"]).dry_run


# --- connect ------------------------------------------------------------------

def test_connect_with_nothing_detected_explains_itself(home, capsys):
    assert main(["connect", "--yes"]) == 1
    assert "No supported coding tool was detected" in capsys.readouterr().out
    assert not [f for f in files(home) if not f.startswith(".briefcase")]


def test_connect_happy_path_detects_writes_and_tells_you_how_to_undo(home, capsys):
    (home / ".claude").mkdir()
    (home / ".cursor").mkdir()
    assert main(["connect", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Detected: Claude Code, Cursor" in out
    assert "+++ " + str(home / ".claude" / "settings.json") in out  # the diff was shown
    assert "wrote  " + str(home / ".claude" / "settings.json") in out
    assert "Undo any time with: briefcase-ai connect --undo" in out
    assert "briefcase-ai receive --detach" in out
    assert "briefcase-ai[receiver]" not in out  # neither tool needs protobuf
    env = json.loads((home / ".claude" / "settings.json").read_text())["env"]
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json" and "OTEL_LOG_USER_PROMPTS" not in env
    assert (home / ".cursor" / "hooks.json").exists() and (home / ".cursor" / "mcp.json").exists()

    assert main(["connect", "--yes"]) == 0  # idempotent
    assert "Nothing to write" in capsys.readouterr().out

    assert main(["connect", "status"]) == 0
    status = capsys.readouterr().out
    assert "claude-code      connected" in status and "cursor           connected" in status
    assert "not running" in status

    assert main(["connect", "--undo"]) == 0
    assert not (home / ".claude" / "settings.json").exists() and not (home / ".cursor" / "hooks.json").exists()
    assert main(["connect", "--undo"]) == 0
    assert "nothing to undo" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["connect", "--all", "--dry-run"], ["--dry-run", "connect", "--all"],
                                  ["connect", "--all", "--dry-run", "--yes", "--capture-content"],
                                  ["mcp", "install", "cursor", "claude-code", "--dry-run"]])
def test_dry_run_writes_nothing(home, capsys, argv):
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "Dry run:" in out and "Nothing was changed" in out
    assert not [f for f in files(home) if not f.startswith(".briefcase")]
    assert edits.load_journal()["files"] == {}


def test_connect_refuses_to_write_without_a_terminal_or_yes(home, capsys, monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["connect", "claude-code"]) == 1
    assert "re-run with --yes" in capsys.readouterr().err
    assert not (home / ".claude").exists()


def test_connect_asks_once_and_respects_no(home, capsys):
    from argparse import Namespace

    from briefcase.connect.adapters import registry

    args = build_parser().parse_args(["connect", "claude-code", "--no-mcp"])
    questions = []

    def ask(question):
        questions.append(question)
        return "n"

    assert connect_commands.run_connect(args, registry(), ask=ask) == 1
    assert len(questions) == 1 and questions[0].startswith("Write 1 file(s) and run 0 command(s)?")
    assert not (home / ".claude").exists()

    answers = iter(["", "n"])  # Enter = yes to the write, no to starting the receiver
    assert connect_commands.run_connect(args, registry(), ask=lambda q: next(answers)) == 0
    assert (home / ".claude" / "settings.json").exists()
    assert isinstance(args, Namespace)


def test_connect_offers_to_start_the_receiver(home, capsys, monkeypatch):
    started = []
    monkeypatch.setattr(receive_commands, "start_detached", lambda args, out=print: started.append(args) or 0)
    assert main(["connect", "claude-code", "--no-mcp", "--yes", "--start"]) == 0
    assert len(started) == 1 and started[0].port == 4318 and started[0].keep_content is False
    assert "Restart the connected tools" in capsys.readouterr().out


def test_capture_content_warns_and_is_the_only_way_content_is_enabled(home, capsys):
    assert main(["connect", "claude-code", "codex", "vscode-copilot", "gemini-cli", "--scope", "project",
                 "--no-mcp", "--yes"]) == 0
    captured = capsys.readouterr()
    assert "warning" not in captured.err
    env = json.loads((home / ".claude" / "settings.json").read_text())["env"]
    assert not [k for k in env if k.startswith("OTEL_LOG_")]
    assert "log_user_prompt = false" in (home / ".codex" / "config.toml").read_text()
    (code_settings,) = home.rglob("Code/User/settings.json")
    assert "github.copilot.chat.otel.captureContent" not in json.loads(code_settings.read_text())
    gemini = json.loads((home.parent / "project" / ".gemini" / "settings.json").read_text())
    assert gemini["telemetry"]["logPrompts"] is False
    # Codex and Copilot need the protobuf extra, and connect says exactly what to run (if it is missing).
    from briefcase.receiver import otlp
    assert ("pip install 'briefcase-ai[receiver]'" in captured.out) or otlp.protobuf_available()

    assert main(["connect", "claude-code", "codex", "--no-mcp", "--yes", "--capture-content"]) == 0
    captured = capsys.readouterr()
    assert "warning: --capture-content" in captured.err
    env = json.loads((home / ".claude" / "settings.json").read_text())["env"]
    assert env["OTEL_LOG_USER_PROMPTS"] == "1" and env["OTEL_LOG_TOOL_DETAILS"] == "1"
    assert "log_user_prompt = true" in (home / ".codex" / "config.toml").read_text()


def test_connect_tells_the_user_the_exact_pip_command_when_protobuf_is_missing(home, capsys, monkeypatch):
    from briefcase.receiver import otlp

    monkeypatch.setattr(otlp, "protobuf_available", lambda: False)
    assert main(["connect", "codex", "--no-mcp", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Codex CLI send(s) OTLP as protobuf" in out and "  pip install 'briefcase-ai[receiver]'" in out
    assert main(["connect", "claude-code", "--no-mcp", "--yes"]) == 0
    assert "pip install" not in capsys.readouterr().out


def test_connect_runs_mcp_commands_through_the_runner_only_after_confirmation(home, tmp_path, monkeypatch, capsys):
    fake_bin(tmp_path, "claude", "2.1.330 (Claude Code)")
    calls = []
    monkeypatch.setattr(engine, "run_command", lambda argv: calls.append(argv) or (0, ""))
    assert main(["connect", "claude-code", "--dry-run"]) == 0
    assert calls == [] and "run    claude mcp add --scope user briefcase" in capsys.readouterr().out
    assert main(["connect", "claude-code", "--yes"]) == 0
    assert calls == [["claude", "mcp", "add", "--scope", "user", "briefcase", "--",
                      "uvx", "--from", "briefcase-ai[mcp]", "briefcase-ai-mcp"]]


def test_connect_bad_input(home, capsys):
    assert main(["connect", "emacs", "--yes"]) == 2
    assert "unknown tool 'emacs'" in capsys.readouterr().err
    assert main(["connect", "claude-code", "--headers", "nope", "--yes"]) == 2
    assert main(["connect", "--undo", "emacs"]) == 2


def test_connect_list(home, capsys):
    (home / ".codex").mkdir()
    assert main(["connect", "list"]) == 0
    out = capsys.readouterr().out
    lines = {line.split()[0]: line.split() for line in out.splitlines()
             if line and not line.startswith(("TOOL", "Details"))}
    assert lines["codex"][1:3] == ["automatic", "yes"] and lines["cursor"][2] == "no"
    assert lines["cline"][1] == "print-only" and lines["gemini-cli"][1] == "experimental"


def test_refused_file_prints_the_snippet_and_exits_zero_without_writing(home, capsys):
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text('{\n  // comment\n  "a": 1\n}\n')
    assert main(["connect", "claude-code", "--no-mcp", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "comments or trailing commas" in out and '"CLAUDE_CODE_ENABLE_TELEMETRY": "1"' in out
    assert "follow the manual steps above" in out
    assert (home / ".claude" / "settings.json").read_text() == '{\n  // comment\n  "a": 1\n}\n'


# --- mcp ----------------------------------------------------------------------

def test_mcp_link_cursor_prints_a_decodable_deeplink(home, capsys):
    assert main(["mcp", "link", "cursor"]) == 0
    out = capsys.readouterr().out
    link = next(line.strip() for line in out.splitlines() if line.strip().startswith("cursor://"))
    assert payloads.decode_cursor_deeplink(link) == ("briefcase", payloads.UVX_CONFIG)


@pytest.mark.parametrize("tool, expected", [
    ("claude-code", "claude mcp add-json briefcase"),
    ("claude", "claude plugin install briefcase@briefcase-ai"),
    ("codex", "codex mcp add briefcase -- uvx"),
    ("vscode", "code --add-mcp"),
])
def test_mcp_link_other_tools(home, capsys, tool, expected):
    assert main(["mcp", "link", tool]) == 0
    assert expected in capsys.readouterr().out


def test_mcp_link_unknown_tool(home, capsys):
    assert main(["mcp", "link", "cline"]) == 2
    assert main(["mcp", "link", "emacs"]) == 2


def test_mcp_install_writes_only_mcp_config(home, tmp_path, capsys):
    assert main(["mcp", "install", "cursor", "claude-code", "vscode-copilot", "--scope", "project", "--yes"]) == 0
    project = tmp_path / "project"
    assert json.loads((project / ".mcp.json").read_text()) == {"mcpServers": {"briefcase": payloads.UVX_CONFIG}}
    assert json.loads((project / ".cursor" / "mcp.json").read_text()) == {
        "mcpServers": {"briefcase": {"type": "stdio", **payloads.UVX_CONFIG}}}
    assert json.loads((project / ".vscode" / "mcp.json").read_text()) == {
        "servers": {"briefcase": {"type": "stdio", **payloads.UVX_CONFIG}}}
    assert not (home / ".claude").exists() and not (project / ".cursor" / "hooks.json").exists()
    assert "briefcase-ai receive" not in capsys.readouterr().out  # no receiver talk for MCP-only installs
    assert main(["connect", "--undo"]) == 0
    assert not (project / ".mcp.json").exists()


def test_mcp_install_uses_the_installed_script_when_on_path(home, tmp_path):
    fake_bin(tmp_path, "briefcase-ai-mcp", "")
    assert main(["mcp", "install", "cursor", "--yes"]) == 0
    assert json.loads((home / ".cursor" / "mcp.json").read_text()) == {
        "mcpServers": {"briefcase": {"type": "stdio", "command": "briefcase-ai-mcp"}}}


# --- doctor --connect -----------------------------------------------------------

def test_doctor_connect(home, capsys):
    assert main(["doctor", "--connect"]) == 0
    assert "[warn] connect: no tools connected yet" in capsys.readouterr().out
    assert main(["doctor", "--connect", "--strict"]) == 1
    capsys.readouterr()

    assert main(["connect", "claude-code", "--no-mcp", "--yes"]) == 0
    capsys.readouterr()
    assert main(["doctor", "--connect"]) == 0
    out = capsys.readouterr().out
    assert "[ok]   connect claude-code: 5 key(s) present" in out
    assert "[warn] receiver: not reachable at http://127.0.0.1:4318" in out
    assert "docker" not in out  # the evaluation-stack checks are a separate section

    (home / ".claude" / "settings.json").write_text("{}")
    assert main(["doctor", "--connect"]) == 1
    assert "[fail] connect claude-code: keys no longer present" in capsys.readouterr().out


# --- receive ------------------------------------------------------------------

def test_receive_refuses_a_public_bind_without_a_token(home, capsys):
    assert main(["receive", "--host", "0.0.0.0", "--port", "0"]) == 2
    assert "without --token" in capsys.readouterr().err


def test_receive_status_and_stop_when_nothing_runs(home, capsys):
    assert main(["receive", "--status"]) == 1
    assert "not running" in capsys.readouterr().out
    assert main(["receive", "--stop"]) == 0
    assert "not running" in capsys.readouterr().out


def test_receive_bad_sink_and_keep_content_warning(home, capsys):
    assert main(["receive", "--to", "nowhere", "--keep-content", "--port", "0"]) == 2
    err = capsys.readouterr().err
    assert "warning: --keep-content is on" in err and "--to nowhere" in err


def test_receive_detach_passes_the_token_by_environment_not_argv(home, capsys, monkeypatch):
    from briefcase.receiver import daemon

    seen = {}

    def fake_detach(args, host, port, to, token, wait=5.0):
        seen.update(args=args, to=to, token=token)
        return {"pid": 4242, "endpoint": f"http://{host}:{port}"}

    monkeypatch.setattr(daemon, "detach", fake_detach)
    assert main(["receive", "--detach", "--token", "s3cret", "--port", "4999"]) == 0
    assert seen["token"] == "s3cret" and "s3cret" not in seen["args"] and "--token" not in seen["args"]
    assert seen["to"] == str(home / ".briefcase" / "receiver" / "records.jsonl")
    assert "receiver started (pid 4242)" in capsys.readouterr().out


def test_a_bare_connect_keeps_the_endpoint_recorded_by_the_earlier_connect(home, capsys):
    assert main(["connect", "claude-code", "--no-mcp", "--yes", "--endpoint", "https://otel.example.com"]) == 0
    settings = home / ".claude" / "settings.json"
    assert json.loads(settings.read_text())["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otel.example.com"
    capsys.readouterr()
    assert main(["connect", "claude-code", "--no-mcp", "--yes"]) == 0
    assert json.loads(settings.read_text())["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otel.example.com"
    assert "Keeping the telemetry destination" in capsys.readouterr().out
    assert main(["connect", "claude-code", "--no-mcp", "--yes", "--endpoint", "http://127.0.0.1:4318"]) == 0
    assert json.loads(settings.read_text())["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"


def test_mcp_install_for_codex_without_the_binary_keeps_the_otel_table(home):
    assert main(["connect", "codex", "--no-mcp", "--yes", "--endpoint", "https://otel.example.com"]) == 0
    config = home / ".codex" / "config.toml"
    assert "https://otel.example.com" in config.read_text()
    assert main(["mcp", "install", "codex", "--yes"]) == 0
    text = config.read_text()
    assert "[otel]" in text and "https://otel.example.com" in text and "mcp_servers.briefcase" in text


def _claude_endpoint(home):
    return json.loads((home / ".claude" / "settings.json").read_text())["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"]


def test_a_bare_connect_keeps_each_tools_own_recorded_endpoint(home):
    assert main(["connect", "claude-code", "--no-mcp", "--yes", "--endpoint", "https://claude.example.com"]) == 0
    assert main(["connect", "codex", "--no-mcp", "--yes", "--endpoint", "https://codex.example.com"]) == 0
    assert main(["connect", "claude-code", "codex", "--no-mcp", "--yes"]) == 0
    assert _claude_endpoint(home) == "https://claude.example.com"
    assert "https://codex.example.com/v1/logs" in (home / ".codex" / "config.toml").read_text()


def test_a_newly_named_tool_gets_the_default_not_another_tools_remote(home):
    assert main(["connect", "claude-code", "--no-mcp", "--yes", "--endpoint", "https://claude.example.com"]) == 0
    assert main(["connect", "claude-code", "codex", "--no-mcp", "--yes"]) == 0
    assert _claude_endpoint(home) == "https://claude.example.com"
    assert "http://127.0.0.1:4318/v1/logs" in (home / ".codex" / "config.toml").read_text()


def test_a_bare_reconnect_keeps_codex_headers_and_prompt_capture(home):
    assert main(["connect", "codex", "--no-mcp", "--yes", "--endpoint", "https://codex.example.com",
                 "--headers", "Authorization=Bearer abc", "--capture-content"]) == 0
    before = (home / ".codex" / "config.toml").read_text()
    assert "Bearer abc" in before and "log_user_prompt = true" in before
    assert main(["connect", "codex", "--no-mcp", "--yes"]) == 0
    assert (home / ".codex" / "config.toml").read_text() == before


def test_a_second_project_for_cursor_gets_its_own_telemetry_hooks(home, tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    assert main(["connect", "cursor", "--no-mcp", "--scope", "project", "--yes"]) == 0
    assert (first / ".cursor" / "hooks.json").exists()
    monkeypatch.chdir(second)
    assert main(["connect", "cursor", "--no-mcp", "--scope", "project", "--yes"]) == 0
    assert (second / ".cursor" / "hooks.json").exists()


def test_reconnect_repairs_settings_that_went_missing(home, capsys):
    assert main(["connect", "claude-code", "--no-mcp", "--yes", "--endpoint", "https://claude.example.com"]) == 0
    settings = home / ".claude" / "settings.json"
    settings.unlink()
    assert main(["connect", "claude-code", "--no-mcp", "--yes"]) == 0
    assert _claude_endpoint(home) == "https://claude.example.com"  # repaired with the recorded endpoint


def test_reconnect_repairs_codex_telemetry_when_only_the_mcp_block_was_ever_written(home):
    assert main(["mcp", "install", "codex", "--yes"]) == 0
    config = home / ".codex" / "config.toml"
    assert "[otel]" not in config.read_text()
    assert main(["connect", "codex", "--no-mcp", "--yes"]) == 0
    assert "[otel]" in config.read_text()


def test_start_runs_the_receiver_for_an_already_connected_local_tool(home, monkeypatch):
    started = []
    monkeypatch.setattr(receive_commands, "start_detached", lambda args, out=print: started.append(args) or 0)
    assert main(["connect", "claude-code", "--no-mcp", "--yes"]) == 0
    assert main(["connect", "claude-code", "--no-mcp", "--yes", "--start"]) == 0
    assert len(started) == 1
