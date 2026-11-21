"""``briefcase exec -- <command...>``: run a program with decision capture
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

Limits: an interpreter started with ``-I`` / ``-E`` (or ``-S``) ignores
``PYTHONPATH`` (or ``site``) and is not instrumented; only Python programs
are.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from typing import Dict, List, Mapping, Optional, Sequence

from briefcase._bootstrap import BOOTSTRAP_DIR

ENV_AUTO = "BRIEFCASE_AUTO"
ENV_EXPORTER = "BRIEFCASE_EXPORTER"
ENV_CAPTURE_CONTENT = "BRIEFCASE_CAPTURE_CONTENT"
ENV_SKIP = "BRIEFCASE_AUTO_SKIP"

EXIT_USAGE = 2
EXIT_CANNOT_EXECUTE = 126
EXIT_NOT_FOUND = 127


def build_env(
    base: Optional[Mapping[str, str]] = None,
    *,
    to: Optional[str] = None,
    capture_content: Optional[str] = None,
    skip: Optional[str] = None,
) -> Dict[str, str]:
    """Environment for the child: ``base`` plus the bootstrap settings."""
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
    return env


def _strip_separator(command: Sequence[str]) -> List[str]:
    command = list(command)
    if command and command[0] == "--":
        command = command[1:]
    return command


def _run_child(command: List[str], env: Dict[str, str]) -> int:
    """Run ``command`` as a child process, forwarding termination signals,
    and return its exit code (``128 + N`` when signal N killed it)."""
    try:
        child = subprocess.Popen(command, env=env)
    except FileNotFoundError:
        print(f"briefcase exec: command not found: {command[0]}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except OSError as exc:
        print(f"briefcase exec: cannot run {command[0]}: {exc}", file=sys.stderr)
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
    replace_process: Optional[bool] = None,
) -> int:
    """Run ``command`` with auto-instrumentation enabled.

    ``replace_process`` defaults to True on POSIX: the command takes over
    this process and the call does not return. With False (the default on
    Windows) the command runs as a child and its exit code is returned.
    """
    command = _strip_separator(command)
    if not command:
        print(
            "briefcase exec: no command given. Usage: briefcase exec -- <command> [args...]",
            file=sys.stderr,
        )
        return EXIT_USAGE
    child_env = build_env(env, to=to, capture_content=capture_content, skip=skip)
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
        print(f"briefcase exec: command not found: {command[0]}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except OSError as exc:
        print(f"briefcase exec: cannot run {command[0]}: {exc}", file=sys.stderr)
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
        replace_process=False if getattr(args, "no_replace", False) else None,
    )


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--to", metavar="DEST",
        help="Where records go: console, a path ending in .jsonl, an http(s):// URL, or "
             "otlp (sets $BRIEFCASE_EXPORTER; default: the environment, else stderr).",
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
        "--no-replace", action="store_true",
        help="Run the command as a child process instead of replacing this one.",
    )
    parser.add_argument(
        "exec_command", nargs=argparse.REMAINDER, metavar="-- command [args...]",
        help="The program to run, after a literal --.",
    )
    parser.set_defaults(func=cmd_exec)


def register(subparsers) -> argparse.ArgumentParser:
    """Add the ``exec`` subcommand to the ``briefcase`` CLI's subparsers
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
