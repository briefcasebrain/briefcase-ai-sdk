"""The policy file ``briefcase policy init`` writes."""

from __future__ import annotations

_HEAD = '''\
# briefcase policy for coding tools (Claude Code, Cursor, Codex CLI).
#
#   briefcase policy check              validate this file and list every rule
#   briefcase policy test -- rm -rf /   see what a command would get, and why
#   briefcase policy log                recent decisions
#   briefcase policy off                remove the hooks (this file stays)
version = 1

# The built-in rules this policy starts from:
#   "observe"  no rules; every tool call is logged and nothing is blocked
#   "guarded"  deny reading credential files and destructive commands;
#              ask before package installs and commands that can send data out
#   "strict"   guarded, plus: ask before sudo, writes outside the project,
#              any MCP tool and any web request; sending data out is denied
preset = "{preset}"

# "enforce" applies the rules. "observe" logs what they would do and blocks nothing.
mode = "{mode}"

# What happens when this file is broken or the hook itself fails:
#   false  tool calls are ALLOWED, and `briefcase status` reports the problem
#   true   every tool call is BLOCKED until it is fixed
# After changing this, run `briefcase policy init` again so the tools' own
# hook settings match.
fail_closed = {fail_closed}

# Shell commands that cannot be analysed (eval of a variable, text piped into a
# shell, a command name computed at run time): "allow" | "ask" | "deny"
on_unparseable = "{on_unparseable}"

# What an "ask" becomes where the tool cannot ask from a hook (Codex CLI;
# Cursor file reads and writes): "deny" | "allow"
ask_fallback = "deny"

# Deny the agent's writes to this file, to briefcase's state and to the tools'
# hook settings. Leave this on: without it the agent can switch the policy off.
protect_self = {protect_self}

# Branches the guarded and strict presets refuse to force-push.
# protected_branches = ["main", "master", "release/*"]

# Preset rules to switch off, by id.
# disable = ["pkg.install"]

# Your rules. They are checked before the preset's, top to bottom, and the
# first rule that matches decides. Each needs `tool` and `effect`.
#   tool    "shell" | "read" | "write" | "file" (read and write) | "mcp" | "web"
#   effect  "allow" | "ask" | "deny"
#
# [[rule]]
# id = "no-prod-db"
# tool = "shell"
# effect = "deny"
# command = ["psql --host=prod*", "kubectl delete"]
# reason = "Production changes go through the runbook."
#
# [[rule]]
# id = "test-fixtures"
# tool = "file"
# effect = "allow"
# paths = ["tests/fixtures/.env"]
#
# [[rule]]
# id = "github-writes"
# tool = "mcp"
# effect = "ask"
# server = "github"
# name = ["create_*", "delete_*", "merge_*"]
#
# [[rule]]
# id = "no-pastebins"
# tool = "web"
# effect = "deny"
# hosts = ["pastebin.com", "*.ngrok.io"]
'''


def render(preset: str = "guarded", fail_closed: bool = False) -> str:
    from briefcase.policy import model

    defaults = model._defaults(preset)
    return _HEAD.format(
        preset=preset, mode=defaults["mode"], fail_closed="true" if fail_closed else "false",
        on_unparseable=defaults["on_unparseable"], protect_self="true" if defaults["protect_self"] else "false",
    )
