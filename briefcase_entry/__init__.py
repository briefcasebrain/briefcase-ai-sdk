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
    options = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            break
        name, eq, _ = token.partition("=")
        if len(name) > 2 and name.startswith("--") and any(
                option.startswith(name) for option in _GLOBAL_VALUE_OPTIONS):
            index += 1 if eq else 2
            continue
        options.append(token)
        if name == "--require-policy-sha256" and not eq and index + 1 < len(argv):
            operand = argv[index + 1]
            if operand != "hook" and not operand.startswith("-"):
                options.append(operand)
                index += 1
        index += 1
    if index + 1 < len(argv) and argv[index] == "hook" and argv[index + 1] in _HOOK_TOOLS:
        # What stood before `hook` stays with the options: a binding there binds the hook as much as one after it.
        return argv[index + 1], options + argv[index + 2:]
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


# The denial for an entry that could not load the launcher (briefcase/policy/hook_main.py has the same bytes, and a test
# keeps them equal): this file must be able to deliver it on its own.
_REASON = (b"Blocked by briefcase-ai policy: the hook could not complete and it was installed with --fail-closed or "
           b"a required policy. Reinstall with `briefcase-ai policy init`, or turn enforcement off with "
           b"`briefcase-ai policy off`.")
_DENY = (b'{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", '
         b'"permissionDecisionReason": "' + _REASON + b'"}}\n')
_DENY_CURSOR = (b'{"permission": "deny", "user_message": "' + _REASON + b'", "agent_message": "' + _REASON + b'"}\n')


def _write(fd, data):
    try:
        while data:
            count = os.write(fd, data)
            if count <= 0:
                return False
            data = data[count:]
        return True
    except BaseException:  # noqa: B036
        return False


def _binds(options):
    return any("--fail-closed" in a or "--require-policy-sha256" in a for a in options)


def last_resort(tool, options):
    """What a hook invocation does when nothing of the policy package could decide: block with the fixed text when the
    command line binds the hook (the reason on stderr with exit 2, else the JSON denial on stdout with exit 0),
    otherwise allow (Cursor blocks on empty output, so it is told to allow)."""
    if _binds(options):
        if _write(2, _REASON + b"\n"):
            return 2
        return 0 if _write(1, _DENY_CURSOR if tool == "cursor" else _DENY) else 2
    if tool == "cursor":
        _write(1, b'{"permission": "allow"}\n')
    return 0


def _hook(tool, options):
    """Every decision (is it strict, is it enforcing) is the launcher module's own, loaded by path: this file has no
    rule of its own to disagree with it. Only the fixed denial, for when the launcher cannot be loaded, is kept here."""
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
            entry = getattr(launcher, "main", None)  # the launcher's named entry, called here: nothing relies on the
            if not callable(entry):                  # file calling itself when it is run
                raise ImportError("hook_main.py has no main")
            sys.argv = [path, tool, *options]
            code = entry()
            if type(code) is not int or code not in (0, 2):
                raise TypeError("hook_main.main returned an invalid hook status")
            return code
        try:  # plain Cursor forwarding: silent, never blocks
            from briefcase.cli import hook_commands

            return hook_commands.dispatch(tool, options)
        except BaseException:  # noqa: B036
            return 0
    except BaseException:  # noqa: B036 - nothing may escape without the verdict the command line promised
        return last_resort(tool, options)


def main():
    argv = sys.argv[1:]
    found = hook_invocation(argv)
    if found is not None:
        return _hook(*found)
    from briefcase.cli.__main__ import main as cli_main

    return cli_main()
