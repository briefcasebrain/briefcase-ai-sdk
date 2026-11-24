"""Plan, apply, undo and verify for ``briefcase-ai connect``.

Pure orchestration over :mod:`briefcase.connect.adapters` and
:mod:`briefcase.connect.edits`. Printing and prompting are injected so the CLI
and the tests drive the same code.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from briefcase.connect import edits
from briefcase.connect import mcp as mcp_payloads
from briefcase.connect import mcpreg as _registry
from briefcase.connect.adapters import Adapter, ProfileError, registry, version_in_range
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
    once, changed or gone since), ``differs`` (ours and intact, but this invocation asked for other settings),
    ``unknown`` (a lookup could not say who owns it) or ``unsafe`` (writing it would export content or a credential
    this invocation did not ask for). ``differs`` and ``unsafe`` are failures; the rest are skipped steps."""

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
    endpoints: Dict[str, str] = field(default_factory=dict)  # where each tool's telemetry target reports
    secrets: List[str] = field(default_factory=list)  # supplied values that are never printed, anywhere

    @property
    def differs(self) -> bool:
        return any(b.blocker == "differs" for b in self.blocked)

    @property
    def failing(self) -> bool:
        """Does this plan contain a non-zero result? If so nothing in it is written."""
        return any(b.blocker in ("differs", "unsafe") for b in self.blocked)

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


def _undo_then_connect(tool: str, edited: bool = False) -> str:
    """The recovery steps, to be followed literally. For a target that was edited, undo leaves the edited content
    and forgets it, so the one step that only you can do comes first."""
    if tool.startswith("policy:"):  # the enforcement hooks (briefcase.policy.install.PREFIX) have their own commands
        name = tool[len("policy:"):]
        first = "remove the briefcase hook entry yourself, then " if edited else ""
        return f"{first}run `briefcase-ai policy off {name}` and then `briefcase-ai policy init {name}` again"
    first = "remove the briefcase keys or tables yourself, then " if edited else ""
    return f"{first}run `briefcase-ai connect --undo {tool}` and then connect again"


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


def _wants_more(item: FileEdit, rec: Dict[str, Any]) -> bool:
    """Does this invocation write keys or tables the recorded target never had?"""
    if item.format == "toml":
        return not set(item.toml_tables) <= set(rec.get("tables", []))
    wanted = {tuple(o["path"]) for o in item.ops if o["op"] in ("set", "append")}
    return not wanted <= {tuple(o["path"]) for o in rec.get("ops", [])}


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
        risks = edits.exposure(item, resolved.old_text, getattr(ctx, "given", {}) or {})
        given = getattr(ctx, "given", {}) or {}
        for layer in item.layers:  # settings the tool merges in from managed, project and local files
            risks += edits.layer_risks(item, layer, given)
        risks += edits.environment_risks(item, os.environ)  # what is already exported where the tool will start
        if risks:
            plan.blocked.append(Blocked(item.tool, item.kind, "unsafe", (
                f"{where}: not written, because it already sets {', '.join(risks)}; with the endpoint briefcase-ai "
                "would write that would send prompts or a credential somewhere you did not ask for. Pass "
                "--capture-content or --headers to say you mean it, or remove the key(s) first")))
            return
        if resolved.status == REFUSED:
            plan.manual.append(Manual(item.tool, f"Add to {item.path}", edits.snippet(item), resolved.reason))
        if item.kind == "telemetry":
            plan.endpoints[item.tool] = ctx.endpoint
        plan.resolved.append(resolved)
        return
    if edits.content_hash(item.format, rec.get("ops", []), resolved.old_text, rec.get("tables", [])) \
            != rec.get("hash"):
        plan.blocked.append(Blocked(item.tool, item.kind, "modified", (
            f"{where}: modified since connect, so it is no longer treated as briefcase's and was left as it is; "
            f"{_undo_then_connect(item.tool, edited=True)}")))
        return
    diffs = _differences(rec, item.kind, ctx, item)
    if _wants_more(item, rec):
        diffs.append("keys")
    if diffs:
        plan.blocked.append(Blocked(item.tool, item.kind, "differs", (
            f"{where}: connected with other {', '.join(diffs)}; nothing was changed. To change it, "
            f"{_undo_then_connect(item.tool)} with the new settings")))
        return
    if item.kind == "telemetry":
        plan.endpoints[item.tool] = rec.get("endpoint", ctx.endpoint)
    plan.resolved.append(edits.Resolved(item, NOOP, resolved.target, old_text=resolved.old_text,
                                        new_text=resolved.old_text, existed=True, reason="already connected"))


def registration_state(argv: List[str], runner: Callable[[List[str]], Tuple[int, str]]) -> str:
    """Does the tool's own registry hold exactly the command we registered? ``ours``, ``absent``, ``differs`` or
    ``unknown`` (never ``ours`` when the lookup failed)."""
    return _registry.owned(argv, runner)


def _plan_command(plan: Plan, item: Command, ctx: Any, journal: Dict[str, Any],
                  runner: Callable[[List[str]], Tuple[int, str]]) -> None:
    tkey = target_key(item.tool, "mcp", command_target(item.argv))
    rec = journal["targets"].get(tkey)
    if rec is None:
        state, _ = _registry.lookup(item.argv, runner)
        name = _registry.server_name(item.argv)
        shown = mcp_payloads.shell(item.argv)
        if state == _registry.PRESENT:
            plan.blocked.append(Blocked(item.tool, "mcp", "foreign", (
                f"an MCP server named {name!r} is already registered and briefcase did not write it, so it was "
                f"left as it is (its contents are not shown); to use briefcase-ai-mcp, remove it yourself and "
                f"run `{shown}`")))
            return
        if state == _registry.UNKNOWN and item.argv[:1] and item.argv[0] in _registry.REPLACING:
            plan.blocked.append(Blocked(item.tool, "mcp", "unknown", (
                f"could not tell whether an MCP server named {name!r} is already registered, and `{item.argv[0]} "
                f"mcp add` replaces one silently, so nothing was run; fix the lookup problem and connect again, "
                f"or run `{shown}` yourself")))
            return
        plan.commands.append(item)
        return
    shown = mcp_payloads.shell(rec["argv"])
    state = registration_state(rec["argv"], runner)
    if state in (_registry.ABSENT, _registry.DIFFERS):
        plan.blocked.append(Blocked(item.tool, "mcp", "modified", (
            f"{shown}: the registration is gone or was changed since connect, so it is no longer treated as "
            f"briefcase's and was left as it is; {_undo_then_connect(item.tool, edited=True)}")))
        return
    if state == _registry.UNKNOWN:
        plan.blocked.append(Blocked(item.tool, "mcp", "unknown", (
            f"{shown}: the tool's registry could not be read, so whether this is still briefcase's registration is "
            "unknown; it was left as it is")))
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
        plan.secrets = edits.secret_parts(list(plan.secrets) + list((getattr(mine, "headers", None) or {}).values()))
        try:
            items = adapters[name].plan(mine)
        except ProfileError as exc:
            plan.blocked.append(Blocked(name, "telemetry", "unsafe", str(exc)))
            continue
        for item in items:
            if isinstance(item, FileEdit):
                _plan_file(plan, item, mine, journal)
            elif isinstance(item, Command):
                _plan_command(plan, item, mine, journal, runner)
            else:
                plan.manual.append(item)
    return plan


def redacting(out: Printer, secrets: List[str]) -> Printer:
    return lambda text: out(edits.redact(text, secrets))


def render_plan(plan: Plan, adapters: Dict[str, Adapter], out: Printer) -> None:
    out = redacting(out, plan.secrets)
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
    profile = _registry.current_profile(argv)
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
    """Write the plan. Returns the number of failures; after the first failure nothing further is written. A
    registration that someone else made is skipped (not a failure) unless ``skipped_is_failure`` is set, as for
    ``mcp install``, whose whole job is that registration. Nothing that already exists is ever changed, and
    every file write goes through :func:`edits.write_checked`."""
    from briefcase.connect.lock import command_lock

    with command_lock():  # the journal is read, extended and saved under one lock (and files are checked as written)
        return _apply_plan_locked(plan, ctx, out, runner, skipped_is_failure, capture_warning)


def _apply_plan_locked(plan: Plan, ctx: Any, out: Printer, runner: Optional[Callable[[List[str]], Tuple[int, str]]],
                       skipped_is_failure: bool, capture_warning: bool) -> int:
    runner = runner or run_command
    out = redacting(out, plan.secrets)
    journal = edits.load_journal()
    failures = 0
    warned = False
    for resolved in plan.changes:
        edit = resolved.edit
        mine = ctx[edit.tool] if isinstance(ctx, dict) else ctx
        if capture_warning and mine.capture_content and edit.kind == "telemetry" and not warned:
            print(CAPTURE_WARNING, file=sys.stderr)
            warned = True
        if edit.layers and edit.kind == "telemetry":  # layers or environment may have changed since the preview
            late = []
            for layer in edit.layers:
                late += edits.layer_risks(edit, layer, getattr(mine, "given", {}) or {})
            late += edits.environment_risks(edit, os.environ)
            if late:
                failures += 1
                out(f"  failed {edit.path}: not written, because it now sets {', '.join(late)} (it did not when "
                    "this was previewed), and that would combine with the endpoint being written")
                break
        try:
            backup = edits.apply(resolved, journal)
        except (edits.EditRefused, OSError) as exc:
            failures += 1
            out(f"  failed {edit.path}: {exc}")
            break
        out(f"  wrote  {resolved.target}" + (f" (backup: {backup})" if backup else ""))
        ops = edits.written_ops(resolved.applied)
        journal["targets"][target_key(edit.tool, edit.kind, str(resolved.target))] = _record(
            edit.tool, edit.kind, str(resolved.target), mine, ops=ops, tables=list(edit.toml_tables),
            hash=edits.content_hash(edit.format, ops, resolved.new_text, edit.toml_tables))
        edits.save_journal(journal)  # right away: an interruption must not leave a live hook that no record names
    for command in plan.commands if not failures else []:
        # Look again immediately before the add: a same-named entry that appeared since the plan is never replaced.
        state, _ = _registry.lookup(command.argv, runner)
        code, output = (0, "")
        replacing = command.argv[:1] and command.argv[0] in _registry.REPLACING
        if state == _registry.PRESENT or (state == _registry.UNKNOWN and replacing):
            code, output = 1, "already exists"
        else:
            # Record the intent before the add can have happened: an interruption after it succeeds must not leave a
            # live registration that no record names. A failed add removes the record again; undo checks the
            # registry before it says anything about a recorded registration.
            mine = ctx[command.tool] if isinstance(ctx, dict) else ctx
            pending = {"tool": command.tool, "argv": command.argv, "undo_hint": command.undo_hint,
                       **identity(command.argv), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            tkey = target_key(command.tool, "mcp", command_target(command.argv))
            journal["commands"].append(pending)
            journal["targets"][tkey] = _record(command.tool, "mcp", command_target(command.argv), mine,
                                               argv=command.argv, undo_hint=command.undo_hint,
                                               **identity(command.argv))
            edits.save_journal(journal)
            code, output = runner(command.argv)
            if code != 0:
                # A timeout or a crash can follow the add that committed: look before forgetting. Only an entry that is
                # provably absent (or provably someone else's) lets the record go; ours, or unknown, keeps it, and
                # undo checks the registry again before it says anything.
                after = _registry.DIFFERS if "already exists" in output else _registry.owned(command.argv, runner)
                if after in (_registry.ABSENT, _registry.DIFFERS):
                    journal["commands"].remove(pending)
                    del journal["targets"][tkey]
                    edits.save_journal(journal)
        if code != 0 and "already exists" in output:
            # Someone else's entry: never touched, never printed (its args and env may hold credentials).
            scope, key = registration(command.argv)
            name = _registry.server_name(command.argv)
            failures += 1 if skipped_is_failure else 0
            hint = command.undo_hint or ["remove it in the tool's settings"]
            if state == _registry.UNKNOWN:  # never suggest removing what cannot be shown to be someone else's either
                out(f"  {'failed' if skipped_is_failure else 'skip  '} the tool's registry could not be read, so it "
                    f"is not known whether an MCP server named {name!r} is already registered in {scope} scope; "
                    "nothing was run and nothing is suggested for removal. Check it yourself, then connect again")
                continue
            out(f"  {'failed' if skipped_is_failure else 'skip  '} an MCP server named {name!r} is already "
                f"registered in {scope} scope and briefcase did not write it, so it was left as it is; to use "
                f"briefcase-ai-mcp, run `{mcp_payloads.shell(hint)}` and then "
                f"`{mcp_payloads.shell(command.argv)}`")
            continue
        if code != 0:
            failures += 1
            out(f"  failed {mcp_payloads.shell(command.argv)}: {output or f'exit {code}'}")
            break
        out(f"  ran    {mcp_payloads.shell(command.argv)}")
    edits.save_journal(journal)
    return failures


def ops_signature(entry: Dict[str, Any], owners: List[str]) -> str:
    """A fingerprint of the recorded operations of ``owners`` in one journalled file, to bind a confirmation to what was
    shown: a hook added to the same file while the question was open changes it."""
    ops = [{"op": o.get("op"), "path": o.get("path"), "value": o.get("value")} for o in entry["ops"]
           if o.get("tool") in owners]
    return hashlib.sha256(json.dumps(ops, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def undo(tools: Optional[List[str]], out: Printer, dry_run: bool = False, keep_unremoved: bool = False,
         runner: Optional[Callable[[List[str]], Tuple[int, str]]] = None,
         retained: Optional[List[str]] = None, removed: Optional[Dict[str, int]] = None,
         confirmed: Optional[Dict[str, str]] = None) -> int:
    """Remove what connect wrote for ``tools`` (everything when ``None``), but only where the file still holds
    exactly what was written; what was changed since is left as it is and reported. A registration is checked
    against the tool's registry first, and the command to remove it is printed qualified with the profile (and
    directory) it was recorded for. With ``keep_unremoved`` anything that could not be removed keeps its record.
    ``confirmed`` maps the journalled files the person was shown to :func:`ops_signature`; with it, only those
    files are touched, and only while they still hold exactly what was shown.

    Returns how many journalled items were visited."""
    from briefcase.connect.lock import command_lock

    with command_lock(not dry_run):
        return _undo_locked(tools, out, dry_run, keep_unremoved, runner, retained, removed, confirmed)


def _undo_locked(tools: Optional[List[str]], out: Printer, dry_run: bool, keep_unremoved: bool,
                 runner: Optional[Callable[[List[str]], Tuple[int, str]]], retained: Optional[List[str]],
                 removed: Optional[Dict[str, int]], confirmed: Optional[Dict[str, str]] = None) -> int:
    runner = runner or run_command
    journal = edits.load_journal()
    visited = 0
    for path in list(journal["files"]):
        entry = journal["files"][path]
        owners = {op.get("tool") for op in entry["ops"]}
        if confirmed is not None:
            if path not in confirmed:
                continue  # not a file the person was shown
            if ops_signature(entry, list(tools or [])) != confirmed[path]:
                out(f"  {path} changed while the question was open, so it was left as it is; run the command again")
                if retained is not None:
                    retained.extend(t for t in (tools or []) if t in owners)
                continue
        for tool in (sorted(t for t in owners if t) if tools is None else [t for t in tools if t in owners]):
            visited += 1
            stats: Dict[str, int] = {}
            messages, done = edits.undo_entry(path, entry, tool, dry_run=dry_run, keep_unremoved=keep_unremoved,
                                              stats=stats)
            if removed is not None:
                removed[tool] = removed.get(tool, 0) + stats.get("removed", 0)
            for message in messages:
                out(("  would: " if dry_run else "  ") + message)
            if not done and retained is not None:
                retained.append(tool)  # what could not be removed (also in a dry run: the status must match)
            if done and not dry_run:
                del journal["files"][path]
                break
    keep = []
    for command in journal["commands"]:
        if not (tools is None or command["tool"] in tools):
            keep.append(command)
            continue
        visited += 1
        argv, profile, cwd = command["argv"], command.get("profile"), command.get("cwd")
        hint = command.get("undo_hint")
        if (profile or None) != (_registry.current_profile(argv) or None) or (cwd and cwd != os.getcwd()):
            where = f"cd {mcp_payloads.shell([cwd])} && " if cwd else ""
            var = _registry.profile_var(argv)
            unset = f"env -u {var} " if not profile and _registry.current_profile(argv) else ""
            out(f"  registered in profile {profile or 'default'}; to undo it, run: {where}"
                f"{unset}{_registry.profile_prefix(argv, profile)}briefcase-ai connect --undo {command['tool']}")
            keep.append(command)
            continue
        state = _registry.owned(argv, runner)
        if state == _registry.OURS:
            out("  to remove the MCP registration, run: "
                + (_registry.qualified(hint, profile, cwd) if hint else mcp_payloads.shell(argv)
                   + " (remove the `briefcase` MCP server in the tool's settings)"))
        elif state == _registry.UNKNOWN:
            out("  not undone: the tool's registry could not be read, so it is not known whether the registration "
                "is still briefcase's; nothing to run; try again")
            keep.append(command)
        elif state == _registry.DIFFERS:
            out("  the MCP registration was changed since connect, so it is no longer briefcase's: nothing to run")
        else:
            out("  the MCP registration is already gone")
    if not dry_run:
        journal["commands"] = keep
        selected = [k for k, v in journal["targets"].items() if tools is None or v["tool"] in tools]
        for key in selected:
            rec = journal["targets"][key]
            if rec["kind"] == "mcp" and "argv" in rec:
                still = any(c["tool"] == rec["tool"] and command_target(c["argv"], identity_of(c)) == rec["target"]
                            for c in journal["commands"])
            else:
                entry = journal["files"].get(rec["target"])
                still = bool(entry and any(op.get("tool") == rec["tool"] for op in entry["ops"]))
            if not still:
                del journal["targets"][key]
        edits.save_journal(journal)
    return visited


def identity_of(command: Dict[str, Any]) -> Dict[str, Any]:
    return {"profile": command.get("profile"), "cwd": command.get("cwd")}


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
        for c in commands:  # a deleted or replaced registration is not "connected" because the journal remembers it
            if (c.get("profile") or None) == (_registry.current_profile(c["argv"]) or None):
                checks.append((_registry.owned(c["argv"], run_command) not in (_registry.ABSENT, _registry.DIFFERS),
                               f"{c['argv'][0]} mcp registration: {_registry.server_name(c['argv'])}"))
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
