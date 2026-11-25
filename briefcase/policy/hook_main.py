"""Fast entry point for the enforcement hook. Run by file path:

    python -I -S /path/to/briefcase/policy/hook_main.py claude-code --enforce

``briefcase-ai hook <tool> --enforce`` does the same thing, but starting it
imports the whole SDK (about 0.1 s) before every tool call. This file is what
``briefcase-ai policy init`` installs instead: it registers ``briefcase`` as an
empty package, so ``briefcase.policy`` is importable without running
``briefcase/__init__.py``, and then runs the same hook code.

``-I`` keeps the environment and the current directory out of ``sys.path``.
``-S`` skips ``site``; the hook needs only the standard library (it is left
out on Python older than 3.11, where TOML comes from the ``tomli`` package).

If the hook code cannot even be imported, the call is allowed (exit 0), or,
when the command line has ``--fail-closed``, blocked with exit code 2 and a
message on stderr, which all three supported tools treat as a block.
"""

import os
import sys


def _say(stream, text):
    """Write and flush, never raising: a closed pipe must not change the exit status the caller chose. Whether the text
    was delivered."""
    try:
        stream.write(text)
        stream.flush()
        return True
    except BaseException:  # noqa: B036
        return False


_REASON = (b"Blocked by briefcase-ai policy: the hook could not complete and it was installed with --fail-closed or "
           b"a required policy. Reinstall with `briefcase-ai policy init`, or turn enforcement off with "
           b"`briefcase-ai policy off`.")
_DENY = (b'{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", '
         b'"permissionDecisionReason": "' + _REASON + b'"}}\n')
_DENY_CURSOR = (b'{"permission": "deny", "user_message": "' + _REASON + b'", "agent_message": "' + _REASON + b'"}\n')


def _fd_write(fd, data):
    """Write all of ``data`` to a file descriptor; whether it was delivered. No stream object, no import."""
    try:
        while data:
            count = os.write(fd, data)
            if count <= 0:
                return False
            data = data[count:]
        return True
    except BaseException:  # noqa: B036
        return False


def block(tool, write=None):
    """The one delivery routine for a denial, with nothing but fixed bytes: whatever made the hook fail (an import
    included) cannot also stop the denial. The reason goes to stderr with exit 2, which all three tools read as a
    block (Codex needs it nonempty). When stderr cannot carry it, the tool's JSON denial goes to stdout with exit 0;
    only when neither works is the status alone left. ``write(fd, data)`` says whether it delivered (default: a
    file descriptor)."""
    write = write or _fd_write
    if write(2, _REASON + b"\n"):
        return 2
    return 0 if write(1, _DENY_CURSOR if tool == "cursor" else _DENY) else 2


def _leave(code):
    """Exit with ``code`` even when a stream cannot be flushed at shutdown (the interpreter would say 120)."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except BaseException:  # noqa: B036
        os._exit(code)
    return code


def main():
    return _leave(_run())


def load_sibling(name):
    """A module of this directory, loaded by path (no package import, standard library only)."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location("_briefcase_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def enforcing_for(tool, options):
    """:func:`briefcase.policy.optargs.enforcing`, with the same fallback as :func:`strict_for`."""
    try:
        return bool(load_sibling("optargs").enforcing(tool, options))
    except BaseException:  # noqa: B036
        return tool != "cursor" or "--enforce" in options or strict_for(options)


def strict_for(options):
    """Does the command line bind this hook? ``options`` are the arguments after the tool name. The answer of
    ``optargs.normalize``, loaded by path so that it does not depend on anything that can fail to import; if even
    that file cannot be loaded, any mention of a binding option binds."""
    try:
        return bool(load_sibling("optargs").strict(options))
    except BaseException:  # noqa: B036
        return any("--fail-closed" in a or "--require-policy-sha256" in a for a in options)


def block_in_process(tool):
    """:func:`block` through this process's own ``sys.stdout``/``sys.stderr`` objects, which the SDK's command (and its
    tests) may have replaced."""
    delivered = {}

    def write(fd, data):
        stream = sys.stderr if fd == 2 else sys.stdout
        original = sys.__stderr__ if fd == 2 else sys.__stdout__
        if stream is original and stream is not None:
            delivered[fd] = _fd_write(fd, data)
        else:
            delivered[fd] = _say(stream, data.decode("ascii"))
        return delivered[fd]

    code = block(tool, write)
    if code == 2 and sys.stdout is sys.__stdout__ and delivered.get(1) is False:
        os._exit(2)  # the interpreter would retry the failed flush at exit and say 120
    return code


def on_failure(tool, argv):
    """What a hook entry that failed before it could decide does: block when the command line binds the hook,
    otherwise allow (Cursor blocks on empty output, so it is told to allow)."""
    if strict_for(argv):
        return block_in_process(tool)
    if tool == "cursor":
        _say(sys.stdout, '{"permission": "allow"}\n')
    return 0


def _run():
    argv = sys.argv[1:]
    strict = strict_for(argv[1:])
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        package = os.path.dirname(here)
        # The script's own directory must not shadow the standard library.
        sys.path[:] = [entry for entry in sys.path if os.path.abspath(entry or os.curdir) != here]
        if "briefcase" not in sys.modules:
            import types

            stub = types.ModuleType("briefcase")
            stub.__path__ = [package]
            sys.modules["briefcase"] = stub
        from briefcase.policy import hook
    except BaseException:  # noqa: B036
        if strict:
            return block(argv[0] if argv else "")
        if argv[:1] == ["cursor"]:
            _say(sys.stdout, '{"permission": "allow"}\n')   # Cursor blocks on empty output
        return 0
    try:
        return hook.main(argv)
    except BaseException:  # noqa: B036 - a late import or any failure the hook did not catch
        if strict:
            return block(argv[0] if argv else "")
        if argv[:1] == ["cursor"]:
            _say(sys.stdout, '{"permission": "allow"}\n')
        return 0


if __name__ == "__main__":
    sys.exit(main())
