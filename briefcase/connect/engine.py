"""Plan, apply, undo and verify for ``briefcase-ai connect``.

Pure orchestration over :mod:`briefcase.connect.adapters` and
:mod:`briefcase.connect.edits`. Printing and prompting are injected so the CLI
and the tests drive the same code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from briefcase.connect import edits
from briefcase.connect import mcp as mcp_payloads
from briefcase.connect.adapters import Adapter, registry, version_in_range
from briefcase.connect.edits import CHANGE, NOOP, REFUSED, Command, FileEdit, Manual, Resolved

Printer = Callable[[str], None]

CAPTURE_WARNING = (
    "warning: --capture-content turns on content export in the tools themselves. Prompts, tool "
    "parameters and commands will leave the tool and reach the receiver. The receiver still "
    "redacts them unless it is started with --keep-content."
)


@dataclass
class Blocked:
    """A target that is not written. ``blocker`` is ``foreign`` (present, not written by us), ``modified`` (ours
    once, changed or gone since) or ``differs`` (ours and intact, but this invocation asked for other settings)."""

    tool: str
    kind: str
    blocker: str
    text: str


@dataclass
class Plan:
    tools: List[str] = field(default_factory=list)
    resolved: List[Resolved] = field(default_factory=list)
    commands: List[Command] = field(default_factory=list)
    done_commands: List[Command] = field(default_factory=list)
    manual: List[Manual] = field(default_factory=list)
    blocked: List[Blocked] = field(default_factory=list)

    @property
    def differs(self) -> bool:
        return any(b.blocker == "differs" for b in self.blocked)

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


def _undo_then_connect(tool: str) -> str:
    if tool.startswith("policy:"):  # the enforcement hooks (briefcase.policy.install.PREFIX) have their own commands
        name = tool[len("policy:"):]
        return f"run `briefcase-ai policy off {name}` and then `briefcase-ai policy init {name}` again"
    return f"run `briefcase-ai connect --undo {tool}` and then connect again"


def _headers_sha(headers: Dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(headers.items())).encode("utf-8")).hexdigest()


def _differences(rec: Dict[str, Any], kind: str, ctx: Any, edit: Optional[FileEdit]) -> List[str]:
    """Which settings this invocation named differ from what the target was written with (names only; a header
    value is never printed)."""
    given = getattr(ctx, "given", {}) or {}
    found = []
    if "scope" in given and given["scope"] != rec.get("scope"):
        found.append("scope")
    if kind == "telemetry":
        if "endpoint" in given and given["endpoint"] != rec.get("endpoint"):
            found.append("endpoint")
        if "headers" in given and _headers_sha(given["headers"]) != rec.get("headers_sha"):
            found.append("headers")
        if given.get("capture_content") and not rec.get("capture_content"):
            found.append("capture-content")
    if getattr(ctx, "strict", False) and edit is not None and not edits.same_content(edit, rec):
        found.append("settings")
    return found


def _record(tool: str, kind: str, target: str, ctx: Any, **more: Any) -> Dict[str, Any]:
    return {"tool": tool, "kind": kind, "scope": ctx.scope, "target": target, "endpoint": ctx.endpoint,
            "headers_sha": _headers_sha(ctx.headers), "capture_content": bool(ctx.capture_content),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **more}


def _plan_file(plan: Plan, item: FileEdit, ctx: Any, journal: Dict[str, Any]) -> None:
    resolved = edits.resolve(item)
    tkey = target_key(item.tool, item.kind, str(resolved.target))
    rec = journal["targets"].get(tkey)
    where = f"{item.path}"
    if rec is None:
        if edits.holds_any(item, resolved.old_text):
            other = next((t["kind"] for t in journal["targets"].values()
                          if t["tool"] == item.tool and t["target"] == str(resolved.target)), None)
            plan.blocked.append(Blocked(item.tool, item.kind, "foreign", (
                f"{where}: briefcase-ai wrote it for {other} and does not rewrite it; "
                f"{_undo_then_connect(item.tool)}" if other else
                f"{where}: already holds an entry briefcase did not write; it was left as it is (its contents are "
                f"not shown). To let briefcase-ai manage it, remove it yourself and connect again")))
            return
        if resolved.status == REFUSED:
            plan.manual.append(Manual(item.tool, f"Add to {item.path}", edits.snippet(item), resolved.reason))
        plan.resolved.append(resolved)
        return
    if edits.content_hash(item.format, rec.get("ops", []), resolved.old_text) != rec.get("hash"):
        plan.blocked.append(Blocked(item.tool, item.kind, "modified", (
            f"{where}: modified since connect, so it is no longer treated as briefcase's and was left as it is; "
            f"{_undo_then_connect(item.tool)}")))
        return
    diffs = _differences(rec, item.kind, ctx, item)
    if diffs:
        plan.blocked.append(Blocked(item.tool, item.kind, "differs", (
            f"{where}: connected with other {', '.join(diffs)}; nothing was changed. To change it, "
            f"{_undo_then_connect(item.tool)} with the new settings")))
        return
    plan.resolved.append(edits.Resolved(item, NOOP, resolved.target, old_text=resolved.old_text,
                                        new_text=resolved.old_text, existed=True, reason="already connected"))


def registration_state(argv: List[str], runner: Callable[[List[str]], Tuple[int, str]]) -> str:
    """Does the tool's own registry still hold the command we registered? ``ok``, ``missing``, ``differs`` or
    ``unknown`` (cannot tell: it is then taken to be as we left it)."""
    if argv[:1] not in (["claude"], ["codex"]) or "--" not in argv:
        return "unknown"
    name = argv[5] if argv[:1] == ["claude"] else argv[3]
    code, output = runner([argv[0], "mcp", "get", name])
    if code != 0:
        return "missing" if re.search(r"no mcp server|not found|does not exist", output, re.I) else "unknown"
    return "ok" if all(token in output for token in argv[argv.index("--") + 1:]) else "differs"


def _plan_command(plan: Plan, item: Command, ctx: Any, journal: Dict[str, Any],
                  runner: Callable[[List[str]], Tuple[int, str]]) -> None:
    tkey = target_key(item.tool, "mcp", command_target(item.argv))
    rec = journal["targets"].get(tkey)
    if rec is None:
        plan.commands.append(item)
        return
    shown = mcp_payloads.shell(rec["argv"])
    if registration_state(rec["argv"], runner) in ("missing", "differs"):
        plan.blocked.append(Blocked(item.tool, "mcp", "modified", (
            f"{shown}: the registration is gone or was changed since connect, so it is no longer treated as "
            f"briefcase's and was left as it is; {_undo_then_connect(item.tool)}")))
        return
    diffs = _differences(rec, "mcp", ctx, None)
    if diffs:
        plan.blocked.append(Blocked(item.tool, "mcp", "differs", (
            f"{shown}: connected with other {', '.join(diffs)}; nothing was changed. To change it, "
            f"{_undo_then_connect(item.tool)} with the new settings")))
        return
    plan.done_commands.append(Command(item.tool, list(rec["argv"]), item.purpose, undo_hint=item.undo_hint))


def build_plan(tools: List[str], ctx: Any, adapters: Optional[Dict[str, Adapter]] = None,
               journal: Optional[Dict[str, Any]] = None,
               runner: Optional[Callable[[List[str]], Tuple[int, str]]] = None) -> Plan:
    """What connecting ``tools`` would do. A target that is not present and not journalled is written; one that
    is journalled and still exactly as written is reported as connected; every other case is left untouched."""
    adapters = adapters or registry()
    journal = journal if journal is not None else edits.load_journal()
    runner = runner or run_command
    plan = Plan(tools=list(tools))
    for name in tools:
        mine = ctx[name] if isinstance(ctx, dict) else ctx
        for item in adapters[name].plan(mine):
            if isinstance(item, FileEdit):
                _plan_file(plan, item, mine, journal)
            elif isinstance(item, Command):
                _plan_command(plan, item, mine, journal, runner)
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
            suffix = f" ({resolved.reason or 'already set, nothing to do'})" if resolved.status == NOOP else ""
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
                out(f"  ok     {mcp_payloads.shell(command.argv)} (already connected)")
        for blocked in plan.blocked:
            if blocked.tool == name:
                shown = True
                out(f"  {'differs' if blocked.blocker == 'differs' else 'skip   '} {blocked.text}")
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


def registration(argv: List[str]) -> Tuple[str, Tuple[str, ...]]:
    """``(scope, name key)`` of a registration command in its own CLI's argument form: ``claude mcp add --scope S
    NAME --`` has a scope; ``codex mcp add NAME --`` is global (its ``--`` is not a scope)."""
    if argv[:1] == ["claude"]:
        return (argv[4] if len(argv) > 4 else "user"), tuple(argv[:6])
    if argv[:1] == ["codex"]:
        return "global", tuple(argv[:4])
    return "user", tuple(argv[:3])


def identity(argv: List[str]) -> Dict[str, Any]:
    """Where that registration lands: the profile (``CLAUDE_CONFIG_DIR`` or ``CODEX_HOME``) and, for project and
    local scope, the directory. Two commands with different identities never share state."""
    scope, _ = registration(argv)
    profile = os.environ.get("CODEX_HOME" if argv[:1] == ["codex"] else "CLAUDE_CONFIG_DIR")
    return {"profile": profile, "cwd": None if scope in ("user", "global") else os.getcwd()}


def command_target(argv: List[str], ident: Optional[Dict[str, Any]] = None) -> str:
    """The registry a registration command writes to: its name and scope, the profile and, for project and local
    scope, the directory."""
    ident = ident or identity(argv)
    return "|".join([" ".join(registration(argv)[1]), str(ident["profile"]), str(ident["cwd"])])


def target_key(tool: str, kind: str, target: str) -> str:
    """The unit the journal reasons about: one tool, one purpose, one absolute file or registry."""
    return "|".join([tool, kind, target])


def apply_plan(plan: Plan, ctx: Any, out: Printer,
               runner: Optional[Callable[[List[str]], Tuple[int, str]]] = None,
               skipped_is_failure: bool = False, capture_warning: bool = True) -> int:
    """Write the plan. Returns the number of failures. A registration that someone else made is skipped (not a
    failure) unless ``skipped_is_failure`` is set, as for ``mcp install``, whose whole job is that registration.
    Nothing that already exists is ever changed."""
    runner = runner or run_command
    journal = edits.load_journal()
    failures = 0
    warned = False
    for resolved in plan.changes:
        edit = resolved.edit
        mine = ctx[edit.tool] if isinstance(ctx, dict) else ctx
        if capture_warning and mine.capture_content and edit.kind == "telemetry" and not warned:
            print(CAPTURE_WARNING, file=sys.stderr)
            warned = True
        try:
            backup = edits.apply(resolved, journal)
        except (edits.EditRefused, OSError) as exc:
            failures += 1
            out(f"  failed {edit.path}: {exc}")
            continue
        out(f"  wrote  {resolved.target}" + (f" (backup: {backup})" if backup else ""))
        journal["targets"][target_key(edit.tool, edit.kind, str(resolved.target))] = _record(
            edit.tool, edit.kind, str(resolved.target), mine, ops=edits.written_ops(resolved.applied),
            hash=edits.content_hash(edit.format, edits.written_ops(resolved.applied), resolved.new_text))
    for command in plan.commands:
        code, output = runner(command.argv)
        if code != 0 and command.undo_hint and "already exists" in output:
            # Someone else's entry: never touched, never printed (its args and env may hold credentials).
            scope, key = registration(command.argv)
            name = key[-1] if len(key) > 5 else (key[3] if len(key) > 3 else "")
            failures += 1 if skipped_is_failure else 0
            out(f"  {'failed' if skipped_is_failure else 'skip  '} an MCP server named {name!r} is already "
                f"registered in {scope} scope and briefcase did not write it, so it was left as it is; to use "
                f"briefcase-ai-mcp, run `{mcp_payloads.shell(command.undo_hint)}` and then "
                f"`{mcp_payloads.shell(command.argv)}`")
            continue
        if code != 0:
            failures += 1
            out(f"  failed {mcp_payloads.shell(command.argv)}: {output or f'exit {code}'}")
            continue
        out(f"  ran    {mcp_payloads.shell(command.argv)}")
        mine = ctx[command.tool] if isinstance(ctx, dict) else ctx
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        journal["commands"].append({"tool": command.tool, "argv": command.argv, "undo_hint": command.undo_hint,
                                    **identity(command.argv), "at": stamp})
        journal["targets"][target_key(command.tool, "mcp", command_target(command.argv))] = _record(
            command.tool, "mcp", command_target(command.argv), mine, argv=command.argv,
            undo_hint=command.undo_hint)
    edits.save_journal(journal)
    return failures


def undo(tools: Optional[List[str]], out: Printer, dry_run: bool = False) -> int:
    """Remove what connect wrote for ``tools`` (everything when ``None``), but only where the file still holds
    exactly what was written; what was changed since is left as it is and reported.

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
        for key in [k for k, v in journal["targets"].items() if tools is None or v["tool"] in tools]:
            del journal["targets"][key]
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
        targets = [t for t in journal["targets"].values() if t["tool"] == name]
        telemetry = [t for t in targets if t["kind"] == "telemetry"]
        settings: Dict[str, Any] = {}
        if targets:
            settings = {"scope": targets[0]["scope"], "telemetry": bool(telemetry),
                        "mcp": any(t["kind"] == "mcp" for t in targets),
                        "capture_content": any(t.get("capture_content") for t in telemetry)}
            if telemetry:
                settings["endpoint"] = telemetry[0]["endpoint"]
        rows.append({
            "tool": name, "name": adapter.name, "mode": adapter.mode,
            "detected": detection.installed, "reasons": detection.reasons,
            "connected": bool(targets or checks or commands), "checks": checks, "commands": commands,
            "settings": settings,
        })
    return rows


def doctor_checks(adapters: Optional[Dict[str, Adapter]] = None,
                  probe: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None) -> List[Tuple[str, str, str]]:
    """``(severity, label, detail)`` rows for ``briefcase-ai doctor --connect``."""
    adapters = adapters or registry()
    out: List[Tuple[str, str, str]] = []
    try:
        rows = [row for row in status(adapters) if row["connected"]]
    except edits.LegacyJournal as exc:
        return [("fail", "connect", f"{exc.path} was written by an earlier build and is not read; remove what it "
                 "recorded by hand, delete it and connect again (run `briefcase-ai connect` for the list)")]
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
