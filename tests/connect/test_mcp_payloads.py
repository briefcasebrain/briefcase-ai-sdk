"""MCP install payloads for briefcase-ai-mcp."""

import base64
import json
import shlex

import pytest

from briefcase.connect import mcp

UVX = {"command": "uvx", "args": ["--from", "briefcase-ai[mcp]", "briefcase-ai-mcp"]}


def test_server_config_prefers_the_installed_script():
    assert mcp.server_config(lambda name: "/usr/bin/briefcase-mcp") == {"command": "briefcase-ai-mcp"}
    assert mcp.server_config(lambda name: None) == UVX


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
    import tomllib

    assert tomllib.loads(mcp.codex_toml(UVX)) == {"mcp_servers": {"briefcase": UVX}}
