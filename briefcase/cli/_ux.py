"""Shared output helpers for ``briefcase`` commands. Use these instead of ad-hoc prints so
every command reads the same way.

    from briefcase.cli import _ux

    def register(sub):
        p = sub.add_parser("budget", help="Show and set spend limits.")
        _ux.add_json_flag(p)
        p.set_defaults(func=cmd_budget)

    def cmd_budget(args, store, engine, stack):
        state = load()
        if state is None:
            return _ux.fail("no budget is configured", fix="briefcase budget set 20")
        if args.json:
            return _ux.print_json(state)
        print(f"budget: {state['limit']} USD")
        _ux.print_next("briefcase status", "see everything that is on")
        return 0

What is here:

- ``print_next(command, reason=None)``: the one ``Next:`` line a command ends with on
  success. Never print two.
- ``fail(message, fix=None, code=1)``: ``error: ...`` on stderr plus a ``Fix:`` line holding
  the exact command that resolves it; returns the exit code (1 failed, 2 usage).
- ``print_json(obj)`` / ``add_json_flag(parser)``: the ``--json`` convention. JSON goes to
  stdout alone, with no ``Next:`` line and no colour.
- ``is_interactive()``: true only when stdin and stdout are terminals and neither
  ``--no-input`` nor ``BRIEFCASE_NO_INPUT`` is set. Check it before any prompt; when it is
  false, do the non-interactive thing and exit, never block.
- ``confirm(question)`` / ``choose(question, options)`` / ``ask(prompt)``: prompts.
  ``confirm`` returns ``None`` when there is nobody to ask.
- ``use_color(stream)`` / ``style(text, name)``: colour only on a terminal, never when
  ``NO_COLOR`` is set (https://no-color.org) or ``TERM=dumb``.
- ``debug_enabled()``: whether ``--debug`` (or ``BRIEFCASE_DEBUG``) is on; only then may a
  traceback be shown.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Optional, Sequence, Tuple

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2

ENV_NO_INPUT = "BRIEFCASE_NO_INPUT"
ENV_DEBUG = "BRIEFCASE_DEBUG"

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}
_STATE_STYLE = {"on": "green", "off": "dim", "attention": "yellow"}

_no_input = False
_debug = False


# --- modes --------------------------------------------------------------------

def _truthy(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() not in ("", "0", "false", "no", "off")


def set_no_input(value: bool) -> None:
    """Set by the top-level ``--no-input`` flag."""
    global _no_input
    _no_input = bool(value)


def set_debug(value: bool) -> None:
    """Set by the top-level ``--debug`` flag."""
    global _debug
    _debug = bool(value)


def debug_enabled() -> bool:
    return _debug or _truthy(os.environ.get(ENV_DEBUG))


def is_tty(stream: Any = None) -> bool:
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def is_interactive() -> bool:
    """A person is at the keyboard and has not asked for a prompt-free run."""
    if _no_input or _truthy(os.environ.get(ENV_NO_INPUT)):
        return False
    return is_tty(sys.stdin) and is_tty(sys.stdout)


def use_color(stream: Any = None) -> bool:
    if os.environ.get("NO_COLOR"):  # any non-empty value, per no-color.org
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return is_tty(stream)


def style(text: str, name: str, stream: Any = None) -> str:
    """``text`` in the named style (bold, dim, red, green, yellow, cyan) when colour is on."""
    code = _CODES.get(name)
    if not code or not use_color(stream):
        return text
    return f"\033[{code}m{text}\033[0m"


def state_label(state: str, width: int = 9, stream: Any = None) -> str:
    """Fixed-width ``on`` / ``off`` / ``attention`` cell for status listings."""
    return style(f"{state:<{width}}", _STATE_STYLE.get(state, ""), stream)


# --- output -------------------------------------------------------------------

def next_line(command: str, reason: Optional[str] = None) -> str:
    """``Next: <command>``, with the reason as a trailing shell comment so the whole line
    can still be pasted."""
    return f"Next: {command}" + (f"  # {reason}" if reason else "")


def print_next(command: str, reason: Optional[str] = None, out: Callable[[str], Any] = print) -> None:
    out(next_line(command, reason))


def fail(message: str, fix: Optional[str] = None, code: int = EXIT_FAILED) -> int:
    """Print what went wrong and the command that fixes it; return the exit code.

    ``return _ux.fail("the receiver is not running", fix="briefcase receive --detach")``
    """
    print(f"error: {message}", file=sys.stderr)
    if fix:
        print(f"Fix: {fix}", file=sys.stderr)
    return code


def add_json_flag(parser: Any, help: str = "Print machine-readable JSON instead of text.") -> None:
    parser.add_argument("--json", action="store_true", help=help)


def print_json(obj: Any, out: Callable[[str], Any] = print) -> int:
    """Print ``obj`` as indented JSON; returns 0 so a handler can ``return print_json(...)``."""
    out(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    return EXIT_OK


# --- prompts ------------------------------------------------------------------

def ask(prompt: str) -> Optional[str]:
    """One line from the user, stripped; ``None`` on end of input or Ctrl-C."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def confirm(question: str, default: bool = True, assume_yes: bool = False) -> Optional[bool]:
    """``True`` / ``False``, or ``None`` when not interactive (the caller then refuses to
    write, or asks for ``--yes``). End of input counts as "no"."""
    if assume_yes:
        return True
    if not is_interactive():
        return None
    answer = ask(f"{question} [{'Y/n' if default else 'y/N'}] ")
    if answer is None:
        return False
    return default if not answer else answer.lower() in ("y", "yes")


def choose(question: str, options: Sequence[Tuple[str, str]], default: Optional[str] = None,
           attempts: int = 3) -> Optional[str]:
    """Numbered single choice. ``options`` is ``[(key, label), ...]``; the answer may be the
    number, the key or its first letter. Returns the key, or ``None`` on end of input or after
    ``attempts`` unusable answers."""
    print(question)
    for index, (key, label) in enumerate(options, 1):
        print(f"  {index}) {label}")
    keys = [key for key, _ in options]
    hint = "/".join(str(i) for i in range(1, len(options) + 1))
    for _ in range(attempts):
        answer = ask(f"Choice [{hint}]{f' (default {keys.index(default) + 1})' if default in keys else ''}: ")
        if answer is None:
            return None
        if not answer and default in keys:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return keys[int(answer) - 1]
        lowered = answer.lower()
        matches = [key for key in keys if key == lowered] or [key for key in keys if key.startswith(lowered)]
        if lowered and len(matches) == 1:
            return matches[0]
        print(f"Please answer with a number from 1 to {len(options)}.")
    return None
