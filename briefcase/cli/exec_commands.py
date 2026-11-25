"""``briefcase-ai exec -- <command...>``: run a program with decision capture
switched on, without editing it.

The command is run with two additions to its environment:

- ``BRIEFCASE_AUTO=1``
- ``briefcase/_bootstrap`` prepended to ``PYTHONPATH``, so every Python
  interpreter the command starts imports ``briefcase/_bootstrap/sitecustomize.py``,
  which calls ``briefcase.observe()`` (destination from the environment)
  and ``briefcase.auto(lazy=True)``.

The destination is whatever ``briefcase.observe()`` resolves:
``BRIEFCASE_EXPORTER`` (or ``--to``), the ``OTEL_EXPORTER_OTLP_*`` variables,
else JSON lines on stderr.

On POSIX the command replaces this process (``execvpe``), so its exit code
and every signal sent to it behave exactly as if it had been started
directly. Elsewhere it runs as a child whose exit code is returned.

Also runnable on its own::

    python -m briefcase.cli.exec_commands -- python app.py

Checked before the program starts, so a mistake fails here (exit code 2)
instead of producing a run with no records:

- the destination: an unknown ``BRIEFCASE_EXPORTER`` / ``--to`` value, a
  malformed OTLP endpoint or an unsupported OTLP protocol;
- ``--skip`` names that are not integrations;
- a Python command line (or a script's ``#!`` line) with ``-I``, ``-E`` or
  ``-S``: such an interpreter ignores ``PYTHONPATH`` (``-I``, ``-E``) or
  never imports ``sitecustomize`` (``-S``), so it cannot be instrumented
  this way. ``--allow-uninstrumented`` runs it anyway, with a warning.

Limits: only Python programs are instrumented, and only interpreters that
can import briefcase. A program that starts another Python with ``-I`` /
``-E`` / ``-S`` itself, or one from an environment without briefcase,
leaves that child uninstrumented (the child says so on stderr in the
second case).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
from typing import Dict, List, Mapping, Optional, Sequence

from briefcase._bootstrap import BOOTSTRAP_DIR

GROUP = "connect"

ENV_AUTO = "BRIEFCASE_AUTO"
ENV_EXPORTER = "BRIEFCASE_EXPORTER"
ENV_CAPTURE_CONTENT = "BRIEFCASE_CAPTURE_CONTENT"
ENV_SKIP = "BRIEFCASE_AUTO_SKIP"
ENV_DEDUPE = "BRIEFCASE_AUTO_DEDUPE"

_PYTHON_NAME = re.compile(r"^(python|pypy)[0-9.]*(\.exe)?$", re.IGNORECASE)
# Interpreter options that take a value as the next argument.
_PYTHON_VALUE_OPTIONS = ("-W", "-X", "--check-hash-based-pycs")
_BLOCKING_FLAGS = {
    "I": "-I (isolated mode) ignores PYTHONPATH",
    "E": "-E ignores PYTHONPATH and every other PYTHON* variable",
    "S": "-S does not import site, so sitecustomize never runs",
}

EXIT_USAGE = 2
EXIT_CANNOT_EXECUTE = 126
EXIT_NOT_FOUND = 127


def build_env(
    base: Optional[Mapping[str, str]] = None,
    *,
    to: Optional[str] = None,
    capture_content: Optional[str] = None,
    skip: Optional[str] = None,
    dedupe: Optional[str] = None,
) -> Dict[str, str]:
    """Environment for the child: ``base`` plus the bootstrap settings.
    Explicit arguments win over what ``base`` already holds."""
    env = dict(os.environ if base is None else base)
    env[ENV_AUTO] = "1"
    existing = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    # Keep the bootstrap directory first (and only once) so its
    # sitecustomize is the one Python imports.
    env["PYTHONPATH"] = os.pathsep.join(
        [BOOTSTRAP_DIR] + [p for p in existing if os.path.abspath(p) != BOOTSTRAP_DIR]
    )
    if to:
        env[ENV_EXPORTER] = to
    if capture_content:
        env[ENV_CAPTURE_CONTENT] = capture_content
    if skip:
        env[ENV_SKIP] = skip
    if dedupe:
        env[ENV_DEDUPE] = dedupe
    return env


def blocking_python_flags(argv: Sequence[str]) -> List[str]:
    """Reasons why the Python command line ``argv`` (``argv[0]`` is the
    interpreter) cannot be instrumented: its ``-I`` / ``-E`` / ``-S``
    options. Empty when there are none. Only the interpreter's own options
    are read: scanning stops at ``-c``, ``-m``, ``-`` , ``--`` or the script."""
    reasons: List[str] = []
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in ("-", "--") or not arg.startswith("-"):
            break
        if arg.startswith("--"):
            index += 2 if arg in _PYTHON_VALUE_OPTIONS else 1
            continue
        stop = False
        for position, letter in enumerate(arg[1:], 1):
            if letter in ("c", "m"):
                stop = True
                break
            if letter in ("W", "X"):
                if position == len(arg) - 1:
                    index += 1  # the value is the next argument
                break
            reason = _BLOCKING_FLAGS.get(letter)
            if reason and reason not in reasons:
                reasons.append(reason)
        if stop:
            break
        index += 1
    return reasons


def _shebang(path: str) -> List[str]:
    """The interpreter command of a script's ``#!`` line, or []."""
    try:
        with open(path, "rb") as handle:
            first = handle.readline(512)
    except OSError:
        return []
    if not first.startswith(b"#!"):
        return []
    try:
        parts = first[2:].decode("utf-8", "replace").split()
    except Exception:
        return []
    if parts and os.path.basename(parts[0]) == "env":
        parts = parts[1:]
        # env's own options (-S, -i, -u NAME ...) and NAME=value assignments
        while parts and (parts[0].startswith("-") or "=" in parts[0]):
            parts = parts[1:]
    return parts


def uninstrumentable(command: Sequence[str], env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Why ``command`` would run without instrumentation, as far as that
    can be seen from here: interpreter options on the command line or on
    the ``#!`` line of the program. Empty when nothing is known against it."""
    if not command:
        return []
    if _PYTHON_NAME.match(os.path.basename(command[0])):
        return blocking_python_flags(command)
    path = (env or os.environ).get("PATH")
    resolved = shutil.which(command[0], path=path) or command[0]
    line = _shebang(resolved)
    if line and _PYTHON_NAME.match(os.path.basename(line[0])):
        return blocking_python_flags(line)
    return []


def _known_integrations() -> List[str]:
    from briefcase.auto import _SUPPORTED

    return sorted(_SUPPORTED)


def preflight(command: Sequence[str], env: Mapping[str, str],
              allow_uninstrumented: bool = False) -> Optional[str]:
    """The error to stop on before running ``command`` with ``env``, or
    None. See the module docstring for what is checked."""
    from briefcase._observe import check_environment

    problem = check_environment(env)
    if problem:
        return problem
    known = _known_integrations()
    unknown = [
        name.strip() for name in (env.get(ENV_SKIP) or "").split(",")
        if name.strip() and name.strip() not in known
    ]
    if unknown:
        return (f"unknown integration name(s) in --skip / ${ENV_SKIP}: {', '.join(unknown)}. "
                f"Known: {', '.join(known)}.")
    content = (env.get(ENV_CAPTURE_CONTENT) or "").strip().lower()
    if content and content not in ("none", "hash", "full"):
        return f"${ENV_CAPTURE_CONTENT}={content!r} is not one of none, hash, full."
    dedupe = (env.get(ENV_DEDUPE) or "").strip().lower()
    if dedupe and dedupe not in ("none", "provider", "framework"):
        return f"${ENV_DEDUPE}={dedupe!r} is not one of none, provider, framework."
    reasons = uninstrumentable(command, env)
    if reasons:
        message = (
            "this command cannot be instrumented: " + "; ".join(reasons) + ". briefcase-ai exec "
            "works through PYTHONPATH and sitecustomize. Remove the option, or call "
            "briefcase.observe() and briefcase.auto() in the program instead."
        )
        if not allow_uninstrumented:
            return message + " (--allow-uninstrumented runs it anyway, without records.)"
        print(f"briefcase-ai exec: warning: {message} Running it WITHOUT records.", file=sys.stderr)
    return None


def _strip_separator(command: Sequence[str]) -> List[str]:
    command = list(command)
    if command and command[0] == "--":
        command = command[1:]
    return command


def _run_child(command: List[str], env: Dict[str, str]) -> int:
    """Run ``command`` as a child process, forwarding termination signals,
    and return its exit code (``128 + N`` when signal N killed it)."""
    if os.name != "posix":
        # CreateProcess does not resolve .cmd / .bat launchers or consult
        # the child's PATH; do what the shell would.
        resolved = shutil.which(command[0], path=env.get("PATH"))
        if resolved:
            command = [resolved] + list(command[1:])
    try:
        child = subprocess.Popen(command, env=env)
    except FileNotFoundError:
        print(f"briefcase-ai exec: command not found: {command[0]}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except OSError as exc:
        print(f"briefcase-ai exec: cannot run {command[0]}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_EXECUTE

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except OSError:
            pass

    previous = {}
    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.signal(signum, forward)
        except (ValueError, OSError):  # not the main thread / unsupported
            pass
    try:
        while True:
            try:
                code = child.wait()
                break
            except KeyboardInterrupt:
                continue
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
    return 128 - code if code < 0 else code


def run(
    command: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    to: Optional[str] = None,
    capture_content: Optional[str] = None,
    skip: Optional[str] = None,
    dedupe: Optional[str] = None,
    replace_process: Optional[bool] = None,
    allow_uninstrumented: bool = False,
) -> int:
    """Run ``command`` with auto-instrumentation enabled.

    ``replace_process`` defaults to True on POSIX: the command takes over
    this process and the call does not return. With False (the default on
    Windows) the command runs as a child and its exit code is returned.
    """
    command = _strip_separator(command)
    if not command:
        print(
            "briefcase-ai exec: no command given. Usage: briefcase-ai exec -- <command> [args...]",
            file=sys.stderr,
        )
        return EXIT_USAGE
    child_env = build_env(env, to=to, capture_content=capture_content, skip=skip, dedupe=dedupe)
    try:
        problem = preflight(command, child_env, allow_uninstrumented)
    except Exception as exc:  # a broken check must not block the program
        problem = None
        print(f"briefcase-ai exec: warning: could not check the configuration ({exc})",
              file=sys.stderr)
    if problem:
        print(f"briefcase-ai exec: {problem}", file=sys.stderr)
        return EXIT_USAGE
    if replace_process is None:
        replace_process = os.name == "posix"
    if not replace_process:
        return _run_child(command, child_env)

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        os.execvpe(command[0], command, child_env)
    except FileNotFoundError:
        print(f"briefcase-ai exec: command not found: {command[0]}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except OSError as exc:
        print(f"briefcase-ai exec: cannot run {command[0]}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_EXECUTE
    return 0  # pragma: no cover - execvpe does not return on success


def cmd_exec(args, store=None, engine=None, stack=None) -> int:
    """Handler in the shape ``briefcase.cli.__main__`` calls:
    ``func(args, store, engine, stack)``. None of the three is used."""
    return run(
        getattr(args, "exec_command", None) or [],
        to=getattr(args, "to", None),
        capture_content=getattr(args, "capture_content", None),
        skip=getattr(args, "skip", None),
        dedupe=getattr(args, "dedupe", None),
        replace_process=False if getattr(args, "no_replace", False) else None,
        allow_uninstrumented=bool(getattr(args, "allow_uninstrumented", False)),
    )


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--to", metavar="DEST",
        help="Where records go: console, a path ending in .jsonl, chain:PATH.db, an http(s):// URL, "
             "or otlp (sets $BRIEFCASE_EXPORTER; default: the environment, else stderr).",
    )
    parser.add_argument(
        "--capture-content", choices=("none", "hash", "full"),
        help="Record prompt/completion content (sets $BRIEFCASE_CAPTURE_CONTENT; default none).",
    )
    parser.add_argument(
        "--skip", metavar="NAMES",
        help="Comma-separated integrations to leave alone, e.g. langchain,bedrock "
             "(sets $BRIEFCASE_AUTO_SKIP).",
    )
    parser.add_argument(
        "--dedupe", choices=("none", "provider", "framework"),
        help="When a framework and a provider SDK both record a model call: keep both, "
             "linked (none, default), or drop the provider / the framework record "
             "(sets $BRIEFCASE_AUTO_DEDUPE).",
    )
    parser.add_argument(
        "--allow-uninstrumented", action="store_true",
        help="Run a command that cannot be instrumented (python -I/-E/-S) anyway, "
             "with a warning, instead of refusing.",
    )
    parser.add_argument(
        "--no-replace", action="store_true",
        help="Run the command as a child process instead of replacing this one.",
    )
    parser.add_argument(
        "exec_command", nargs=argparse.REMAINDER, metavar="-- command [args...]",
        help="The program to run, after a literal --.",
    )
    parser.set_defaults(func=cmd_exec)


# Where `briefcase-ai --help` lists this command (see briefcase/cli/_discovery.py).
GROUP = "connect"


def register(subparsers) -> argparse.ArgumentParser:
    """Add the ``exec`` subcommand to the ``briefcase-ai`` CLI's subparsers
    (the object returned by ``parser.add_subparsers(...)``)."""
    parser = subparsers.add_parser(
        "exec",
        help="Run a command with decision capture enabled (no code changes).",
        description="Run a Python program with briefcase auto-instrumentation: every installed "
                    "framework and provider SDK emits decision records, with no code changes.",
    )
    _add_arguments(parser)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m briefcase.cli.exec_commands",
        description="Run a Python program with briefcase auto-instrumentation.",
    )
    _add_arguments(parser)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args, None, None, None)


if __name__ == "__main__":
    sys.exit(main())
