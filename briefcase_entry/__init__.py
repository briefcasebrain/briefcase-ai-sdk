"""The ``briefcase-ai`` console script's entry point, in a package of its own that imports only the standard library.

``briefcase-ai hook <tool>`` runs before every tool call of a coding tool, and a failure to start must not become an
allow: Codex and the others treat an ordinary exit status as a failed hook and run the call anyway. Everything of this
SDK that can fail to import (``briefcase/__init__``, ``briefcase.cli`` and what it imports) therefore stays behind the
decision made here, which is a scan of ``argv`` for the ``hook`` subcommand after any global options. An enforcing
hook then runs the isolated launcher (``briefcase/policy/hook_main.py``, which gives a verdict on any failure) by path,
without importing ``briefcase``; if even that file cannot be found or run, a command line that binds the hook is
blocked here with a fixed reason. Every other command goes to the ordinary CLI."""
import os
import sys

_HOOK_TOOLS = ("claude-code", "codex", "cursor")
_GLOBAL_VALUE_OPTIONS = ("--server", "--repo")  # the top-level options that take a value (argparse allows prefixes)


def hook_invocation(argv):
    """``(tool, options)`` when ``argv`` is ``[global options] hook <tool> [options]``, else ``None``."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            break
        name, eq, _ = token.partition("=")
        if not eq and len(name) > 2 and name.startswith("--") and any(
                option.startswith(name) for option in _GLOBAL_VALUE_OPTIONS):
            index += 1  # its value is the next token
        index += 1
    if index + 1 < len(argv) and argv[index] == "hook" and argv[index + 1] in _HOOK_TOOLS:
        return argv[index + 1], argv[index + 2:]
    return None


def _policy_dir():
    """The ``briefcase/policy`` directory, found without importing ``briefcase`` (``PathFinder`` does not run its
    code)."""
    from importlib.machinery import PathFinder

    spec = PathFinder.find_spec("briefcase")
    for directory in (spec.submodule_search_locations or []) if spec else []:
        path = os.path.join(directory, "policy")
        if os.path.isfile(os.path.join(path, "hook_main.py")):
            return path
    return None


def _hook(tool, options):
    """Every decision (is it strict, is it enforcing, how a denial is delivered) is the launcher module's own, loaded
    by path: this file has no rules of its own to disagree with it."""
    launcher = None
    try:
        import importlib.util

        directory = _policy_dir()
        if directory is None:
            raise ImportError("briefcase/policy/hook_main.py was not found")
        path = os.path.join(directory, "hook_main.py")
        spec = importlib.util.spec_from_file_location("_briefcase_hook_main", path)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        if launcher.enforcing_for(tool, options):
            import runpy

            sys.argv = [path, tool, *options]
            try:
                runpy.run_path(path, run_name="__main__")
            except SystemExit as stop:
                code = stop.code
                return code if isinstance(code, int) else (0 if code is None else 1)
            return 0
        try:  # plain Cursor forwarding: silent, never blocks
            from briefcase.cli import hook_commands

            return hook_commands.run_cursor(options)
        except BaseException:  # noqa: B036
            return 0
    except BaseException:  # noqa: B036 - nothing may escape without the verdict the command line promised
        if launcher is not None and launcher.strict_for(options):
            return launcher.block(tool)
        if launcher is None:
            return 2 if any("--fail-closed" in a or "--require-policy-sha256" in a for a in options) else 0
        if tool == "cursor":
            launcher._fd_write(1, b'{"permission": "allow"}\n')  # Cursor blocks on empty output
        return 0


def main():
    argv = sys.argv[1:]
    found = hook_invocation(argv)
    if found is not None:
        return _hook(*found)
    from briefcase.cli.__main__ import main as cli_main

    return cli_main()
