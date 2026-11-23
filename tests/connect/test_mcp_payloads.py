"""MCP install payloads for briefcase-ai-mcp."""

import base64
import json
import shlex

import pytest

from briefcase.connect import mcp

UVX = {"command": "uvx", "args": ["--from", "briefcase-ai[mcp]", "briefcase-ai-mcp"]}


def only(*names):
    """A ``which`` that knows exactly these executables."""
    return lambda name: f"/usr/bin/{name}" if name in names else None


def test_server_config_prefers_the_installed_script(monkeypatch):
    monkeypatch.setattr(mcp, "launcher_works", lambda: True)
    assert mcp.server_config(only("briefcase-ai-mcp")) == {"command": "briefcase-ai-mcp"}
    assert mcp.server_config(only("briefcase-mcp")) == UVX  # the old name does not count
    assert mcp.server_config(lambda name: None) == UVX


def test_server_config_uses_uvx_when_the_installed_script_cannot_run(monkeypatch):
    monkeypatch.setattr(mcp, "launcher_works", lambda: False)  # no `mcp` package in this environment
    assert mcp.server_config(only("briefcase-ai-mcp")) == UVX


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
