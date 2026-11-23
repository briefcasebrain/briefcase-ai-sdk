"""``briefcase-ai connect``: set up coding tools to report to briefcase, in one command.

    briefcase-ai connect                 # detect tools, show the plan and diff, ask once, write
    briefcase-ai connect claude-code     # one tool
    briefcase-ai connect --undo          # remove exactly what connect wrote
    briefcase-ai connect list | status
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from briefcase.connect import edits, engine
from briefcase.connect.adapters import DEFAULT_ENDPOINT, Adapter, Context, registry, resolve_names

CAPTURE_WARNING = (
    "warning: --capture-content turns on content export in the tools themselves. Prompts, tool "
    "parameters and commands will leave the tool and reach the receiver. The receiver still "
    "redacts them unless it is started with --keep-content."
)
RECEIVER_EXTRA = "pip install 'briefcase-ai[receiver]'"

GROUP = "connect"


def register(sub) -> None:
    p = sub.add_parser(
        "connect",
        help="Configure coding tools (Claude Code, Codex, Cursor, Copilot, ...) to report to briefcase.",
        description="Writes each detected tool's telemetry and MCP configuration for you. "
                    "Use `briefcase-ai connect list` to see supported tools and `status` to see what is connected.",
    )
    p.add_argument("tools", nargs="*", metavar="TOOL",
                   help="Tools to connect, or `list` / `status`. Default: every detected tool.")
    p.add_argument("--all", action="store_true", help="Every supported tool, detected or not.")
    p.add_argument("--scope", choices=("user", "project"), default="user",
                   help="Write user-level config (default) or config in the current project.")
    p.add_argument("--endpoint", default=None, help=f"Receiver URL (default {DEFAULT_ENDPOINT}).")
    p.add_argument("--headers", action="append", default=[], metavar="K=V",
                   help="Header the tool should send to the receiver (repeatable), e.g. Authorization=Bearer T.")
    p.add_argument("--mcp", dest="mcp", action="store_true", default=True,
                   help="Also register the briefcase MCP server (default).")
    p.add_argument("--no-mcp", dest="mcp", action="store_false", help="Telemetry only.")
    p.add_argument("--capture-content", action="store_true",
                   help="Also enable prompt/tool content export in the tools. Off by default.")
    p.add_argument("--dry-run", dest="connect_dry_run", action="store_true",
                   help="Show the plan and diff; write nothing.")
    p.add_argument("--undo", action="store_true", help="Remove what connect wrote (all tools, or the named ones).")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    p.add_argument("--force", action="store_true", help="Write even for an unrecognised tool version.")
    p.add_argument("--start", action="store_true", help="Start the receiver in the background afterwards.")
    # Enforcement and fleet rollout live in briefcase.policy; these flags only route to it.
    p.add_argument("--enforce", action="store_true",
                   help="Also write a policy and install the enforcement hooks (same as `briefcase-ai policy init`).")
    p.add_argument("--preset", choices=("observe", "guarded", "strict"), default=None,
                   help="With --enforce: the preset for a new policy file (default guarded).")
    p.add_argument("--managed", action="store_true",
                   help="Write an admin rollout pack to --out instead of configuring this machine.")
    p.add_argument("--out", metavar="DIR", default=None, help="With --managed: the directory to write the pack to.")
    p.add_argument("--policy", metavar="FILE", default=None,
                   help="With --managed: a policy file to enforce on every machine.")
    p.add_argument("--tools", nargs="+", metavar="TOOL", default=None, dest="managed_tools",
                   help="With --managed: the tools to include (default: claude-code cursor codex cowork).")
    p.set_defaults(func=cmd_connect, telemetry=True)


def _parse_headers(pairs: List[str]) -> Dict[str, str]:
    headers = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--headers expects K=V, got {pair!r}")
        headers[key.strip()] = value
    return headers


def _confirm(question: str, assume_yes: bool, default: bool = True,
             ask: Optional[Callable[[str], str]] = None) -> Optional[bool]:
    """``True``/``False``, or ``None`` when there is nobody to ask."""
    if assume_yes:
        return True
    if ask is None:
        if not sys.stdin or not sys.stdin.isatty():
            return None
        ask = input
    try:
        answer = ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return default if not answer else answer in ("y", "yes")


def cmd_list(adapters: Dict[str, Adapter], out=print) -> int:
    out(f"{'TOOL':<16} {'MODE':<13} {'DETECTED':<9} WHAT CONNECT DOES")
    what = {
        "claude-code": "native OTel in ~/.claude/settings.json (http/json) + MCP",
        "cowork": "prints the admin steps (org-level setting)",
        "codex": "[otel] block in ~/.codex/config.toml (protobuf) + MCP",
        "cursor": "hooks in ~/.cursor/hooks.json + MCP in ~/.cursor/mcp.json",
        "vscode-copilot": "Copilot Chat OTel settings in VS Code user settings + MCP",
        "copilot-cli": "prints env vars and the MCP entry",
        "gemini-cli": "telemetry block in .gemini/settings.json (experimental)",
        "cline": "prints env vars",
        "windsurf": "prints the MCP server command",
        "opencode": "prints the MCP server command",
    }
    for name, adapter in adapters.items():
        detected = "yes" if adapter.detect().installed else "no"
        out(f"{name:<16} {adapter.mode:<13} {detected:<9} {what.get(name, '')}")
    out("\nDetails and formats: docs/integrations/README.md")
    return 0


def cmd_status(adapters: Dict[str, Adapter], out=print) -> int:
    from briefcase.receiver import daemon

    rows = engine.status(adapters)
    connected = [row for row in rows if row["connected"]]
    if not connected:
        out("No tools connected yet. Run: briefcase-ai connect")
    for row in connected:
        missing = [what for ok, what in row["checks"] if not ok]
        state = "connected" if not missing else "DRIFTED"
        settings = row["settings"]
        out(f"{row['tool']:<16} {state:<10} endpoint={settings.get('endpoint', '?')} "
            f"content={'on' if settings.get('capture_content') else 'off'}")
        for ok, what in row["checks"]:
            out(f"    {'ok     ' if ok else 'MISSING'} {what}")
        for command in row["commands"]:
            out(f"    ran     {' '.join(command['argv'][:4])} ...")
    others = [row["tool"] for row in rows if row["detected"] and not row["connected"]]
    if others:
        out(f"Detected but not connected: {', '.join(others)}")
    endpoints = {row["settings"].get("endpoint") for row in connected} - {None} or {DEFAULT_ENDPOINT}
    for endpoint in sorted(endpoints):
        up = daemon.probe(endpoint) is not None
        out(f"receiver {endpoint}: {'up' if up else 'not running (briefcase-ai receive --detach)'}")
    return 0


def run_undo(args, out=print) -> int:
    try:
        tools = resolve_names(args.tools) if args.tools else None
    except KeyError as exc:
        print(f"error: unknown tool {exc.args[0]!r}; see `briefcase-ai connect list`", file=sys.stderr)
        return 2
    dry = _dry_run(args)
    out("Undo plan:" if dry else "Undoing:")
    visited = engine.undo(tools, out, dry_run=dry)
    if not visited:
        out("  nothing to undo (the journal has no entries" + (" for those tools)" if tools else ")"))
    elif not dry:
        out("Done. Backups of every file connect touched are next to it (*.briefcase-bak-*).")
    return 0


def _dry_run(args) -> bool:
    # The top-level parser already owns a global --dry-run (for `run submit`);
    # accept it in either position.
    return bool(getattr(args, "connect_dry_run", False) or getattr(args, "dry_run", False))


def run_connect(args, adapters: Dict[str, Adapter], out=print, offer_receiver: bool = True,
                ask: Optional[Callable[[str], str]] = None, runner: Any = None) -> int:
    try:
        headers = _parse_headers(getattr(args, "headers", []) or [])
        named = resolve_names(args.tools)
    except KeyError as exc:
        print(f"error: unknown tool {exc.args[0]!r}; see `briefcase-ai connect list`", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "all", False):
        tools = list(adapters)
    elif named:
        tools = named
    else:
        tools = engine.detected_tools(adapters)
        if not tools:
            out("No supported coding tool was detected on this machine.")
            out("Name one explicitly (briefcase-ai connect claude-code) or see: briefcase-ai connect list")
            return 1
        out("Detected: " + ", ".join(adapters[name].name for name in tools))
    journal = edits.load_journal()
    recorded = journal["tools"]
    explicit = bool(args.endpoint or headers or args.capture_content)
    telemetry = getattr(args, "telemetry", True)
    ctxs: Dict[str, Context] = {}
    kept = []
    for name in tools:
        known = recorded.get(name) or {}
        ctx_for = Context(
            scope=args.scope, headers=headers, mcp=args.mcp, force=getattr(args, "force", False),
            project_dir=Path.cwd(),
            endpoint=(args.endpoint or known.get("endpoint") or DEFAULT_ENDPOINT).rstrip("/"),
            telemetry=telemetry,
            capture_content=bool(args.capture_content or (known.get("capture_content") and not explicit)),
        )
        # A connected tool keeps its own destination, headers and capture setting unless one of those flags
        # is given, but only when the files this scope and project would write already hold them.
        if (known and known.get("telemetry", True) and not explicit and telemetry
                and _telemetry_held(name, ctx_for, adapters, journal)):
            ctx_for.telemetry = False
            kept.append(f"{adapters[name].name} -> {known.get('endpoint') or DEFAULT_ENDPOINT}")
        ctxs[name] = ctx_for
    if kept:
        out("Keeping the telemetry destination each tool already has: " + ", ".join(kept)
            + " (pass --endpoint, --headers or --capture-content to change it)")
    ctx = next((c for c in ctxs.values() if c.telemetry), next(iter(ctxs.values())))
    if args.capture_content:
        print(CAPTURE_WARNING, file=sys.stderr)
    plan = engine.build_plan(tools, ctxs, adapters)
    engine.render_plan(plan, adapters, out)
    dry = _dry_run(args)

    if not plan.has_work:
        out("\nNothing to write: everything that can be set up automatically already is."
            if any(r.status == edits.NOOP for r in plan.resolved) or plan.done_commands
            else "\nNothing can be written automatically for these tools; follow the manual steps above.")
    elif dry:
        out(f"\nDry run: {len(plan.changes)} file(s) would be written and {len(plan.commands)} "
            "command(s) run. Nothing was changed.")
    else:
        out("")
        answer = _confirm(f"Write {len(plan.changes)} file(s) and run {len(plan.commands)} command(s)?",
                          args.yes, ask=ask)
        if answer is None:
            print("error: not a terminal, so cannot ask for confirmation; re-run with --yes "
                  "(or --dry-run to only look).", file=sys.stderr)
            return 1
        if not answer:
            out("Aborted; nothing was changed.")
            return 1
        failures = engine.apply_plan(plan, ctxs, out, runner=runner)
        if failures:
            out(f"\n{failures} step(s) failed; the rest were applied. Run `briefcase-ai connect status`.")
        out("\nUndo any time with: briefcase-ai connect --undo")
        if failures:
            return 1
    if dry:
        return 0

    if offer_receiver and (any(c.telemetry for c in ctxs.values()) or (kept and getattr(args, "start", False))):
        _receiver_step(plan, ctx, adapters, args, out, ask)
    return 0


def _telemetry_held(name: str, ctx: Context, adapters, journal: Dict[str, Any]) -> bool:
    """Do the files that connecting ``name`` with ``ctx`` would write (for this scope and project) already hold
    this tool's telemetry settings, as our journal recorded them?"""
    import dataclasses

    full = dataclasses.replace(ctx, telemetry=True, mcp=False)
    plan = engine.build_plan([name], full, adapters, journal)
    edits_planned = [r for r in plan.resolved if r.status != edits.REFUSED]
    if not edits_planned:
        return False
    for resolved in edits_planned:
        entry = journal["files"].get(str(resolved.target))
        if not entry:
            return False
        ours = [op for op in entry["ops"] if op.get("tool") == name]
        if not ours or not all(ok for ok, _ in edits.check_entry(str(resolved.target), entry, name)):
            return False
        edit = resolved.edit
        if edit.format == "toml":
            have = {t for op in ours for t in op.get("tables", [])}
            if not set(edit.toml_tables) <= have:
                return False
        else:
            have_paths = {tuple(op["path"]) for op in ours if "path" in op}
            if not {tuple(op["path"]) for op in edit.ops} <= have_paths:
                return False
    return True


def _receiver_step(plan, ctx: Context, adapters, args, out, ask) -> None:
    from briefcase.receiver import daemon, otlp

    needy = plan.needs_protobuf(adapters)
    if needy and not otlp.protobuf_available():
        out(f"\n{', '.join(needy)} send(s) OTLP as protobuf. To receive from them, run once:")
        out(f"  {RECEIVER_EXTRA}")
        out("(Claude Code is configured for http/json and Cursor uses hooks; both work without it.)")
    if ctx.endpoint != DEFAULT_ENDPOINT:
        out(f"\nTools now report to {ctx.endpoint}; make sure a receiver is listening there.")
        return
    if daemon.probe(ctx.endpoint) is not None:
        out(f"\nThe receiver is already running at {ctx.endpoint}.")
        return
    start = True if getattr(args, "start", False) else (
        None if args.yes else _confirm("Start the briefcase receiver in the background now?", False, ask=ask))
    if start:
        from argparse import Namespace

        from briefcase.cli.receive_commands import start_detached

        out("")
        start_detached(Namespace(port=4318, host="127.0.0.1", to=None, keep_content=False,
                                 metrics=False, token=None, max_body_mb=8.0), out)
    else:
        out("\nStart the receiver when you are ready:")
        out("  briefcase-ai receive            # foreground, records to the console")
        out("  briefcase-ai receive --detach   # background, records to ~/.briefcase/receiver/records.jsonl")
    out("Restart the connected tools so they pick up the new settings.")


def cmd_connect(args, store=None, engine_=None, stack=None) -> int:
    adapters = registry()
    if args.tools and args.tools[0] in ("list", "status") and len(args.tools) == 1:
        return cmd_list(adapters) if args.tools[0] == "list" else cmd_status(adapters)
    if getattr(args, "managed", False):
        from briefcase.policy import managed
        return managed.cmd_managed(args)
    if args.undo:
        return run_undo(args)
    code = run_connect(args, adapters)
    if code == 0 and getattr(args, "enforce", False):
        code = _enforce_step(args)
    return code


def _enforce_step(args) -> int:
    """``connect --enforce``: connect, then ``briefcase-ai policy init`` for the same tools."""
    from argparse import Namespace

    from briefcase.cli import policy_commands
    from briefcase.policy import install

    named = [name for name in resolve_names(args.tools) if name in install.TOOLS]
    print("\n== Policy ==")
    return policy_commands.run_init(Namespace(
        tools=named, preset=getattr(args, "preset", None), scope=args.scope, fail_closed=False, portable=False,
        capture_content=args.capture_content, endpoint=args.endpoint, force=getattr(args, "force", False),
        policy_dry_run=_dry_run(args), dry_run=False, yes=args.yes,
    ))
