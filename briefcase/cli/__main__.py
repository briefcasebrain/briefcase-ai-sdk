"""``briefcase-ai`` CLI entry point: ``briefcase-ai <command> ...`` (run ``python -m briefcase.cli``).

Commands are not listed here. Every ``briefcase/cli/*_commands.py`` module that exposes
``register(subparsers)`` is discovered and registered (see ``briefcase/cli/_discovery.py`` for
the module protocol: ``register``, ``GROUP`` and the optional ``status()`` hook). A module
that fails to import is skipped; ``--debug`` says which and why.

Three front doors:

- ``briefcase-ai`` with no arguments: the guided setup on a first interactive run, otherwise
  the status screen plus the grouped command list (exit 0, never a prompt in a script).
- ``briefcase-ai status``: what is on, what is off, what to do next.
- ``briefcase-ai features``: the catalog generated from ``briefcase/_features/*.json``.

``briefcase-ai --help`` groups commands under Connect, Capture, Enforce, Verify and Review
(plus "Start here" and "Evaluate"); ``briefcase-ai <command> --help`` is plain argparse.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import textwrap
from typing import Any, List

try:
    from briefcase.cli import _discovery, _ux
    from briefcase.cli.engine import OciJJEngine
    from briefcase.cli.state import Store

    _IMPORT_ERROR: Any = None
except BaseException as _exc:  # noqa: B036 - a hook command must still give its verdict (see main)
    _IMPORT_ERROR = _exc

DESCRIPTION = (
    "Compliance monitoring, auditing, and evaluating third-party artificial intelligence systems and models.\n\n"
    "Record what AI coding tools and LLM apps do, limit it, and prove the records "
    "are intact. Run `briefcase-ai` with no arguments for a guided setup."
)


class _GroupedParser(argparse.ArgumentParser):
    """Top-level parser whose ``--help`` lists commands under the flow verbs instead of
    argparse's single flat block. Subcommand parsers stay plain ``ArgumentParser``."""

    modules: List[Any] = []
    subparsers: Any = None

    def _command_help(self) -> dict:
        actions = getattr(self.subparsers, "_choices_actions", [])
        return {action.dest: (action.help or "") for action in actions}

    def _feature_counts(self) -> dict:
        try:
            from briefcase.features import load_catalog

            return {verb: len(items) for verb, items in load_catalog().groups()}
        except Exception:  # noqa: BLE001 - help must print even if the manifests are unreadable
            return {}

    def format_commands(self) -> str:
        """The grouped command listing (also printed by a bare ``briefcase-ai``)."""
        helps = self._command_help()
        by_group: dict = {}
        for entry in sorted(self.modules, key=lambda module: module.order):
            for command in entry.commands:
                by_group.setdefault(entry.group, []).append(command)
        width = max([len(name) for name in helps] + [8]) + 2
        columns = max(shutil.get_terminal_size().columns - 2, 60)
        counts = self._feature_counts()
        lines = []
        for key, heading, meaning in _discovery.GROUPS:
            commands = by_group.get(key, [])
            is_verb = key in _discovery.VERBS
            if not commands and not is_verb:
                continue
            lines.append(f"{heading}: {meaning}" if meaning else heading)
            for command in commands:
                wrapped = textwrap.wrap(helps.get(command) or "", max(columns - width - 4, 30)) or [""]
                lines.append(f"  {command:<{width}}{wrapped[0]}".rstrip())
                lines.extend(f"  {'':<{width}}{more}" for more in wrapped[1:])
            if is_verb and counts.get(key):
                count = f"{counts[key]} feature" + ("" if counts[key] == 1 else "s")
                lines.append(f"  ({count} in this group: briefcase-ai features --verb {key})")
            elif is_verb and not commands:
                lines.append("  (nothing installed for this step yet)")
            lines.append("")
        return "\n".join(lines).rstrip("\n")

    def format_help(self) -> str:
        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        options = self._get_formatter()
        options.start_section("Global options")
        options.add_arguments([action for action in self._actions if action.option_strings])
        options.end_section()
        banner = [_ux.banner_text(), ""] if _ux.is_tty(sys.stdout) else []
        return "\n".join([
            *banner,
            formatter.format_help().rstrip("\n"),
            "",
            self.format_commands(),
            "",
            options.format_help().rstrip("\n"),
            "",
            "Run `briefcase-ai <command> --help` for one command, `briefcase-ai status` to see where "
            "you are,\nand `briefcase-ai features` for everything briefcase can do.",
            "",
        ])


def build_parser() -> _GroupedParser:
    from briefcase import __version__

    parser = _GroupedParser(
        prog="briefcase-ai", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"{_ux.PRODUCT} {__version__}")
    parser.add_argument("--server", help="oci-jj-server gRPC endpoint (default $OCI_JJ_SERVER).")
    parser.add_argument("--repo", help="oci-jj repository / image family (default $OCI_JJ_REPO).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the resolved oci-jj command for a run submit instead of executing it.",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Show tracebacks and name any command module that failed to load (or set BRIEFCASE_DEBUG=1).",
    )
    parser.add_argument(
        "--no-input", action="store_true",
        help="Never prompt; a bare `briefcase-ai` prints status and this list (or set BRIEFCASE_NO_INPUT=1).",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>", parser_class=argparse.ArgumentParser)
    parser.subparsers = sub
    # Every briefcase/cli/*_commands.py with a register(subparsers); one broken module is
    # skipped, not fatal.
    parser.modules = _discovery.register_all(sub)
    return parser


def make_engine(args, *, env=None):
    """Select the engine from ``$BRIEFCASE_ENGINE`` (``grpc`` | ``cli`` | ``auto``, default ``auto``).

    ``cli`` → the subprocess ``OciJJEngine``. ``grpc`` → ``GrpcEngine`` (fails loudly if the server is
    unreachable — an explicit choice). ``auto`` → prefer ``GrpcEngine`` (gRPC reflection), falling back
    to the ``oci-jj`` binary only if reflection is unavailable *and* the binary is on PATH.
    """
    env = env if env is not None else os.environ
    mode = env.get("BRIEFCASE_ENGINE", "auto").lower()
    server, repo = getattr(args, "server", None), getattr(args, "repo", None)
    if mode == "cli":
        return OciJJEngine(server=server, repo=repo)

    from briefcase.cli.grpc_engine import GrpcEngine, grpc_available

    if mode == "grpc":
        return GrpcEngine(server=server, repo=repo)
    # auto: prefer gRPC when its optional [run] deps are installed (reflection resolves lazily on the
    # first real exec, with a clear "is the stack up?" error if the server is unreachable). Otherwise
    # use the subprocess oci-jj engine. Construction is I/O-free either way, so --dry-run stays offline.
    if grpc_available():
        return GrpcEngine(server=server, repo=repo)
    return OciJJEngine(server=server, repo=repo)


class _LazyEngine:
    """Defer engine construction until a handler first touches it, so engine-free commands
    (``dataset``/``secret``/``run list``…) never open a gRPC channel or shell out."""

    def __init__(self, factory) -> None:
        self._factory = factory
        self._engine = None

    def _resolve(self):
        if self._engine is None:
            self._engine = self._factory()
        return self._engine

    def __getattr__(self, name):
        return getattr(self._resolve(), name)


def _subparser(parser, name):
    choices = getattr(getattr(parser, "subparsers", None), "choices", {}) or {}
    return choices.get(name)


def _binds_anywhere(argv) -> bool:
    return any("--fail-closed" in a or "--require-policy-sha256" in a for a in argv)


def _hook_invocation(argv):
    """``(tool, options)`` for ``[global options] hook <tool> ...``: the console entry's scanner, the only one. When
    that cannot be imported, any command line that names ``hook`` and a binding flag anywhere is taken as a hook
    invocation that must block (the tool is read from after ``hook``, else unknown)."""
    try:
        from briefcase_entry import hook_invocation

        return hook_invocation(argv)
    except BaseException:  # noqa: B036 - the scanner itself is unavailable
        if "hook" in argv and _binds_anywhere(argv):
            return "", argv  # which token is the tool cannot be told reliably: block directly (see main)
        return (argv[1], argv[2:]) if argv[:1] == ["hook"] and argv[1:2] in (["cursor"], ["claude-code"], ["codex"]) \
            else None


_NOTHING_LOADS = "Blocked by briefcase-ai policy: the hook could not start and it is bound to a policy."


def _blocked_directly(argv) -> int:
    # A global operand may be named "hook". Only the subcommand is followed by the host name.
    tool = next((host for command, host in zip(argv, argv[1:])
                 if command == "hook" and host in ("codex", "claude-code", "cursor")), "")
    try:
        from briefcase.policy import hook_main

        return hook_main.block_in_process(tool)
    except BaseException:  # noqa: B036
        pass
    if tool == "cursor":
        denial = ('{"permission":"deny","user_message":"' + _NOTHING_LOADS +
                  '","agent_message":"' + _NOTHING_LOADS + '"}')
    else:
        denial = ('{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",'
                  '"permissionDecisionReason":"' + _NOTHING_LOADS + '"}}')
    for fd, message, status in ((2, _NOTHING_LOADS, 2), (1, denial, 0)):
        try:
            data = (message + "\n").encode("ascii")
            while data:
                count = os.write(fd, data)
                if count <= 0:
                    break
                data = data[count:]
            else:
                return status
        except BaseException:  # noqa: B036
            continue
    return 2


def _hook_unavailable(tool, rest) -> int:
    """The hook dispatcher could not be imported or crashed outright. What the command line promised is decided by the
    same rule as everywhere else (``enforcing``): plain Cursor forwarding stays silent, an enforcing invocation gives
    its verdict, without relying on anything that just failed."""
    try:
        from briefcase.policy import hook_main

        if not hook_main.enforcing_for(tool, list(rest)):
            return 0
        return hook_main.on_failure(tool, list(rest))
    except BaseException:  # noqa: B036
        pass
    try:
        import briefcase_entry

        return briefcase_entry.last_resort(tool, list(rest))
    except BaseException:  # noqa: B036 - nothing of the SDK loads: the command line alone decides
        if _binds_anywhere(rest):
            try:
                os.write(2, b"Blocked by briefcase-ai policy: the hook could not start and it is bound to a policy.\n")
            except BaseException:  # noqa: B036
                pass
            return 2
        return 0


def main(argv=None, store=None, engine=None, stack=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    found = _hook_invocation(argv)
    if found is not None and found[0] == "":
        return _blocked_directly(argv)
    if found is not None:
        # Hook entry points: must stay silent and fail open (or block, when the command line binds them), so they
        # bypass argparse (which exits non-zero and prints on a bad flag), command discovery and the store. The import
        # of the dispatcher is inside the handler too: a failure there still produces the verdict the line promised.
        try:
            from briefcase.cli import hook_commands

            return hook_commands.dispatch(found[0], found[1])
        except BaseException:  # noqa: B036
            return _hook_unavailable(found[0], found[1])
    if _IMPORT_ERROR is not None:  # not a hook: the failure is the error
        raise _IMPORT_ERROR
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra:
        if args.command == "record-help":
            print("Unexpected record-help argument. Quote the question or pipe it on one line.", file=sys.stderr)
            return 2
        parser.error("unrecognized arguments: " + " ".join(extra))
    _ux.set_debug(args.debug)
    _ux.set_no_input(args.no_input)
    if args.command == "record-help":
        # Free text and pasted launch commands must never enter general exception or debug diagnostics.
        return _run(lambda: args.func(args, None, None, None), (), private=True)
    if _ux.debug_enabled():
        for line in _discovery.warnings(parser.modules):
            print(line, file=sys.stderr)
    if not args.command:
        # The front door: guided setup on a first interactive run, else status + command list.
        from briefcase.cli import setup_commands

        return _run(lambda: setup_commands.front_door(parser.format_commands(), parser.modules), argv)
    if not getattr(args, "func", None):
        # A command group without its subcommand (`briefcase-ai run`): show that group's help.
        (_subparser(parser, args.command) or parser).print_help(sys.stderr)
        return 2
    # Lazy for the same reason as the engine: opening the store creates ~/.briefcase, which
    # `connect --dry-run`, `mcp link` and friends must not do.
    store = store or _LazyEngine(Store)
    engine = engine or _LazyEngine(lambda: make_engine(args))
    return _run(lambda: args.func(args, store, engine, stack), argv)


_COMMAND_WORD = re.compile(r"^[a-z][a-z0-9-]*$")


def _scrub_argv(argv):
    """The part of ``argv`` that is safe to print in a retry hint: the command words that lead it. Everything from the
    first option, value or ``KEY=VALUE`` onward may carry a credential (``--headers``, an unambiguous prefix of it
    such as ``--head``, ``--token=...``, ``secret set KEY=VALUE``), whatever form argparse accepted it in, so it is
    replaced by one marker and never echoed."""
    out = []
    for index, word in enumerate(argv):
        if not _COMMAND_WORD.match(word) or index >= 3:
            out.append("<same arguments>")
            break
        out.append(word)
    return out


def _run(call, argv, *, private=False) -> int:
    """Run a handler. An unexpected exception becomes one ``error:`` line and exit 1; the
    traceback is shown only with ``--debug``."""
    try:
        return call()
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except BrokenPipeError:
        # The reader went away (`briefcase-ai features | head`). Point stdout at /dev/null so the
        # interpreter's exit-time flush does not raise again.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        return 0
    except Exception as exc:  # noqa: BLE001
        if private:
            print("Record guidance could not finish. Retry briefcase-ai record-help --static.", file=sys.stderr)
            return 1
        if _ux.debug_enabled():
            raise
        message = " ".join(str(exc).split())
        rerun = " ".join(_scrub_argv([word for word in argv if word != "--debug"]))
        return _ux.fail(f"{type(exc).__name__}: {message}" if message else type(exc).__name__,
                        fix=f"briefcase-ai --debug {rerun}".rstrip() + "   # shows the full traceback")


if __name__ == "__main__":
    sys.exit(main())
