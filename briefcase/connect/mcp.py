"""Install payloads for the SDK's own MCP server (``briefcase-mcp``).

One server config, rendered into each tool's install mechanism: a Cursor
deeplink, ``claude mcp add`` / ``add-json``, ``codex mcp add``,
``code --add-mcp``, and the project-level JSON files.
"""

from __future__ import annotations

import base64
import json
import shlex
import shutil
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

SERVER_NAME = "briefcase"
CURSOR_DEEPLINK = "cursor://anysphere.cursor-deeplink/mcp/install"
UVX_CONFIG = {"command": "uvx", "args": ["--from", "briefcase-ai[mcp]", "briefcase-mcp"]}


def server_config(which: Optional[Callable[[str], Optional[str]]] = None) -> Dict[str, Any]:
    """``briefcase-mcp`` when it is on PATH, else a ``uvx`` one-liner that
    needs nothing installed beforehand."""
    which = which or shutil.which
    if which("briefcase-mcp"):
        return {"command": "briefcase-mcp"}
    return {"command": UVX_CONFIG["command"], "args": list(UVX_CONFIG["args"])}


def stdio_entry(config: Dict[str, Any]) -> Dict[str, Any]:
    """The config with an explicit ``"type": "stdio"`` (Cursor, VS Code)."""
    return {"type": "stdio", **config}


def cursor_deeplink(config: Dict[str, Any], name: str = SERVER_NAME) -> str:
    payload = base64.b64encode(json.dumps(config, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"{CURSOR_DEEPLINK}?name={quote(name, safe='')}&config={quote(payload, safe='')}"


def decode_cursor_deeplink(url: str) -> Tuple[str, Dict[str, Any]]:
    parts = urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}{parts.path}" != CURSOR_DEEPLINK:
        raise ValueError(f"not a Cursor MCP install deeplink: {url!r}")
    query = parse_qs(parts.query, strict_parsing=True)
    config = json.loads(base64.b64decode(query["config"][0], validate=True).decode("utf-8"))
    return query["name"][0], config


def _command_tail(config: Dict[str, Any]) -> List[str]:
    return [config["command"], *config.get("args", [])]


def claude_add_argv(config: Dict[str, Any], scope: str = "user", name: str = SERVER_NAME) -> List[str]:
    return ["claude", "mcp", "add", "--scope", scope, name, "--", *_command_tail(config)]


def claude_remove_argv(scope: str = "user", name: str = SERVER_NAME) -> List[str]:
    return ["claude", "mcp", "remove", "--scope", scope, name]


def claude_add_json_argv(config: Dict[str, Any], name: str = SERVER_NAME) -> List[str]:
    return ["claude", "mcp", "add-json", name, json.dumps(stdio_entry(config), separators=(",", ":"))]


def codex_add_argv(config: Dict[str, Any], name: str = SERVER_NAME) -> List[str]:
    return ["codex", "mcp", "add", name, "--", *_command_tail(config)]


def codex_remove_argv(name: str = SERVER_NAME) -> List[str]:
    return ["codex", "mcp", "remove", name]


def vscode_add_argv(config: Dict[str, Any], name: str = SERVER_NAME) -> List[str]:
    payload = {"name": name, **stdio_entry(config)}
    return ["code", "--add-mcp", json.dumps(payload, separators=(",", ":"))]


def mcp_servers_json(config: Dict[str, Any], key: str = "mcpServers", typed: bool = False,
                     name: str = SERVER_NAME) -> str:
    entry = stdio_entry(config) if typed else config
    return json.dumps({key: {name: entry}}, indent=2)


def codex_toml(config: Dict[str, Any], name: str = SERVER_NAME) -> str:
    lines = [f"[mcp_servers.{name}]", f"command = {json.dumps(config['command'])}"]
    if config.get("args"):
        lines.append("args = [" + ", ".join(json.dumps(a) for a in config["args"]) + "]")
    return "\n".join(lines)


def shell(argv: List[str]) -> str:
    return shlex.join(argv)
