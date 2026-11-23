"""``briefcase-ai connect --managed --out DIR``: a rollout pack for an administrator.

Nothing here touches a system path. It writes files into ``DIR`` and a
``README.md`` that says where each one goes on each operating system; an
administrator's device-management tooling does the placing.

What is generated as a ready file is a format that a vendor's documentation
states (the page and the date are in ``manifest.json`` and the README):

- Claude Code ``managed-settings.d/10-briefcase.json`` and its directories:
  https://code.claude.com/docs/en/managed-settings
- the macOS configuration profile: the ``com.anthropic.claudecode`` domain from
  the same page, wrapped in Apple's managed-preferences payload,
  https://developer.apple.com/documentation/devicemanagement/managedpreferences
- Cursor's enterprise ``hooks.json`` and its system paths: https://cursor.com/docs/hooks
- Codex's system ``hooks.json``: https://developers.openai.com/codex/hooks

Anything that could not be confirmed is written with a ``.template`` suffix
and a header saying what is unconfirmed. No file in the pack has been
installed on a managed machine by this project.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from briefcase.connect import edits
from briefcase.connect.adapters import DEFAULT_ENDPOINT, Context, load_data, registry
from briefcase.policy import install, model
from briefcase.policy import tools as tool_formats

DEFAULT_TOOLS = ("claude-code", "cursor", "codex", "cowork")
_NAMESPACE = uuid.UUID("6f0c3f0e-5b0a-4d53-9a0c-2f6f1c1c7a10")
APPLE_DOCS = "https://developer.apple.com/documentation/devicemanagement/managedpreferences"
CLAUDE_MANAGED_DOCS = "https://code.claude.com/docs/en/managed-settings"
SYSTEM_POLICY = {
    "macOS": "/Library/Application Support/Briefcase/policy.toml",
    "Linux and WSL": "/etc/briefcase/policy.toml",
    "Windows": r"C:\ProgramData\Briefcase\policy.toml",
}


class Pack(object):
    def __init__(self) -> None:
        self.files: Dict[str, str] = {}
        self.entries: List[Dict[str, Any]] = []

    def add(self, path: str, text: str, kind: str, where: Any, docs: str, note: str = "") -> None:
        self.files[path] = text if text.endswith("\n") else text + "\n"
        self.entries.append({"file": path, "kind": kind, "install_to": where, "source": docs, "note": note})


def _json(doc: Any) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def claude_settings(ctx: Context, enforce: bool, fail_closed: bool, data: Dict[str, Any]) -> Dict[str, Any]:
    """The managed-settings drop-in: telemetry ``env`` keys and, with a policy, the PreToolUse hook."""
    adapter = registry()["claude-code"]
    doc: Dict[str, Any] = {"env": adapter.telemetry_env(ctx)}  # type: ignore[attr-defined]
    if enforce:
        options = install.HookOptions(fail_closed=fail_closed, endpoint=ctx.endpoint, portable=True)
        command = install.hook_command("claude-code", options)
        doc["hooks"] = {"PreToolUse": [install.claude_entry(command, data["enforce"], fail_closed)]}
    return doc


def mobileconfig(settings: Dict[str, Any], organization: str = "briefcase") -> str:
    """Apple configuration profile forcing ``settings`` in the ``com.anthropic.claudecode`` domain."""
    import plistlib

    domain = "com.anthropic.claudecode"
    identifier = "ai.briefcase.claudecode"
    profile = {
        "PayloadContent": [{
            "PayloadContent": {domain: {"Forced": [{"mcx_preference_settings": settings}]}},
            "PayloadDisplayName": "Claude Code managed settings (briefcase)",
            "PayloadIdentifier": identifier + ".preferences",
            "PayloadType": "com.apple.ManagedClient.preferences",
            "PayloadUUID": str(uuid.uuid5(_NAMESPACE, identifier + ".preferences")).upper(),
            "PayloadVersion": 1,
        }],
        "PayloadDescription": "Points Claude Code's telemetry at the briefcase receiver"
                              + (" and runs the briefcase-ai policy before each tool call." if "hooks" in settings
                                 else "."),
        "PayloadDisplayName": "briefcase for Claude Code",
        "PayloadIdentifier": identifier,
        "PayloadOrganization": organization,
        "PayloadType": "Configuration",
        "PayloadUUID": str(uuid.uuid5(_NAMESPACE, identifier)).upper(),
        "PayloadVersion": 1,
    }
    return plistlib.dumps(profile, sort_keys=False).decode("utf-8")


def reg_template(settings: Dict[str, Any]) -> str:
    value = json.dumps(settings, separators=(",", ":"), ensure_ascii=True)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return "\n".join([
        "; TEMPLATE - check before use.",
        "; Confirmed (https://code.claude.com/docs/en/managed-settings, 2026-11-21): Claude Code reads its managed",
        ";   settings as JSON from a REG_SZ value named Settings under HKLM\\SOFTWARE\\Policies\\ClaudeCode.",
        "; Not confirmed: this file's .reg syntax and escaping were not checked against Microsoft's",
        ";   documentation or imported on Windows. Import it on one test machine, run `claude doctor`, and",
        ";   confirm /status shows `Enterprise managed settings (HKLM)` before any wider rollout.",
        "; Alternative that needs no registry: copy managed-settings.d\\10-briefcase.json to",
        ";   C:\\Program Files\\ClaudeCode\\managed-settings.d\\ (see README.md).",
        "Windows Registry Editor Version 5.00",
        "",
        "[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\ClaudeCode]",
        f'"Settings"="{escaped}"',
        "",
    ])


def cursor_hooks(ctx: Context, enforce: bool, fail_closed: bool, data: Dict[str, Any]) -> Dict[str, Any]:
    endpoint = ["--endpoint", ctx.endpoint] if ctx.endpoint != DEFAULT_ENDPOINT else []
    observe = " ".join(["briefcase-ai", "hook", "cursor"] + endpoint)
    hooks: Dict[str, List[Dict[str, Any]]] = {
        event: [{"command": observe, "timeout": data["hook_timeout_seconds"]}] for event in data["hook_events"]}
    if enforce:
        options = install.HookOptions(fail_closed=fail_closed, endpoint=ctx.endpoint, portable=True)
        command = install.hook_command("cursor", options)
        for event, entry in install.cursor_entries(command, data["enforce"], fail_closed).items():
            hooks.setdefault(event, []).append(entry)
    return {"version": 1, "hooks": hooks}


def codex_hooks(ctx: Context, fail_closed: bool, data: Dict[str, Any]) -> Dict[str, Any]:
    options = install.HookOptions(fail_closed=fail_closed, endpoint=ctx.endpoint, portable=True)
    return {"hooks": {"PreToolUse": [install.codex_entry(install.hook_command("codex", options), data["enforce"])]}}


def codex_requirements_template(ctx: Context, fail_closed: bool, data: Dict[str, Any]) -> str:
    options = install.HookOptions(fail_closed=fail_closed, endpoint=ctx.endpoint, portable=True)
    command = install.hook_command("codex", options)
    return "\n".join([
        "# TEMPLATE - merge into your organization's Codex requirements.toml.",
        "# Confirmed (https://developers.openai.com/codex/hooks, 2026-11-21): managed hooks are declared under",
        "#   [hooks] in requirements.toml, and [features].hooks = true keeps them on for users who disabled hooks.",
        "# Not confirmed: where requirements.toml lives on each operating system. The ready-made alternative",
        "#   in this pack is codex/hooks.json for /etc/codex/hooks.json, a documented system path.",
        "[features]",
        "hooks = true",
        "",
        "[[hooks.PreToolUse]]",
        f"matcher = {json.dumps(tool_formats.CODEX_MATCHER)}",
        "",
        "[[hooks.PreToolUse.hooks]]",
        'type = "command"',
        f"command = {json.dumps(command)}",
        f"timeout = {data['enforce']['timeout_seconds']}",
        'statusMessage = "Checking briefcase policy"',
        "",
    ])


def codex_otel_template(ctx: Context) -> str:
    adapter = registry()["codex"]
    return "\n".join([
        "# TEMPLATE - add to each user's ~/.codex/config.toml.",
        "# Confirmed (https://developers.openai.com/codex/config-advanced, 2026-11-21): the [otel] table below.",
        "# Not confirmed: a system-wide location for it. Codex ignores [otel] in project config, so this has to",
        "#   reach the user-level file; `briefcase-ai connect codex --endpoint <url>` writes the same block for one user.",  # noqa: E501
        "# Codex sends OTLP as protobuf: the receiver needs `pip install 'briefcase-ai[receiver]'`.",
        adapter.otel_toml(ctx),  # type: ignore[attr-defined]
        "",
    ])


def cowork_checklist(ctx: Context) -> str:
    return "\n".join([
        "# Claude Desktop / Cowork: admin checklist",
        "",
        "TEMPLATE: these steps come from `briefcase-ai connect cowork` and have not been checked against the",
        "current admin console. Menu names may differ; the settings to look for are the OpenTelemetry export",
        "switch and its OTLP endpoint.",
        "",
        "Telemetry for Claude Desktop and Cowork is set by an organization admin; nothing on a user's machine",
        "can turn it on.",
        "",
        "- [ ] Open Organization settings > Data and privacy > Monitoring.",
        "- [ ] Enable OpenTelemetry export.",
        f"- [ ] Set the OTLP endpoint to the receiver your organization runs: `{ctx.endpoint}`",
        "- [ ] If the receiver needs a token, add the header `Authorization=Bearer <token>`.",
        "- [ ] On the receiver host, if the export arrives as protobuf: `pip install 'briefcase-ai[receiver]'`.",
        "- [ ] Send one message from Claude Desktop and check that a record arrives (`briefcase-ai receive --status`).",
        "",
        "Cowork always sends `user.email` and `workspace.host_paths`. The receiver replaces the e-mail with a",
        "keyed hash and drops the paths unless it is started with `--keep-content`.",
        "",
        "There is no hook in Claude Desktop that the briefcase-ai policy can use, so the policy is not enforced there.",
        "",
    ])


def build(tools: List[str], ctx: Context, policy_text: Optional[str] = None) -> Pack:
    """Everything in the pack, in memory. ``policy_text`` must already be a valid policy."""
    data = load_data()["tools"]
    pack = Pack()
    enforce = policy_text is not None
    fail_closed = False
    if enforce:
        layer = model.parse_layer(policy_text or "", "system", "policy.toml")
        fail_closed = bool(layer.settings.get("fail_closed", False))
        pack.add("briefcase/policy.toml", policy_text or "", "ready", SYSTEM_POLICY,
                 "briefcase", "The policy every machine enforces. A user or project policy can only add restrictions.")
    if "claude-code" in tools:
        settings = claude_settings(ctx, enforce, fail_closed, data["claude-code"])
        pack.add("claude-code/managed-settings.d/10-briefcase.json", _json(settings), "ready", {
            "macOS": "/Library/Application Support/ClaudeCode/managed-settings.d/10-briefcase.json",
            "Linux and WSL": "/etc/claude-code/managed-settings.d/10-briefcase.json",
            "Windows": r"C:\Program Files\ClaudeCode\managed-settings.d\10-briefcase.json",
        }, CLAUDE_MANAGED_DOCS,
            "Drop-in merged with managed-settings.json; users cannot remove or disable managed hooks.")
        pack.add("claude-code/macos/briefcase-claude-code.mobileconfig", mobileconfig(settings), "ready",
                 {"macOS": "upload as a custom configuration profile in your MDM (device channel)"},
                 f"{CLAUDE_MANAGED_DOCS} and {APPLE_DOCS}",
                 "Use this OR the drop-in file on macOS, not both: an MDM profile outranks the file. "
                 "Built from the two documented formats; not installed on a Mac by this project.")
        pack.add("claude-code/windows/claude-code-policy.reg.template", reg_template(settings), "template",
                 {"Windows": r"HKLM\SOFTWARE\Policies\ClaudeCode, value Settings (REG_SZ)"}, CLAUDE_MANAGED_DOCS,
                 "The registry location is documented; the .reg file itself is unverified.")
    if "cursor" in tools:
        pack.add("cursor/hooks.json", _json(cursor_hooks(ctx, enforce, fail_closed, data["cursor"])), "ready", {
            "macOS": "/Library/Application Support/Cursor/hooks.json",
            "Linux and WSL": "/etc/cursor/hooks.json",
            "Windows": r"C:\ProgramData\Cursor\hooks.json",
        }, data["cursor"]["enforce"]["docs_url"],
            "Enterprise hooks run first and cannot be removed by a user. Never run against Cursor by this project.")
    if "codex" in tools:
        if enforce:
            pack.add("codex/hooks.json", _json(codex_hooks(ctx, fail_closed, data["codex"])), "ready", {
                "macOS": "/etc/codex/hooks.json", "Linux and WSL": "/etc/codex/hooks.json",
                "Windows": r"%ProgramData%\OpenAI\Codex\hooks.json",
            }, data["codex"]["enforce"]["docs_url"],
                "System hooks are trusted by policy. Never run against Codex by this project.")
            pack.add("codex/requirements-hooks.toml.template",
                     codex_requirements_template(ctx, fail_closed, data["codex"]), "template",
                     "your organization's requirements.toml", data["codex"]["enforce"]["docs_url"],
                     "Use instead of hooks.json when you already manage requirements.toml.")
        pack.add("codex/config-otel.toml.template", codex_otel_template(ctx), "template",
                 "each user's ~/.codex/config.toml", data["codex"]["docs_url"],
                 "Telemetry only. No documented system-wide location.")
    if "cowork" in tools:
        pack.add("cowork/CHECKLIST.md", cowork_checklist(ctx), "template", "the organization admin console",
                 data["cowork"]["docs_url"], "Steps for a person, not a file to deploy.")
    pack.files["README.md"] = readme(pack, tools, ctx, enforce, fail_closed)
    pack.files["manifest.json"] = _json({
        "generated_by": "briefcase-ai connect --managed", "endpoint": ctx.endpoint, "tools": tools,
        "policy": enforce, "fail_closed": fail_closed, "files": pack.entries,
    })
    return pack


def _where(where: Any) -> str:
    if isinstance(where, dict):
        return "<br>".join(f"{name}: `{path}`" for name, path in where.items())
    return str(where)


def readme(pack: Pack, tools: List[str], ctx: Context, enforce: bool, fail_closed: bool) -> str:
    lines = [
        "# briefcase rollout pack",
        "",
        "Generated by `briefcase-ai connect --managed`. Nothing was installed: every file below is for your",
        "device-management tooling to place. Files ending in `.template` are NOT ready to deploy; each starts",
        "with what is confirmed and what is not.",
        "",
        f"- Receiver endpoint in these files: `{ctx.endpoint}`",
        "- Policy enforcement: " + ("yes, from `briefcase/policy.toml`" if enforce
                                    else "no (telemetry only; pass --policy FILE to add it)"),
    ]
    if enforce:
        lines.append("- When the policy or the hook is broken: tool calls are "
                     + ("BLOCKED (`fail_closed = true`)" if fail_closed
                        else "allowed and the problem is reported (fail open)"))
    lines += [
        "",
        "## Before you deploy",
        "",
        "1. Run a receiver the fleet can reach and put its URL in these files (`--endpoint`). For anything",
        "   other than `127.0.0.1` it needs a token and TLS in front of it:",
        "   `briefcase-ai receive --host 0.0.0.0 --token <secret>`.",
    ]
    if enforce:
        lines += [
            "2. Install briefcase on every machine so that `briefcase-ai` is on the PATH the coding tools see:",
            "   `pip install briefcase-ai` (or your package manager). The hooks run `briefcase-ai hook <tool> --enforce`.",  # noqa: E501
            "3. Deploy `briefcase/policy.toml` to the system policy path, readable by everyone and writable",
            "   only by an administrator:",
            "",
        ]
        lines += [f"   - {name}: `{path}`" for name, path in SYSTEM_POLICY.items()]
        lines += [
            "",
            "   A system policy is the base policy on that machine. `~/.briefcase/policy.toml` and a project's",
            "   `.briefcase/policy.toml` can add `ask` and `deny` rules to it and cannot allow anything it denies.",
            "4. Each hook decides on the machine itself (no network hop) and then posts a decision record",
            "   (rule id and effect; the command or path only as a keyed hash) to",
            f"   `{ctx.endpoint}/v1/policy/decisions`,",
            "   without waiting for it. If that receiver needs a token, set `BRIEFCASE_RECEIVER_TOKEN` in the",
            "   environment the coding tools run in; without it the records are refused and enforcement is unaffected.",
        ]
    lines += ["", "## Files", "", "| File | Status | Where it goes | Format from |", "| --- | --- | --- | --- |"]
    for entry in pack.entries:
        status = "ready" if entry["kind"] == "ready" else "TEMPLATE"
        lines.append(f"| `{entry['file']}` | {status} | {_where(entry['install_to'])} | {entry['source']} |")
    lines.append("")
    for entry in pack.entries:
        if entry["note"]:
            lines.append(f"- `{entry['file']}`: {entry['note']}")
    if "claude-code" in tools:
        lines += [
            "",
            "## Claude Code",
            "",
            "Pick ONE delivery per operating system: the drop-in file, or the macOS profile / Windows registry",
            "value. Claude Code applies the highest-ranked managed source that has a policy and skips the rest",
            "unless `managedSourcesBehavior` is `merge`.",
            "",
            "- Managed settings win over user, project and local settings. Hook entries merge across levels:",
            "  a user can add hooks but cannot remove a managed one, and `disableAllHooks` in user settings",
            "  does not disable managed hooks.",
        ]
        if enforce:
            lines += [
                "- The hook decides `deny` and `ask`; an allowed call still goes through Claude Code's own",
                "  permission prompts.",
            ]
            if fail_closed:
                lines.append('- `onFailure: "block"` makes a hook that cannot start or times out block the tool call. '
                             "It needs Claude Code 2.1.295 or later; older versions ignore the hook failure.")
            else:
                lines.append("- If `briefcase-ai` is missing on a machine the hook fails and Claude Code runs the tool "
                             "anyway. Set `fail_closed = true` in the policy and regenerate to block instead.")
            lines += [
                "- Optional hardening, not included because it changes what developers can do. Both are documented",
                "  managed settings you can add to the drop-in:",
                '  `"allowManagedHooksOnly": true` (only managed hooks run) and',
                '  `"permissions": {"disableBypassPermissionsMode": "disable"}`.',
            ]
        lines += [
            "",
            "Verify on one machine: run `/status` in Claude Code and read the `Setting sources` line. It should",
            "list `Enterprise managed settings` with `(drop-ins)`, `(file + drop-ins)`, `(plist)` or `(HKLM)`.",
            "`claude doctor` lists entries Claude Code dropped.",
        ]
    if "cursor" in tools:
        lines += [
            "",
            "## Cursor",
            "",
            "`cursor/hooks.json` is the enterprise hooks file. It forwards session, prompt and tool events to the",
            "receiver" + (" and runs the policy before shell commands, MCP tools, file reads and file writes."
                          if enforce else "."),
            "Cursor can ask the user only for shell commands and MCP tools; elsewhere an `ask` rule follows the",
            "policy's `ask_fallback`. Verify in Cursor under Customize > Hooks, and in the Hooks output channel.",
        ]
    if "codex" in tools:
        lines += [
            "",
            "## Codex CLI",
            "",
            ("`codex/hooks.json` is a system hooks file; system hooks are trusted without the `/hooks` review. "
             "Codex cannot ask from a hook, so an `ask` rule follows the policy's `ask_fallback`."
             if enforce else "Without a policy there is no Codex hook to deploy."),
            "Telemetry is the `[otel]` block in `codex/config-otel.toml.template`, which has to reach each user's",
            "`~/.codex/config.toml`.",
        ]
    if "cowork" in tools:
        lines += ["", "## Claude Desktop / Cowork", "",
                  "See `cowork/CHECKLIST.md`. It is a list of admin-console steps."]
    lines += [
        "",
        "## Verify a machine",
        "",
        "```bash",
        "briefcase-ai doctor --connect      # hooks present, tool versions, managed overrides, receiver reachable",
    ]
    if enforce:
        lines += [
            "briefcase-ai policy check          # the system policy is listed as `ok system`",
            "briefcase-ai policy test -- rm -rf /",
        ]
    lines += [
        "```",
        "",
        "`briefcase-ai doctor --connect` reports on what `briefcase-ai connect` wrote for the current user; settings",
        "delivered by this pack live in system locations, so also use each tool's own check above.",
        "",
        "## What this does and does not guarantee",
        "",
        "- Files in system locations cannot be changed by an agent running as the user. That is what makes this",
        "  stronger than `briefcase-ai policy init` on one machine.",
        "- The policy sees tool calls, not their effects. A script the agent writes and then runs is judged by",
        "  its command line, not by what the script does. Treat the policy as a guardrail next to OS-level",
        "  controls (sandboxing, file permissions, network egress rules), not instead of them.",
        "- A developer who is an administrator on their machine can edit any of these files.",
        "",
    ]
    return "\n".join(lines)


def write(pack: Pack, out_dir: Path) -> List[Path]:
    written = []
    for relative, text in sorted(pack.files.items()):
        target = out_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def resolve_tools(names: List[str]) -> Tuple[List[str], Optional[str]]:
    from briefcase.connect.adapters import ALIASES

    out = []
    for raw in names:
        name = ALIASES.get(raw.lower(), raw.lower())
        if name not in DEFAULT_TOOLS:
            return [], raw
        if name not in out:
            out.append(name)
    return out or list(DEFAULT_TOOLS), None


def cmd_managed(args, out=print) -> int:
    from briefcase.cli.connect_commands import _parse_headers

    names = list(getattr(args, "managed_tools", None) or getattr(args, "tools", None) or [])
    tools, bad = resolve_tools(names)
    if bad is not None:
        print(f"error: {bad!r} has no managed rollout; choose from: {', '.join(DEFAULT_TOOLS)}", file=sys.stderr)
        return 2
    if not getattr(args, "out", None):
        print("error: --managed needs --out DIR (nothing is ever written to a system path)\n"
              "example: briefcase-ai connect --managed --out ./briefcase-rollout --endpoint https://otel.example.com",
              file=sys.stderr)
        return 2
    out_dir = Path(args.out).expanduser()
    if edits.is_system_path(out_dir.resolve()):
        print(f"error: {out_dir} is a system path; briefcase never writes there. Choose a working directory "
              "and let your device-management tooling place the files.", file=sys.stderr)
        return 1
    try:
        headers = _parse_headers(getattr(args, "headers", []) or [])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    policy_text = None
    if getattr(args, "policy", None):
        try:
            policy_text = Path(args.policy).expanduser().read_text(encoding="utf-8")
            model.parse_layer(policy_text, "system", str(args.policy))
        except OSError as exc:
            print(f"error: cannot read {args.policy}: {exc}", file=sys.stderr)
            return 1
        except model.PolicyError as exc:
            print(f"error: {args.policy} is not a valid policy: {exc}\nCheck it with: briefcase-ai policy check",
                  file=sys.stderr)
            return 1
    ctx = Context(endpoint=(getattr(args, "endpoint", None) or DEFAULT_ENDPOINT).rstrip("/"), headers=headers,
                  capture_content=bool(getattr(args, "capture_content", False)), mcp=False)
    pack = build(tools, ctx, policy_text)
    dry = bool(getattr(args, "connect_dry_run", False) or getattr(args, "dry_run", False))
    if out_dir.exists() and any(out_dir.iterdir()) and not getattr(args, "force", False) and not dry:
        print(f"error: {out_dir} is not empty; choose a new directory or pass --force to overwrite files in it.",
              file=sys.stderr)
        return 1
    out(f"Rollout pack for: {', '.join(tools)}" + ("" if policy_text is None else " (with policy enforcement)"))
    for entry in pack.entries:
        out(f"  {'ready   ' if entry['kind'] == 'ready' else 'TEMPLATE'} {entry['file']}")
    out("  ready    README.md, manifest.json")
    if ctx.endpoint == DEFAULT_ENDPOINT:
        out("note: the files point at http://127.0.0.1:4318. For a fleet, pass --endpoint with the URL of a "
            "receiver every machine can reach.")
    if policy_text is None:
        out("note: no --policy was given, so the pack only sets up telemetry and enforces nothing.")
    if dry:
        out(f"\nDry run: {len(pack.files)} file(s) would be written to {out_dir}. Nothing was changed.")
        return 0
    write(pack, out_dir)
    out(f"\nWrote {len(pack.files)} file(s) to {out_dir}. Nothing outside that directory was touched.")
    out(f"Next: read {out_dir / 'README.md'}")
    return 0
