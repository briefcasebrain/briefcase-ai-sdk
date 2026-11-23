"""Plan, apply, undo and verify for ``briefcase-ai connect``.

Pure orchestration over :mod:`briefcase.connect.adapters` and
:mod:`briefcase.connect.edits`. Printing and prompting are injected so the CLI
and the tests drive the same code.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from briefcase.connect import edits
from briefcase.connect import mcp as mcp_payloads
from briefcase.connect.adapters import Adapter, registry, version_in_range
from briefcase.connect.edits import CHANGE, NOOP, REFUSED, Command, FileEdit, Manual, Resolved

Printer = Callable[[str], None]


@dataclass
class Plan:
    tools: List[str] = field(default_factory=list)
    resolved: List[Resolved] = field(default_factory=list)
    commands: List[Command] = field(default_factory=list)
    done_commands: List[Command] = field(default_factory=list)
    manual: List[Manual] = field(default_factory=list)

    @property
    def changes(self) -> List[Resolved]:
        return [r for r in self.resolved if r.status == CHANGE]

    @property
    def has_work(self) -> bool:
        return bool(self.changes or self.commands)

    def needs_protobuf(self, adapters: Dict[str, Adapter]) -> List[str]:
        touched = {r.edit.tool for r in self.resolved} | {m.tool for m in self.manual}
        return [adapters[t].name for t in self.tools if t in touched and adapters[t].needs_protobuf]


def detected_tools(adapters: Optional[Dict[str, Adapter]] = None) -> List[str]:
    adapters = adapters or registry()
    return [name for name, adapter in adapters.items() if adapter.detect().installed]


def run_command(argv: List[str]) -> Tuple[int, str]:
    """Run one planned command. An argument list, never a shell string."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def build_plan(tools: List[str], ctx: Any, adapters: Optional[Dict[str, Adapter]] = None,
               journal: Optional[Dict[str, Any]] = None) -> Plan:
    adapters = adapters or registry()
    journal = journal if journal is not None else edits.load_journal()
    ran = {tuple(c["argv"]) for c in journal.get("commands", []) if _journalled(journal, c["tool"], c["argv"])}
    plan = Plan(tools=list(tools))
    for name in tools:
        for item in adapters[name].plan(ctx[name] if isinstance(ctx, dict) else ctx):
            if isinstance(item, FileEdit):
                resolved = edits.resolve(item)
                if resolved.status == REFUSED:
                    plan.manual.append(Manual(name, f"Add to {item.path}", edits.snippet(item), resolved.reason))
                plan.resolved.append(resolved)
            elif isinstance(item, Command):
                (plan.done_commands if tuple(item.argv) in ran else plan.commands).append(item)
            else:
                plan.manual.append(item)
    return plan


def render_plan(plan: Plan, adapters: Dict[str, Adapter], out: Printer) -> None:
    for name in plan.tools:
        adapter = adapters[name]
        tag = "" if adapter.mode == "automatic" else f" [{adapter.mode}]"
        out(f"\n== {adapter.name}{tag} ==")
        shown = False
        for resolved in (r for r in plan.resolved if r.edit.tool == name):
            shown = True
            edit = resolved.edit
            if resolved.status == REFUSED:
                out(f"  skip   {edit.path}: {resolved.reason} (snippet below)")
                continue
            verb = "ok    " if resolved.status == NOOP else ("write " if resolved.existed else "create")
            suffix = " (already set, nothing to do)" if resolved.status == NOOP else ""
            out(f"  {verb} {edit.path}{suffix}")
            out(f"         {edit.purpose}")
            for note in edit.notes + resolved.warnings:
                out(f"         note: {note}")
            if resolved.status == CHANGE:
                for line in edits.render_diff(resolved).splitlines():
                    out(f"    {line}")
        for command in plan.commands:
            if command.tool == name:
                shown = True
                out(f"  run    {mcp_payloads.shell(command.argv)}")
                out(f"         {command.purpose}")
        for command in plan.done_commands:
            if command.tool == name:
                shown = True
                out(f"  ok     {mcp_payloads.shell(command.argv)} (already run)")
        for manual in plan.manual:
            if manual.tool == name:
                shown = True
                out(f"  manual {manual.title}")
                if manual.reason:
                    out(f"         why: {manual.reason}")
                for line in manual.text.splitlines():
                    out(f"      {line}")
        if not shown:
            out("  nothing to do")


def _registration_key(argv: List[str]) -> Dict[str, Any]:
    """Where a ``claude mcp add`` lands: its scope, the active Claude profile, and (for project and local
    scope) the directory, which together name the config file."""
    scope = argv[4] if len(argv) > 4 else "user"
    return {"config_dir": os.environ.get("CLAUDE_CONFIG_DIR"), "cwd": None if scope == "user" else os.getcwd()}


def _journalled(journal: Dict[str, Any], tool: str, argv: List[str]) -> bool:
    """Did an earlier connect of this SDK register ``argv[:6]`` (same name and scope) in the same config file?"""
    key = _registration_key(argv)
    return any(c["tool"] == tool and c["argv"][:6] == argv[:6]
               and c.get("config_dir") == key["config_dir"] and c.get("cwd") == key["cwd"]
               for c in journal.get("commands", []))


def apply_plan(plan: Plan, ctx: Any, out: Printer,
               runner: Optional[Callable[[List[str]], Tuple[int, str]]] = None,
               skipped_is_failure: bool = False) -> int:
    """Write the plan. Returns the number of failures. An MCP registration that someone else made is skipped
    (not a failure) unless ``skipped_is_failure`` is set, as for ``mcp install``, whose whole job is that
    registration."""
    runner = runner or run_command
    journal = edits.load_journal()
    failures = 0
    for resolved in plan.changes:
        try:
            backup = edits.apply(resolved, journal)
        except (edits.EditRefused, OSError) as exc:
            failures += 1
            out(f"  failed {resolved.edit.path}: {exc}")
            continue
        out(f"  wrote  {resolved.target}" + (f" (backup: {backup})" if backup else ""))
    for command in plan.commands:
        code, output = runner(command.argv)
        if code != 0 and command.undo_hint and "already exists" in output:
            scope = command.argv[4] if len(command.argv) > 4 else "user"
            name = command.argv[5] if len(command.argv) > 5 else ""
            if _journalled(journal, command.tool, command.argv):
                # We registered it, so replacing it loses nothing of anyone else's.
                out(f"  replace {mcp_payloads.shell(command.undo_hint)}")
                if runner(command.undo_hint)[0] == 0:
                    code, output = runner(command.argv)
            else:
                # Someone else's entry: never touched, never printed (its args and env may hold credentials).
                failures += 1 if skipped_is_failure else 0
                out(f"  {'failed' if skipped_is_failure else 'skip  '} an MCP server named {name!r} is already "
                    f"registered in {scope} scope and was not "
                    f"registered by briefcase, so it was left as it is; to use briefcase-ai-mcp, run "
                    f"`{mcp_payloads.shell(command.undo_hint)}` and then `{mcp_payloads.shell(command.argv)}`")
                continue
        if code != 0:
            failures += 1
            out(f"  failed {mcp_payloads.shell(command.argv)}: {output or f'exit {code}'}")
            continue
        out(f"  ran    {mcp_payloads.shell(command.argv)}")
        journal["commands"] = [c for c in journal["commands"] if not (
            c["tool"] == command.tool and c["argv"][:6] == command.argv[:6]
            and c.get("config_dir") == _registration_key(command.argv)["config_dir"]
            and c.get("cwd") == _registration_key(command.argv)["cwd"])]  # same registration, older command
        journal["commands"].append({
            "tool": command.tool, "argv": command.argv, "undo_hint": command.undo_hint,
            **_registration_key(command.argv),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
    touched = {r.edit.tool for r in plan.resolved if r.status in (CHANGE, NOOP)} | {
        c.tool for c in plan.commands + plan.done_commands}
    for name in touched:
        mine = ctx[name] if isinstance(ctx, dict) else ctx
        prior = journal["tools"].get(name) or {}
        applied = mine.telemetry or not prior  # a telemetry-free run (mcp install) keeps what was recorded
        journal["tools"][name] = {
            "scope": mine.scope,
            "endpoint": mine.endpoint if applied else prior.get("endpoint", mine.endpoint),
            "capture_content": mine.capture_content if applied else prior.get("capture_content", False),
            "telemetry": mine.telemetry or bool(prior.get("telemetry", False)),
            "mcp": mine.mcp, "connected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    edits.save_journal(journal)
    return failures


def undo(tools: Optional[List[str]], out: Printer, dry_run: bool = False) -> int:
    """Remove what connect wrote for ``tools`` (everything when ``None``).

    Returns how many journalled files were visited."""
    journal = edits.load_journal()
    visited = 0
    for path in list(journal["files"]):
        entry = journal["files"][path]
        owners = {op.get("tool") for op in entry["ops"]}
        for tool in (sorted(t for t in owners if t) if tools is None else [t for t in tools if t in owners]):
            visited += 1
            messages, done = edits.undo_entry(path, entry, tool, dry_run=dry_run)
            for message in messages:
                out(("  would: " if dry_run else "  ") + message)
            if done and not dry_run:
                del journal["files"][path]
                break
    keep = []
    for command in journal["commands"]:
        if tools is None or command["tool"] in tools:
            visited += 1
            if command.get("undo_hint"):
                out("  to remove the MCP registration, run: " + mcp_payloads.shell(command["undo_hint"]))
            else:
                out("  not undone automatically: " + mcp_payloads.shell(command["argv"])
                    + " (remove the `briefcase` MCP server in the tool's settings)")
        else:
            keep.append(command)
    if not dry_run:
        journal["commands"] = keep
        for name in list(journal["tools"]):
            if tools is None or name in tools:
                del journal["tools"][name]
        edits.save_journal(journal)
    return visited


def status(adapters: Optional[Dict[str, Adapter]] = None) -> List[Dict[str, Any]]:
    """One row per tool: detected, connected, and whether our keys survive."""
    adapters = adapters or registry()
    journal = edits.load_journal()
    rows = []
    for name, adapter in adapters.items():
        detection = adapter.detect()
        checks: List[Tuple[bool, str]] = []
        for path, entry in journal["files"].items():
            checks.extend(edits.check_entry(path, entry, name))
        commands = [c for c in journal["commands"] if c["tool"] == name]
        connected = name in journal["tools"] or bool(checks or commands)
        rows.append({
            "tool": name, "name": adapter.name, "mode": adapter.mode,
            "detected": detection.installed, "reasons": detection.reasons,
            "connected": connected, "checks": checks, "commands": commands,
            "settings": journal["tools"].get(name, {}),
        })
    return rows


def doctor_checks(adapters: Optional[Dict[str, Adapter]] = None,
                  probe: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None) -> List[Tuple[str, str, str]]:
    """``(severity, label, detail)`` rows for ``briefcase-ai doctor --connect``."""
    adapters = adapters or registry()
    out: List[Tuple[str, str, str]] = []
    rows = [row for row in status(adapters) if row["connected"]]
    if not rows:
        return [("warn", "connect", "no tools connected yet; run: briefcase-ai connect")]
    endpoints = set()
    for row in rows:
        adapter = adapters[row["tool"]]
        label = f"connect {row['tool']}"
        missing = [what for ok, what in row["checks"] if not ok]
        if missing:
            out.append(("fail", label, "keys no longer present: " + "; ".join(missing)
                        + " (run: briefcase-ai connect " + row["tool"] + ")"))
        else:
            out.append(("ok", label, f"{len(row['checks'])} key(s) present"
                        + (f", {len(row['commands'])} command(s) run" if row["commands"] else "")))
        version = adapter.version()
        in_range = version_in_range(version, adapter.data.get("supported"))
        if in_range is False:
            supported = adapter.data["supported"]
            out.append(("warn", f"{label} version",
                        f"{version} is outside the supported range >= {supported.get('min')}, "
                        f"< {supported.get('below')} (formats verified {adapter.verified_on})"))
        elif version:
            out.append(("ok", f"{label} version", f"{version} (formats verified {adapter.verified_on}; "
                        "no tested range recorded)" if in_range is None else f"{version} (in supported range)"))
        else:
            out.append(("warn", f"{label} version", "could not determine the installed version"))
        overrides = getattr(adapter, "managed_overrides", None)
        if overrides is not None:
            keys = [op["path"][-1] for entry in edits.load_journal()["files"].values()
                    for op in entry["ops"] if op.get("tool") == row["tool"] and op.get("path", [""])[0] == "env"]
            for path, hit in overrides(keys):
                out.append(("warn", f"{label} managed settings",
                            f"{path} overrides {', '.join(hit)}; your user values are ignored"))
        if row["settings"].get("endpoint"):
            endpoints.add(row["settings"]["endpoint"])
        if row["settings"].get("capture_content"):
            out.append(("warn", f"{label} content", "connected with --capture-content: prompts and tool "
                        "details are being exported"))
    if probe is not None:
        for endpoint in sorted(endpoints):
            health = probe(endpoint)
            if health is None:
                out.append(("warn", "receiver",
                            f"not reachable at {endpoint}; start it with: briefcase-ai receive --detach"))
                continue
            out.append(("ok", "receiver", f"reachable at {endpoint}"))
            needy = [adapters[r["tool"]].name for r in rows if adapters[r["tool"]].needs_protobuf
                     and r["settings"].get("endpoint") == endpoint]
            if needy and not health.get("protobuf"):
                out.append(("warn", "receiver protobuf", f"{', '.join(needy)} send protobuf but the receiver "
                            "cannot decode it; run: pip install 'briefcase-ai[receiver]' and restart it"))
    return out
