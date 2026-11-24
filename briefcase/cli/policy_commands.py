"""``briefcase-ai policy``: allow, ask or deny what coding tools may run.

    briefcase-ai policy init                # write ~/.briefcase/policy.toml and install the hooks
    briefcase-ai policy check               # validate, list every rule, show what differs from the preset
    briefcase-ai policy test -- rm -rf /    # dry-run one decision and explain it
    briefcase-ai policy log                 # recent decisions (works without the receiver)
    briefcase-ai policy off                 # remove the hooks; the policy file stays
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

GROUP = "enforce"
ORDER = 10
FEATURE_ID = "policy-hooks"
PRESETS = ("observe", "guarded", "strict")
_EFFECT_LABEL = {"allow": "ALLOW", "ask": "ASK", "deny": "DENY"}


ENV_HELP = """\
Environment:
  BRIEFCASE_HOME               where policy.toml and the decision log live (default ~/.briefcase)
  BRIEFCASE_SYSTEM_POLICY      path of the administrator's policy (default per OS, see docs)
  BRIEFCASE_RECEIVER_ENDPOINT  receiver the hook reports decisions to (default http://127.0.0.1:4318)
  BRIEFCASE_RECEIVER_TOKEN     bearer token for that receiver
  BRIEFCASE_POLICY_REPORT=0    do not report decisions to a receiver (the local log is still written)

Docs: docs/features/policy-hooks.md"""


def _sub(psub, name: str, help_text: str, purpose: str, example: str):
    import argparse

    return psub.add_parser(name, help=help_text, description=f"{purpose}\n\nExample:\n  {example}",
                           formatter_class=argparse.RawDescriptionHelpFormatter)


def register(sub) -> None:
    import argparse

    p = sub.add_parser(
        "policy",
        help="Allow, ask or deny what Claude Code, Cursor and Codex may run.",
        description="Policy for coding tools: a hook in Claude Code, Cursor and Codex CLI checks each shell "
                    "command,\nfile access, MCP tool and web request against ~/.briefcase/policy.toml before "
                    "it runs.\n\nExample:\n  briefcase-ai policy init --preset guarded",
        epilog=ENV_HELP, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    psub = p.add_subparsers(dest="sub")

    init = _sub(psub, "init", "Write the policy file and install the enforcement hooks.",
                "Writes ~/.briefcase/policy.toml, then adds a hook to each coding tool (diff, one confirmation,\n"
                "undo with `briefcase-ai policy off`).", "briefcase-ai policy init --preset guarded")
    init.add_argument("tools", nargs="*", metavar="TOOL",
                      help="claude-code, cursor, codex. Default: the ones detected on this machine.")
    init.add_argument("--preset", choices=PRESETS, default=None,
                      help="Built-in rule set for a new policy file (default guarded).")
    init.add_argument("--scope", choices=("user", "project"), default="user",
                      help="user: ~/.briefcase/policy.toml and your tool settings (default). project: "
                           ".briefcase/policy.toml and the project's tool settings, which can only add restrictions.")
    init.add_argument("--fail-closed", action="store_true",
                      help="Block tool calls when the policy or the hook is broken (default: allow and report).")
    init.add_argument("--accept-codex-startup-risk", action="store_true",
                      help="With --fail-closed and Codex: accept that a hook executable that is missing when Codex "
                           "starts cannot be blocked by briefcase-ai (Codex then runs the tool).")
    init.add_argument("--portable", action="store_true",
                      help="Install `briefcase-ai hook <tool> --enforce` instead of an absolute path (slower start).")
    init.add_argument("--capture-content", action="store_true",
                      help="Also log the command, path or URL of each decision. Default: a keyed hash only.")
    init.add_argument("--endpoint", default=None, help="Receiver URL for decision records (default 127.0.0.1:4318).")
    init.add_argument("--force", action="store_true",
                      help="Replace an existing policy file (a backup is kept) and ignore tool version checks.")
    init.add_argument("--dry-run", dest="policy_dry_run", action="store_true", help="Show the plan; write nothing.")
    init.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    init.set_defaults(func=cmd_init)

    check = _sub(psub, "check", "Validate the policy and list its rules.",
                 "Validates every policy file that applies here and lists the rules in the order they are\n"
                 "checked. Exit 1 when a file is invalid.", "briefcase-ai policy check")
    check.add_argument("--json", action="store_true", help="Machine-readable output.")
    check.set_defaults(func=cmd_check)

    test = _sub(psub, "test", "Dry-run one decision and explain which rule matched.",
                "Shows what the policy would do with a command, a file, an MCP tool or a URL, and why.\n"
                "Nothing is run.", "briefcase-ai policy test -- git push --force origin main")
    test.add_argument("--file", metavar="PATH", help="A file read (add --write for a write).")
    test.add_argument("--write", action="store_true", help="With --file: test a write instead of a read.")
    test.add_argument("--mcp", metavar="SERVER.TOOL", help="An MCP tool call.")
    test.add_argument("--url", metavar="URL", help="A web request.")
    test.add_argument("--cwd", metavar="DIR", default=None, help="Directory the call would run in (default: here).")
    test.add_argument("--preset", choices=PRESETS, default=None,
                      help="Test a built-in preset instead of your policy files.")
    test.add_argument("--expect", choices=("allow", "ask", "deny"), default=None,
                      help="Exit 1 unless this is the result (for CI).")
    test.add_argument("--json", action="store_true", help="Machine-readable output.")
    test.add_argument("words", nargs="*", metavar="COMMAND", help="A shell command, after `--`.")
    test.set_defaults(func=cmd_test)

    log = _sub(psub, "log", "Show recent policy decisions.",
               "Reads ~/.briefcase/policy/decisions.jsonl, which the hook appends to on every decision.",
               "briefcase-ai policy log --effect deny")
    log.add_argument("-n", type=int, default=20, dest="count", help="How many (default 20).")
    log.add_argument("--effect", choices=("allow", "ask", "deny"), default=None, help="Only this effect.")
    log.add_argument("--json", action="store_true", help="One JSON object per line.")
    log.set_defaults(func=cmd_log)

    off = _sub(psub, "off", "Remove the enforcement hooks; keep the policy file.",
               "Removes exactly the hook entries `briefcase-ai policy init` added.", "briefcase-ai policy off")
    off.add_argument("tools", nargs="*", metavar="TOOL", help="Only these tools (default: all).")
    off.add_argument("--dry-run", dest="policy_dry_run", action="store_true", help="Show what would be removed.")
    off.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    off.set_defaults(func=cmd_off)

    status_p = _sub(psub, "status", "Show whether enforcement is on and healthy.",
                    "Policy files, installed hooks and recent hook problems.", "briefcase-ai policy status")
    status_p.add_argument("--json", action="store_true", help="Machine-readable output.")
    status_p.set_defaults(func=cmd_status)
    p.set_defaults(func=_usage(p))


# --- shared helpers -----------------------------------------------------------

def _usage(parser):
    def show(args, store=None, engine=None, stack=None) -> int:
        parser.print_help(sys.stderr)
        return 2
    return show


def _dry_run(args) -> bool:
    return bool(getattr(args, "policy_dry_run", False) or getattr(args, "dry_run", False))


def _error(text: str) -> None:
    print(f"error: {text}", file=sys.stderr)


def _tilde(path: Any) -> str:
    text = str(path)
    home = os.path.expanduser("~")
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def _resolve_tools(names: List[str]) -> List[str]:
    from briefcase.connect.adapters import ALIASES
    from briefcase.policy import install

    out = []
    for raw in names:
        name = ALIASES.get(raw.lower(), raw.lower())
        if name.startswith(install.PREFIX):
            name = name[len(install.PREFIX):]
        if name not in install.TOOLS:
            raise KeyError(raw)
        if name not in out:
            out.append(name)
    return out


def _recent_problem(max_age: float = 24 * 3600) -> Optional[Dict[str, Any]]:
    from briefcase.policy import hook

    last = hook.read_health().get("last_error")
    if isinstance(last, dict) and time.time() - float(last.get("at", 0) or 0) < max_age:
        return last
    return None


def _ago(timestamp: float) -> str:
    seconds = max(0, int(time.time() - timestamp))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "just now"


# --- status -------------------------------------------------------------------

def overview(project_dir: Optional[str] = None) -> Dict[str, Any]:
    """Everything ``status`` and ``doctor`` report, as plain data."""
    from briefcase.policy import install, model

    loaded = model.load(project_dir or os.getcwd())
    hooks = install.installed()
    files = [{"source": source, "path": path, "valid": ok} for source, path, ok in loaded.paths]
    problems: List[str] = []
    for source, path, text in loaded.errors:
        problems.append(f"{path or source}: {text}")
    for row in hooks:
        missing = [what for ok, what in row["files"] if not ok]
        if missing:
            problems.append(f"{row['tool']}: hook entry no longer present ({'; '.join(missing)})")
        if not row["launcher_ok"]:
            problems.append(f"{row['tool']}: the hook command points at a file that no longer exists")
        if loaded.policy is not None and loaded.fail_closed and not row["fail_closed"]:
            problems.append(f"{row['tool']}: the policy says fail_closed but the installed hook does not; "
                            "run `briefcase-ai policy off` and then `briefcase-ai policy init --fail-closed`")
    last = _recent_problem()
    settings = dict(loaded.policy.settings) if loaded.policy is not None else None
    return {"files": files, "errors": [list(e) for e in loaded.errors], "hooks": hooks, "problems": problems,
            "last_hook_error": last, "settings": settings, "fail_closed": loaded.fail_closed,
            "has_policy": loaded.policy is not None}


def status() -> Dict[str, str]:
    """The row ``briefcase-ai status`` shows for this feature, titled "Policy"."""
    row = _status_row()
    row["title"] = "Policy"
    return row


def _status_row() -> Dict[str, str]:
    """The row ``briefcase-ai status`` shows for this feature. Read-only and quick: it reads the
    policy files, the connect journal and the hook's health file. ``state`` is ``on``,
    ``off`` or ``attention`` (installed, but broken or failing open)."""
    try:
        info = overview()
    except Exception as exc:  # status must never take `briefcase-ai status` down
        return {"id": FEATURE_ID, "state": "attention", "detail": f"could not read policy state: {exc}",
                "next": "briefcase-ai policy check"}
    tools = ", ".join(row["tool"] for row in info["hooks"])
    if not info["hooks"]:
        detail = "policy file present, hooks not installed" if info["files"] else "not set up"
        return {"id": FEATURE_ID, "state": "off", "detail": detail, "next": "briefcase-ai policy init"}
    if info["errors"]:
        blocked = "tool calls are BLOCKED" if info["fail_closed"] else "enforcement is OFF (failing open)"
        return {"id": FEATURE_ID, "state": "attention",
                "detail": f"policy file invalid, {blocked}: {info['errors'][0][2]}", "next": "briefcase-ai policy check"}  # noqa: E501
    if not info["has_policy"]:
        return {"id": FEATURE_ID, "state": "attention", "detail": f"hooks installed for {tools} but there is no "
                "policy file, so nothing is enforced", "next": "briefcase-ai policy init"}
    if info["problems"]:
        return {"id": FEATURE_ID, "state": "attention", "detail": info["problems"][0], "next": "briefcase-ai policy init"}  # noqa: E501
    settings = info["settings"]
    detail = f"{settings['preset']} preset, mode {settings['mode']}, hooks: {tools}"
    if info["last_hook_error"]:
        last = info["last_hook_error"]
        return {"id": FEATURE_ID, "state": "attention",
                "detail": f"{detail}; last hook problem {_ago(float(last['at']))}: {last.get('message', '')}",
                "next": "briefcase-ai policy check"}
    return {"id": FEATURE_ID, "state": "on", "detail": detail, "next": "briefcase-ai policy log"}


def doctor_checks() -> List[Any]:
    """``(severity, label, detail)`` rows for ``briefcase-ai doctor --connect``."""
    info = overview()
    rows: List[Any] = []
    if not info["hooks"] and not info["files"]:
        return [("ok", "policy", "not set up (briefcase-ai policy init)")]
    for entry in info["files"]:
        rows.append(("ok" if entry["valid"] else "fail", f"policy {entry['source']}",
                     _tilde(entry["path"]) + ("" if entry["valid"] else ": invalid, run `briefcase-ai policy check`")))
    if not info["hooks"]:
        rows.append(("warn", "policy hooks", "no hooks installed, so nothing is enforced; run: briefcase-ai policy init"))  # noqa: E501
    for row in info["hooks"]:
        bad = [what for ok, what in row["files"] if not ok]
        if bad or not row["launcher_ok"]:
            rows.append(("fail", f"policy hook {row['tool']}", "; ".join(bad) or "hook command target is missing"))
        else:
            rows.append(("ok", f"policy hook {row['tool']}",
                         f"{len(row['files'])} entry(ies) present" + (", fail-closed" if row["fail_closed"] else "")))
    for problem in info["problems"]:
        if "fail_closed" in problem:
            rows.append(("warn", "policy fail_closed", problem))
    if info["last_hook_error"]:
        last = info["last_hook_error"]
        rows.append(("warn", "policy hook errors", f"{_ago(float(last['at']))}: {last.get('message', '')}"))
    return rows


def cmd_status(args, store=None, engine=None, stack=None) -> int:
    info = overview()
    row = status()
    if getattr(args, "json", False):
        hooks = [{"tool": h["tool"], "command": h["command"], "fail_closed": h["fail_closed"],
                  "launcher_ok": h["launcher_ok"], "entries": [{"ok": ok, "what": what} for ok, what in h["files"]]}
                 for h in info["hooks"]]
        print(json.dumps({"status": row, "files": info["files"], "hooks": hooks, "problems": info["problems"],
                          "last_hook_error": info["last_hook_error"], "settings": info["settings"]},
                         indent=2, default=str))
        return 0
    print(f"policy: {row['state'].upper()}  {row['detail']}")
    for entry in info["files"]:
        print(f"  {'ok     ' if entry['valid'] else 'INVALID'} {entry['source']:<8} {_tilde(entry['path'])}")
    for hook_row in info["hooks"]:
        ok = all(flag for flag, _ in hook_row["files"]) and hook_row["launcher_ok"]
        print(f"  {'ok     ' if ok else 'BROKEN '} hook     {hook_row['tool']}"
              + (" (fail-closed)" if hook_row["fail_closed"] else ""))
    for problem in info["problems"]:
        print(f"  problem: {problem}")
    if info["last_hook_error"]:
        last = info["last_hook_error"]
        print(f"  last hook problem {_ago(float(last['at']))}: {last.get('message', '')}")
    print(f"Next: {row['next']}")
    return 0 if row["state"] in ("on", "off") else 1


# --- init / off ---------------------------------------------------------------

def _confirm(question: str, assume_yes: bool, ask: Optional[Callable[[str], str]] = None) -> Optional[bool]:
    from briefcase.cli.connect_commands import _confirm as confirm
    return confirm(question, assume_yes, ask=ask)


def _read_or_none(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _policy_file_is_ours(path: Path, text: Optional[str]) -> bool:
    """Is the policy file at ``path`` (whose text at preview time was ``text``) exactly what an earlier `policy init`
    wrote (by hash)?"""
    from briefcase.connect import edits

    try:
        journal = edits.load_journal()
    except edits.LegacyJournal:
        return False
    rec = journal["targets"].get(f"policy|policy-file|{path.resolve()}")
    return bool(rec and text is not None and rec.get("hash") == _sha(text))


def _record_policy_file(path: Path, text: str) -> None:
    from briefcase.connect.lock import command_lock

    with command_lock():
        _record_locked(path, text)


def _record_locked(path: Path, text: str) -> None:
    from briefcase.connect import edits

    journal = edits.load_journal()
    journal["targets"][f"policy|policy-file|{path.resolve()}"] = {
        "tool": "policy", "kind": "policy-file", "scope": "user", "target": str(path.resolve()),
        "hash": _sha(text), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    edits.save_journal(journal)


def run_init(args, out: Callable[[str], None] = print, ask: Optional[Callable[[str], str]] = None) -> int:
    return _run_init(args, out, ask)


def _run_init(args, out: Callable[[str], None] = print, ask: Optional[Callable[[str], str]] = None) -> int:
    from briefcase.connect import edits
    from briefcase.connect import engine as connect_engine
    from briefcase.connect.adapters import DEFAULT_ENDPOINT, Context
    from briefcase.policy import install, model, template

    try:
        named = _resolve_tools(list(getattr(args, "tools", []) or []))
    except KeyError as exc:
        _error(f"unknown tool {exc.args[0]!r}; choose from: {', '.join(install.TOOLS)}")
        return 2
    scope = getattr(args, "scope", "user")
    project = scope == "project"
    policy_path = Path.cwd() / ".briefcase" / "policy.toml" if project else Path(model.user_policy_path())
    source = "project" if project else "user"
    dry = _dry_run(args)
    force = bool(getattr(args, "force", False))
    preset = getattr(args, "preset", None)

    # 1. The policy file: keep a valid one, never overwrite silently.
    write_policy = None
    fail_closed = bool(getattr(args, "fail_closed", False))
    preview_text = _read_or_none(policy_path) if policy_path.exists() else None  # what the preview is based on
    if policy_path.exists() and force and not _policy_file_is_ours(policy_path, preview_text):
        _error(f"{_tilde(policy_path)} was not written by `briefcase-ai policy init` (or was edited since), so --force "
               "will not replace it: its restrictions would stop applying. Move it aside yourself, or edit it "
               "(`briefcase-ai policy check` validates it), then run `briefcase-ai policy init` again.")
        return 1
    if policy_path.exists() and not force:
        try:
            layer = model.load_layer(str(policy_path), source)
        except model.PolicyError as exc:
            _error(f"{_tilde(policy_path)} is not a valid policy: {exc}\n"
                   "Fix it (briefcase-ai policy check), or replace it with: briefcase-ai policy init --force")
            return 1
        kept = layer.settings.get("preset", "guarded") if layer else "guarded"
        out(f"Policy: {_tilde(policy_path)} (kept as it is; preset {kept})")
        if preset and preset != kept:
            out(f"  note: --preset {preset} was not applied to the existing file. Edit `preset = \"{preset}\"` "
                "in it, or pass --force to replace it (a backup is kept).")
        if layer is not None and layer.settings.get("fail_closed"):
            fail_closed = True
        elif fail_closed:
            out("  note: --fail-closed sets the hooks to block on failure. Also set `fail_closed = true` in the "
                "policy file so a broken policy blocks too.")
    else:
        write_policy = template.render(preset or "guarded", fail_closed=fail_closed)
        verb = "replace" if policy_path.exists() else "create"
        out(f"Policy: {verb} {_tilde(policy_path)} (preset {preset or 'guarded'}"
            + (", fail_closed" if fail_closed else "") + ")")
        if project:
            out("  A project policy can only add restrictions to your user policy.")

    # 2. The hooks, through the connect edit engine.
    options = install.HookOptions(
        fail_closed=fail_closed, capture_content=bool(getattr(args, "capture_content", False)),
        endpoint=(getattr(args, "endpoint", None) or DEFAULT_ENDPOINT).rstrip("/"),
        portable=bool(getattr(args, "portable", False)) or project,
        accept_codex_startup_risk=bool(getattr(args, "accept_codex_startup_risk", False)),
    )
    adapters = install.adapters(options)
    if named:
        tools = [install.PREFIX + name for name in named]
    else:
        tools = install.detected(adapters)
        if tools:
            out("Detected: " + ", ".join(adapters[name].data["name"] for name in tools))
        else:
            out("No supported coding tool was detected (Claude Code, Cursor, Codex CLI).")
            out("The policy file is still useful with `briefcase-ai policy test`. To install a hook anyway, "
                "name the tool: briefcase-ai policy init claude-code")
    if options.capture_content:
        print("warning: --capture-content writes the command, path or URL of every decision to "
              "~/.briefcase/policy/decisions.jsonl and sends it to the receiver.", file=sys.stderr)
    ctx = Context(scope=scope, endpoint=options.endpoint, mcp=False, telemetry=False,
                  capture_content=options.capture_content, force=force, project_dir=Path.cwd(), strict=True)
    try:
        plan = connect_engine.build_plan(tools, ctx, adapters)  # type: ignore[arg-type]
    except edits.LegacyJournal as exc:
        _error(str(exc))
        return 1
    connect_engine.render_plan(plan, adapters, out)  # type: ignore[arg-type]
    refused = [r for r in plan.resolved if r.status == edits.REFUSED]
    if plan.blocked or refused:
        out("\nNothing was changed, and enforcement is NOT in place for the tool(s) above: " + (
            "a hook is installed with other settings." if plan.differs else
            "a hook file cannot be edited safely (add the snippet shown by hand)." if refused and not plan.blocked
            else "a hook entry is missing, edited, or not ours (see above)."))
        return 1

    if dry:
        out(f"\nDry run: {1 if write_policy else 0} policy file and {len(plan.changes)} tool file(s) would be "
            "written. Nothing was changed.")
        return 0
    if tools and not plan.has_work and not [r for r in plan.resolved if r.status == edits.NOOP]:
        out("\nNo hook was installed (see the manual steps above): enforcement is NOT in place.")
        return 1
    if write_policy is None and not plan.has_work:
        out("\nNothing to write: the policy file exists and the hooks are already installed."
            if tools else "\nNothing was installed.")
        out("Next: briefcase-ai policy test -- rm -rf /")
        return 0
    out("")
    question = (f"Write {'the policy file and ' if write_policy else ''}{len(plan.changes)} tool file(s)?"
                if plan.changes else "Write the policy file?")
    answer = _confirm(question, bool(getattr(args, "yes", False)), ask=ask)
    if answer is None:
        _error("not a terminal, so cannot ask for confirmation; re-run with --yes (or --dry-run to only look).")
        return 1
    if not answer:
        out("Aborted; nothing was changed.")
        return 1
    if write_policy is not None:
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        if policy_path.exists():
            backup = edits.backup_path(policy_path)
            shutil.copy2(str(policy_path), str(backup))
            out(f"  backup {_tilde(backup)}")
        try:
            edits.write_checked(policy_path, write_policy, preview_text,
                                0o600 if not project else 0o644)
        except edits.EditRefused as exc:
            _error(str(exc))
            return 1
        _record_policy_file(policy_path, write_policy)
        out(f"  wrote  {_tilde(policy_path)}")
    failures = connect_engine.apply_plan(plan, ctx, out, capture_warning=False) if plan.has_work else 0
    if failures:
        out(f"\n{failures} step(s) failed; the rest were applied. Run `briefcase-ai policy status`.")
        return 1
    out("\nUndo the hooks any time with: briefcase-ai policy off")
    if install.PREFIX + "codex" in tools:
        out("Codex CLI: open /hooks once and trust the new hook; Codex skips it until you do.")
    if install.PREFIX + "cursor" in tools:
        out("Cursor reloads hooks.json on save; restart it if the hook does not show under Customize > Hooks.")
    out("Next: briefcase-ai policy test -- rm -rf /")
    return 0


def cmd_init(args, store=None, engine=None, stack=None) -> int:
    return run_init(args)


def cmd_off(args, store=None, engine=None, stack=None) -> int:
    return _cmd_off(args)


def _cmd_off(args) -> int:
    from briefcase.connect import engine as connect_engine
    from briefcase.policy import install, model

    try:
        named = _resolve_tools(list(args.tools or []))
    except KeyError as exc:
        _error(f"unknown tool {exc.args[0]!r}; choose from: {', '.join(install.TOOLS)}")
        return 2
    owners = [install.PREFIX + name for name in (named or install.TOOLS)]
    dry = _dry_run(args)
    present = [row["tool"] for row in install.installed() if install.PREFIX + row["tool"] in owners]
    unrecorded = [n[len(install.PREFIX):] for n in owners if n[len(install.PREFIX):] not in present
                  and install.hook_active(n[len(install.PREFIX):])]
    if unrecorded and not present:
        print(f"Enforcement is still ON for {', '.join(unrecorded)}: a hook exists that `policy init` has no record "
              "of (it was edited, copied, or its record was lost), so it is not removed. Remove the entry yourself "
              "(it runs `... hook <tool> --enforce`).", file=sys.stderr)
        return 1
    if not present:
        print("No policy hooks are installed" + (" for those tools." if named else "."))
        print("Next: briefcase-ai policy init")
        return 0
    if not dry:
        answer = _confirm(f"Remove the policy hooks for {', '.join(present)}?", bool(args.yes))
        if answer is None:
            _error("not a terminal, so cannot ask for confirmation; re-run with --yes.")
            return 1
        if not answer:
            print("Aborted; nothing was changed.")
            return 1
    print("Would remove:" if dry else "Removing:")
    kept: List[str] = []
    removed: Dict[str, int] = {}
    # Only the entries the person was shown and confirmed: a hook installed for another tool while the question was
    # open is not theirs to have removed. The journal is re-read under the lock and each entry must still hold
    # exactly what was written before it is touched.
    owners = [install.PREFIX + name for name in present]
    connect_engine.undo(owners, print, dry_run=dry, keep_unremoved=True, retained=kept, removed=removed)
    if dry:
        # What would be left: hook entries on disk now, minus the ones this run would remove.
        kept = sorted({n[len(install.PREFIX):] for n in owners
                       if install.hook_count(n[len(install.PREFIX):]) > removed.get(n, 0)}
                      | {n[len(install.PREFIX):] for n in kept})
        if kept:
            print("Enforcement would still be ON: a hook entry was changed after install and would not be removed.",
                  file=sys.stderr)
        return 1 if kept else 0
    still = sorted({row["tool"] for row in install.installed() if install.PREFIX + row["tool"] in owners}
                   | {name for name in (n[len(install.PREFIX):] for n in owners) if install.hook_active(name)})
    if still:
        print(f"Enforcement is still ON for {', '.join(still)}: the hook entry was changed after it was installed, "
              "so it was not removed and its record was kept. Remove the entry yourself (it runs "
              "`... hook <tool> --enforce`), then run `briefcase-ai policy off` again.", file=sys.stderr)
        return 1
    print(f"Enforcement is off. The policy file is kept: {_tilde(model.user_policy_path())}")
    print("Next: briefcase-ai policy init   (to turn it back on)")
    return 0


# --- check --------------------------------------------------------------------

def _changes_vs_preset(policy, base_layer) -> List[str]:
    from briefcase.policy import model

    lines = []
    defaults = model._defaults(policy.settings["preset"])
    for key in ("mode", "fail_closed", "on_unparseable", "default", "ask_fallback", "protect_self",
                "protected_branches"):
        if policy.settings[key] != defaults[key]:
            lines.append(f"setting  {key} = {json.dumps(policy.settings[key])}   (preset default: "
                         f"{json.dumps(defaults[key])})")
    for rule_id in policy.disabled:
        lines.append(f"disabled {rule_id}")
    for index, group in enumerate(policy.groups):
        for rule in group:
            if rule.source not in ("preset", "built-in"):
                where = "added   " if index == 0 else "stricter"
                lines.append(f"{where} {rule.id} ({rule.effect} {'/'.join(rule.kinds)}; {rule.source} policy)")
    return lines


def cmd_check(args, store=None, engine=None, stack=None) -> int:
    from briefcase.policy import install, model

    loaded = model.load(os.getcwd())
    hooks = install.installed()
    policy = loaded.policy
    if getattr(args, "json", False):
        doc: Dict[str, Any] = {
            "valid": not loaded.errors and policy is not None,
            "files": [{"source": s, "path": p, "valid": ok} for s, p, ok in loaded.paths],
            "errors": [{"source": s, "path": p, "error": e} for s, p, e in loaded.errors],
            "hooks": [row["tool"] for row in hooks],
        }
        if policy is not None:
            doc["settings"] = policy.settings
            doc["notes"] = policy.notes
            doc["changes_vs_preset"] = _changes_vs_preset(policy, policy.layers[0])
            doc["rules"] = ([dict(r.to_dict(), group="protection") for r in policy.protect]
                            + [dict(r.to_dict(), group=index) for index, g in enumerate(policy.groups) for r in g])
        print(json.dumps(doc, indent=2, default=str))
        return 0 if doc["valid"] else 1

    print("Policy files")
    seen = {path for _, path, _ in loaded.paths}
    for source, path in model.layer_paths(os.getcwd()):
        if path not in seen:
            print(f"  -       {source:<8} {_tilde(path)} (none)")
    for source, path, ok in loaded.paths:
        print(f"  {'ok     ' if ok else 'INVALID'} {source:<8} {_tilde(path)}")
    for source, path, text in loaded.errors:
        print(f"\nerror in {_tilde(path) or source}:\n  {text}")
    if policy is None:
        if not loaded.paths:
            print("\nThere is no policy file yet.\nNext: briefcase-ai policy init")
            return 1
        closed = "Tool calls are BLOCKED until this is fixed (fail_closed)." if loaded.fail_closed else \
            "Until this is fixed NOTHING is enforced: the hook allows every call (fail open)."
        print(f"\nThe policy cannot be applied. {closed}")
        print("An invalid file is never applied in part.")
        print("Next: fix the line above, then run `briefcase-ai policy check` again "
              "(or `briefcase-ai policy init --force` to start over)")
        return 1
    settings = policy.settings
    print("\nSettings")
    print(f"  preset {settings['preset']}   mode {settings['mode']}"
          f"   fail_closed {str(settings['fail_closed']).lower()}   on_unparseable {settings['on_unparseable']}"
          f"   ask_fallback {settings['ask_fallback']}"
          f"   protect_self {str(settings['protect_self']).lower()}")
    if settings["mode"] == "observe":
        print("  mode is observe: decisions are logged and nothing is blocked.")
    changes = _changes_vs_preset(policy, policy.layers[0])
    print(f"\nChanged from the {settings['preset']} preset")
    for line in changes or ["nothing: this is the preset as shipped"]:
        print(f"  {line}")
    print("\nRules, in the order they are checked (first match in a group decides; the strictest group wins)")
    groups = [("built-in protection (always checked)", policy.protect)]
    base = policy.layers[0].source
    groups.append((f"{base} policy, then the {settings['preset']} preset", policy.groups[0]))
    for layer, group in zip(policy.layers[1:], policy.groups[1:]):
        groups.append((f"{layer.source} policy (can only restrict)", group))
    for title, rules in groups:
        print(f"  {title}")
        if not rules:
            print("    (no rules)")
        for rule in rules:
            what = rule.describe()
            if len(what) > 96:
                what = what[:93] + "..."
            print(f"    {rule.effect:<5} {rule.id:<26} {'/'.join(rule.kinds):<10} {what}")
    if policy.notes:
        print("\nIgnored")
        for note in policy.notes:
            print(f"  {note}")
    print("\nHooks")
    if not hooks:
        print("  none installed, so this policy is not enforced anywhere yet. Run: briefcase-ai policy init")
    for row in hooks:
        ok = all(flag for flag, _ in row["files"]) and row["launcher_ok"]
        extra = " (fail-closed)" if row["fail_closed"] else ""
        print(f"  {'ok    ' if ok else 'BROKEN'} {row['tool']}{extra}")
        if settings["fail_closed"] and not row["fail_closed"]:
            print("         the policy says fail_closed but this hook was installed without it; "
                  "run `briefcase-ai policy init` again")
    if loaded.errors:
        print("\nResult: INVALID (see the error above). The valid files are still enforced"
              + (", and tool calls are blocked (fail_closed)." if loaded.fail_closed else "."))
        return 1
    try:
        from briefcase.policy import hook
        os.remove(hook.health_path())     # a clean check clears old hook problems
    except OSError:
        pass
    print("\nResult: OK")
    print("Next: briefcase-ai policy test -- rm -rf /")
    return 0


# --- test ---------------------------------------------------------------------

def _policy_for_test(args, cwd: str):
    from briefcase.policy import model

    if getattr(args, "preset", None):
        layer = model.parse_layer(f'version = 1\npreset = "{args.preset}"\nmode = "enforce"\n', "user", "(preset)")
        return model.merge([layer]), f"the {args.preset} preset (not your policy files)", []
    loaded = model.load(cwd)
    if loaded.policy is not None:
        used = ", ".join(_tilde(path) for _, path, ok in loaded.paths if ok)
        return loaded.policy, used, loaded.errors
    if loaded.errors:
        return None, "", loaded.errors
    layer = model.parse_layer('version = 1\npreset = "guarded"\n', "user", "(preset)")
    return model.merge([layer]), "the guarded preset (you have no policy file yet; run `briefcase-ai policy init`)", []


def cmd_test(args, store=None, engine=None, stack=None) -> int:
    from briefcase.policy import engine as policy_engine
    from briefcase.policy import tools

    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    given = [bool(args.words), bool(args.file), bool(args.mcp), bool(args.url)]
    if sum(given) != 1:
        _error("give exactly one thing to test: `-- COMMAND`, --file PATH, --mcp SERVER.TOOL or --url URL\n"
               "example: briefcase-ai policy test -- git push --force origin main")
        return 2
    if args.words:
        # One argument is taken as a whole command line; several are re-quoted so
        # `briefcase-ai policy test -- rm -rf "my dir"` tests what the shell passed in.
        import shlex
        command = args.words[0] if len(args.words) == 1 else " ".join(shlex.quote(w) for w in args.words)
        call = policy_engine.ToolCall("shell", command=command, cwd=cwd, project_dir=cwd)
        subject = command
    elif args.file:
        kind = "write" if args.write else "read"
        call = policy_engine.ToolCall(kind, paths=[args.file], cwd=cwd, project_dir=cwd)
        subject = f"{kind} {args.file}"
    elif args.mcp:
        server, _, name = args.mcp.partition(".")
        call = policy_engine.ToolCall("mcp", server=server or None, name=name or None, cwd=cwd, project_dir=cwd)
        subject = f"MCP tool {args.mcp}"
    else:
        call = policy_engine.ToolCall("web", url=args.url, cwd=cwd, project_dir=cwd)
        subject = f"web request {args.url}"
    policy, used, errors = _policy_for_test(args, cwd)
    if policy is None:
        _error("the policy cannot be applied: " + "; ".join(f"{_tilde(p)}: {e}" for _, p, e in errors)
               + "\nFix it with: briefcase-ai policy check")
        return 1
    decision = policy_engine.evaluate(policy, call)
    per_tool = {}
    for tool in tools.TOOLS:
        effect, note = tools.effective(tool, call.kind, decision.effect, policy.settings["ask_fallback"])
        per_tool[tool] = {"effect": effect, "note": note}
    if args.json:
        doc = decision.to_dict()
        doc.update(subject=subject, policy=used, tools=per_tool)
        print(json.dumps(doc, indent=2, default=str))
    else:
        _print_test(decision, subject, used, per_tool, errors)
    if args.expect and decision.would != args.expect:
        if not args.json:
            print(f"\nexpected {args.expect}, got {decision.would}", file=sys.stderr)
        return 1
    return 0


def _print_test(decision, subject: str, used: str, per_tool: Dict[str, Any], errors: List[Any]) -> None:
    from briefcase.policy import tools

    label = _EFFECT_LABEL[decision.would]
    match = decision.match
    if match is None or match.effect == "allow" and match.rule_id is None:
        print(f"{label:<5}  no rule matched")
    else:
        origin = {"preset": "preset rule", "built-in": "built-in protection", "setting": "policy setting"}.get(
            match.source, f"{match.source} policy rule")
        print(f"{label:<5}  {origin} {match.rule_id}")
        if match.reason:
            print(f"       {match.reason}")
    if decision.mode == "observe" and decision.would != "allow":
        print("       mode is observe, so this is logged and NOT blocked.")
    print(f"\nTested: {subject}")
    print("\nHow it was read")
    by_unit: Dict[str, List[Any]] = {}
    for found in decision.matches:
        by_unit.setdefault(found.unit, []).append(found)
    order = list(decision.units) + [unit for unit in by_unit if unit not in decision.units]
    for unit in order:
        found = by_unit.get(unit)
        if not found:
            print(f"  {unit}\n      allow  (no rule matches)")
            continue
        print(f"  {unit}")
        for item in found:
            detail = f"  [{item.detail}]" if item.detail else ""
            print(f"      {item.effect:<5}  {item.rule_id}{detail}")
    if not order:
        print("  (nothing to judge)")
    print("\nWhat each tool would do")
    for tool in tools.TOOLS:
        row = per_tool[tool]
        effect = row["effect"] if row["effect"] is not None else "not enforced"
        note = f"   ({row['note']})" if row["note"] else ""
        print(f"  {tools.NAMES[tool]:<12} {effect}{note}")
    print(f"\nPolicy: {used}")
    for _, path, text in errors:
        print(f"warning: {_tilde(path)} is invalid and was left out: {text}")


# --- log ----------------------------------------------------------------------

def read_log(count: int, effect: Optional[str] = None) -> List[Dict[str, Any]]:
    from briefcase.policy import hook

    entries: List[Dict[str, Any]] = []
    for path in (hook.log_path() + ".1", hook.log_path()):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and (effect is None or entry.get("effect") == effect):
                        entries.append(entry)
        except OSError:
            continue
    return entries[-count:] if count > 0 else entries


def cmd_log(args, store=None, engine=None, stack=None) -> int:
    from briefcase.policy import hook

    entries = read_log(args.count, args.effect)
    if args.json:
        for entry in entries:
            print(json.dumps(entry, default=str))
        return 0
    if not entries:
        print(f"No decisions logged yet ({_tilde(hook.log_path())}).")
        print("Next: briefcase-ai policy status")
        return 0
    print(f"{'TIME':<20} {'TOOL':<12} {'KIND':<6} {'EFFECT':<6} {'RULE':<26} WHAT")
    for entry in entries:
        what = entry.get("target") or (("#" + str(entry["target_hmac_sha256"])[:12])
                                       if entry.get("target_hmac_sha256") else "")
        if entry.get("error"):
            what = "hook problem: " + str(entry["error"])
        effect = str(entry.get("effect", ""))
        if entry.get("mode") == "observe" and entry.get("would") not in (None, "allow"):
            effect = f"({entry['would']})"
        print(f"{str(entry.get('ts', ''))[:19]:<20} {str(entry.get('tool', '')):<12} {str(entry.get('kind', '')):<6} "
              f"{effect:<6} {str(entry.get('rule') or '-'):<26} {' '.join(str(what).split())[:80]}")
    if not any(entry.get("target") for entry in entries):
        print("\nWHAT is a keyed hash: commands and paths are not logged unless the hooks were installed "
              "with --capture-content.")
    print("Next: briefcase-ai policy test -- <command>   (to see why a command gets its result)")
    return 0
