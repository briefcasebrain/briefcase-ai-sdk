"""MCP install payloads for briefcase-ai-mcp."""

import base64
import json
import shlex

import pytest

from briefcase.connect import mcp

UVX = {"command": "uvx", "args": ["--from", "briefcase-ai[mcp]", "briefcase-ai-mcp"]}


def stub(directory, name, exit_code, output=""):
    """A real executable on disk that answers `--check` like the launcher would."""
    import stat

    path = directory / name
    path.write_text(f"#!/bin/sh\n[ \"$1\" = --check ] || exit 99\necho '{output}'\nexit {exit_code}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_server_config_prefers_a_launcher_that_passes_its_own_check(tmp_path, monkeypatch):
    stub(tmp_path, "briefcase-ai-mcp", 0, "ok")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert mcp.server_config() == {"command": "briefcase-ai-mcp"}


def test_server_config_uses_uvx_when_the_launcher_on_path_fails_its_check(tmp_path, monkeypatch):
    stub(tmp_path, "briefcase-ai-mcp", 1, "needs the mcp extra")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert mcp.server_config() == UVX


def test_server_config_ignores_the_old_launcher_name_and_a_missing_launcher(tmp_path, monkeypatch):
    stub(tmp_path, "briefcase-mcp", 0, "ok")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert mcp.server_config() == UVX


def test_the_check_runs_the_launcher_found_not_the_current_interpreter(tmp_path, monkeypatch):
    """A launcher from another environment is judged by what it says, whatever this interpreter has."""
    stub(tmp_path, "briefcase-ai-mcp", 1)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert mcp.server_config() == UVX


def test_the_check_flag_on_the_real_launcher_exits_one_without_the_extra():
    import subprocess
    import sys

    code = ("import sys; sys.modules['mcp'] = None; sys.argv = ['briefcase-ai-mcp', '--check'];"
            " from briefcase.mcp.__main__ import main; main()")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "Traceback" not in done.stderr and "pip install briefcase-ai[mcp]" in done.stderr


def test_the_launcher_without_the_mcp_extra_prints_one_line_and_no_traceback():
    import subprocess
    import sys

    code = "import sys; sys.modules['mcp'] = None; from briefcase.mcp.__main__ import main; main()"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "Traceback" not in done.stderr
    assert done.stderr.strip().count("\n") == 0 and "pip install briefcase-ai[mcp]" in done.stderr


@pytest.mark.parametrize("config", [UVX, {"command": "briefcase-ai-mcp"},
                                    {"command": "c m d", "args": ["a&b", "x=y+z/?"], "env": {"K": "v"}}])
def test_cursor_deeplink_round_trip(config):
    url = mcp.cursor_deeplink(config)
    assert url.startswith("cursor://anysphere.cursor-deeplink/mcp/install?name=briefcase&config=")
    assert mcp.decode_cursor_deeplink(url) == ("briefcase", config)
    # The config parameter is plain base64 of the JSON server config.
    from urllib.parse import parse_qs, urlsplit

    raw = parse_qs(urlsplit(url).query)["config"][0]
    assert json.loads(base64.b64decode(raw)) == config


def test_decode_rejects_other_links():
    with pytest.raises(ValueError):
        mcp.decode_cursor_deeplink("https://example.com/mcp/install?name=a&config=e30=")


def test_cli_payloads_are_argument_lists():
    assert mcp.claude_add_argv(UVX) == [
        "claude", "mcp", "add", "--scope", "user", "briefcase", "--",
        "uvx", "--from", "briefcase-ai[mcp]", "briefcase-ai-mcp"]
    argv = mcp.claude_add_json_argv(UVX)
    assert argv[:4] == ["claude", "mcp", "add-json", "briefcase"]
    assert json.loads(argv[4]) == {"type": "stdio", **UVX}
    assert mcp.codex_add_argv({"command": "briefcase-ai-mcp"}) == [
        "codex", "mcp", "add", "briefcase", "--", "briefcase-ai-mcp"]
    code = mcp.vscode_add_argv({"command": "briefcase-ai-mcp"})
    assert code[:2] == ["code", "--add-mcp"]
    assert json.loads(code[2]) == {"name": "briefcase", "type": "stdio", "command": "briefcase-ai-mcp"}
    # Rendered for copy-paste, the JSON survives a shell round trip.
    assert shlex.split(mcp.shell(code)) == code


def test_file_payloads():
    assert json.loads(mcp.mcp_servers_json(UVX)) == {"mcpServers": {"briefcase": UVX}}
    assert json.loads(mcp.mcp_servers_json(UVX, key="servers", typed=True)) == {
        "servers": {"briefcase": {"type": "stdio", **UVX}}}
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        import tomli as tomllib

    assert tomllib.loads(mcp.codex_toml(UVX)) == {"mcp_servers": {"briefcase": UVX}}
