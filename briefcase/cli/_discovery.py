"""Command auto-discovery for the ``briefcase`` CLI.

Adding a command needs no edit to ``__main__.py``. Drop a module named
``briefcase/cli/<name>_commands.py`` next to this file; the CLI imports every such module
(sorted by name) and calls its ``register``. The module protocol:

``register(subparsers)`` (required)
    Add one or more commands to the top-level subparsers object and bind each to a handler
    with ``parser.set_defaults(func=handler)``. Handlers are called as
    ``handler(args, store, engine, stack)`` and return the exit code (0 ok, 1 failed,
    2 usage). ``store`` and ``engine`` are lazy: nothing is created until a handler touches
    them, so a command that ignores them never creates ``~/.briefcase`` or opens a channel.
    Keep ``register`` cheap: argparse wiring only, heavy imports inside the handler.

``GROUP = "connect"`` (optional, default ``"other"``)
    Where the module's commands are listed in ``briefcase --help``. One of ``"start"``,
    ``"connect"``, ``"capture"``, ``"enforce"``, ``"verify"``, ``"review"``, ``"evaluate"``,
    ``"other"``. An unknown value is listed under "Other".

``ORDER = 50`` (optional, default 100)
    Position inside the group in ``briefcase --help``: lower numbers are listed first, ties
    keep module-name order.

``status() -> dict`` (optional)
    Lets the command appear on ``briefcase status``. Return one dict (or a list of them)::

        {"id": "policy",                  # stable, unique, kebab-case
         "state": "on",                   # "on" | "off" | "attention"
         "detail": "3 rules, deny by default",
         "next": "briefcase policy init"} # command to suggest, or None

    Optional keys: ``"title"`` (label shown instead of the id) and ``"data"`` (extra JSON
    for ``briefcase status --json``). ``"attention"`` means on but needing action; its
    ``next`` is offered before any ``"off"`` item's. The hook runs on every
    ``briefcase status``, so it must be read-only, quick (no network beyond localhost, no
    subprocess that can hang) and must not raise for a fresh machine: return
    ``state: "off"`` instead. A hook that raises or returns something else is left out
    (reported under ``--debug``). Return ``None`` to show nothing.

A module that fails to import or register is skipped: the rest of the CLI keeps working and
``briefcase --debug ...`` prints one line naming the module and the error.
"""
from __future__ import annotations

import argparse
import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SUFFIX = "_commands"
DEFAULT_GROUP = "other"

# (key, heading, one-line meaning), in display order. "start" holds the front-door commands;
# the five flow verbs follow; "evaluate" is the evaluation-job lifecycle.
GROUPS = (
    ("start", "Start here", "find your way around"),
    ("connect", "Connect", "wire tools, apps and destinations"),
    ("capture", "Capture", "what gets recorded"),
    ("enforce", "Enforce", "stop or limit things"),
    ("verify", "Verify", "prove records are intact and reproducible"),
    ("review", "Review", "look at what happened"),
    ("evaluate", "Evaluate", "datasets, secrets and evaluation runs"),
    ("other", "Other", ""),
)
GROUP_KEYS = tuple(key for key, _, _ in GROUPS)
VERBS = ("connect", "capture", "enforce", "verify", "review")


@dataclass
class CommandModule:
    """One discovered ``*_commands`` module and what became of it."""

    name: str                                   # e.g. "connect_commands"
    module: Any = None                          # None when the import failed
    group: str = DEFAULT_GROUP
    order: int = 100
    error: Optional[str] = None                 # import or register failure, one line
    commands: List[str] = field(default_factory=list)   # top-level commands it registered


def _one_line(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def module_names(package: Any = None) -> List[str]:
    """Sorted names of the ``*_commands`` modules in the CLI package."""
    if package is None:
        import briefcase.cli as package
    return sorted(
        info.name for info in pkgutil.iter_modules(package.__path__)
        if info.name.endswith(SUFFIX) and not info.ispkg
    )


def discover(package: Any = None) -> List[CommandModule]:
    """Import every ``*_commands`` module. Never raises: a module that cannot be imported
    comes back with ``error`` set and ``module`` ``None``."""
    if package is None:
        import briefcase.cli as package
    found = []
    for name in module_names(package):
        entry = CommandModule(name=name)
        try:
            module = importlib.import_module(f"{package.__name__}.{name}")
        except Exception as exc:  # noqa: BLE001 - one broken module must not break the CLI
            entry.error = f"import failed: {_one_line(exc)}"
            found.append(entry)
            continue
        entry.module = module
        if not callable(getattr(module, "register", None)):
            entry.error = "no register(subparsers) function"
        group = getattr(module, "GROUP", DEFAULT_GROUP)
        entry.group = group if isinstance(group, str) and group in GROUP_KEYS else DEFAULT_GROUP
        order = getattr(module, "ORDER", 100)
        entry.order = order if isinstance(order, int) and not isinstance(order, bool) else 100
        found.append(entry)
    return found


def register_all(subparsers: Any, modules: Optional[List[CommandModule]] = None) -> List[CommandModule]:
    """Call ``register`` on each discovered module and record which commands it added."""
    modules = discover() if modules is None else modules
    for entry in modules:
        if entry.module is None or entry.error:
            continue
        before = set(subparsers.choices)
        try:
            entry.module.register(subparsers)
        except Exception as exc:  # noqa: BLE001 - same isolation as the import
            entry.error = f"register failed: {_one_line(exc)}"
        entry.commands = [name for name in subparsers.choices if name not in before]
    return modules


def command_index(modules: Optional[List[CommandModule]] = None) -> Dict[str, CommandModule]:
    """Top-level command name -> the module that registered it. With no ``modules``, a
    throwaway parser is built to find out."""
    if modules is None:
        modules = register_all(argparse.ArgumentParser(add_help=False).add_subparsers())
    return {command: entry for entry in modules for command in entry.commands}


def warnings(modules: List[CommandModule]) -> List[str]:
    """One line per module that was skipped, for ``--debug``."""
    return [f"warning: skipped briefcase/cli/{entry.name}.py ({entry.error})"
            for entry in modules if entry.error]


def run_status_hooks(modules: List[CommandModule]):
    """Run each module's optional ``status()`` hook. Returns ``(rows, problems)``: the valid
    rows, each with the module's ``group`` added, and one line per hook that raised or
    returned something unusable."""
    rows: List[Dict[str, Any]] = []
    problems: List[str] = []
    seen = set()
    for entry in modules:
        hook = getattr(entry.module, "status", None) if entry.module is not None else None
        if not callable(hook):
            continue
        try:
            result = hook()
        except Exception as exc:  # noqa: BLE001 - a broken hook must not break `status`
            problems.append(f"warning: {entry.name}.status() raised {_one_line(exc)}")
            continue
        if result is None:
            continue
        for item in result if isinstance(result, (list, tuple)) else [result]:
            row = _valid_row(item)
            if row is None or row["id"] in seen:
                problems.append(f"warning: {entry.name}.status() returned an unusable row: {item!r}")
                continue
            seen.add(row["id"])
            row["group"] = entry.group
            row["source"] = entry.name
            rows.append(row)
    return rows, problems


def _valid_row(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    identifier, state = item.get("id"), item.get("state")
    if not isinstance(identifier, str) or not identifier or state not in ("on", "off", "attention"):
        return None
    next_command = item.get("next")
    row = {
        "id": identifier,
        "title": str(item.get("title") or identifier),
        "state": state,
        "detail": str(item.get("detail") or ""),
        "next": next_command if isinstance(next_command, str) and next_command else None,
    }
    if "data" in item:
        row["data"] = item["data"]
    return row
