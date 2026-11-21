"""``briefcase mcp install`` / ``briefcase mcp link``: one-step MCP setup.

``install`` registers the SDK's ``briefcase-mcp`` server with the tools on this
machine (the MCP half of ``briefcase connect``). ``link`` prints the payload
for one tool so it can be pasted, clicked or shared.
"""

from __future__ import annotations

import sys

from briefcase.connect import mcp as payloads
from briefcase.connect.adapters import registry, resolve_names, which

LINK_TOOLS = ("cursor", "claude-code", "codex", "vscode-copilot")


def register(sub) -> None:
    mcp = sub.add_parser("mcp", help="Install the briefcase MCP server into coding tools.")
    msub = mcp.add_subparsers(dest="sub")
    install = msub.add_parser("install", help="Register briefcase-mcp with detected (or named) tools.")
    install.add_argument("tools", nargs="*", help="Tools to set up (default: every detected tool).")
    install.add_argument("--scope", choices=("user", "project"), default="user")
    install.add_argument("--dry-run", dest="connect_dry_run", action="store_true",
                         help="Show the plan and diff; write nothing.")
    install.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    install.add_argument("--force", action="store_true", help="Write even for an unrecognised tool version.")
    install.set_defaults(func=cmd_mcp_install)
    link = msub.add_parser("link", help="Print the install payload (deeplink, command, JSON) for one tool.")
    link.add_argument("tool", help="cursor | claude-code | codex | vscode-copilot")
    link.set_defaults(func=cmd_mcp_link)


def link_text(tool: str) -> str:
    config = payloads.server_config(which)
    lines = []
    if tool == "cursor":
        lines += ["Cursor one-click install (open this link):", "  " + payloads.cursor_deeplink(config), "",
                  "Or add to ~/.cursor/mcp.json (or .cursor/mcp.json in a project):",
                  payloads.mcp_servers_json(config, typed=True)]
    elif tool == "claude-code":
        lines += ["Claude Code, user scope:", "  " + payloads.shell(payloads.claude_add_argv(config)), "",
                  "Or as JSON:", "  " + payloads.shell(payloads.claude_add_json_argv(config)), "",
                  "Or add to .mcp.json in a project:", payloads.mcp_servers_json(config), "",
                  "Or install the plugin:",
                  "  claude plugin marketplace add briefcasebrain/briefcase-ai-sdk",
                  "  claude plugin install briefcase@briefcase-ai"]
    elif tool == "codex":
        lines += ["Codex CLI:", "  " + payloads.shell(payloads.codex_add_argv(config)), "",
                  "Or add to ~/.codex/config.toml:", payloads.codex_toml(config)]
    elif tool == "vscode-copilot":
        lines += ["VS Code:", "  " + payloads.shell(payloads.vscode_add_argv(config)), "",
                  "Or add to .vscode/mcp.json in a workspace:",
                  payloads.mcp_servers_json(config, key="servers", typed=True)]
    return "\n".join(lines)


def cmd_mcp_link(args, store=None, engine=None, stack=None) -> int:
    try:
        (tool,) = resolve_names([args.tool])
    except KeyError:
        tool = ""
    if tool not in LINK_TOOLS:
        print(f"error: no install payload for {args.tool!r}; choose one of: {', '.join(LINK_TOOLS)}",
              file=sys.stderr)
        return 2
    print(link_text(tool))
    return 0


def cmd_mcp_install(args, store=None, engine=None, stack=None) -> int:
    from briefcase.cli import connect_commands

    args.telemetry = False
    args.mcp = True
    args.undo = False
    args.all = False
    args.endpoint = None
    args.headers = []
    args.capture_content = False
    args.start = False
    return connect_commands.run_connect(args, registry(), offer_receiver=False)
