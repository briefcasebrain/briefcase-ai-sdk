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


def main():
    argv = sys.argv[1:]
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
    except BaseException as exc:  # noqa: B036
        if "--fail-closed" in argv:
            sys.stderr.write(
                f"Blocked by briefcase-ai policy: the hook could not start ({type(exc).__name__}: {exc}) and it was "
                "installed with --fail-closed. Reinstall with `briefcase-ai policy init`, or turn enforcement off "
                "with `briefcase-ai policy off`.\n")
            return 2
        if argv[:1] == ["cursor"]:
            sys.stdout.write('{"permission": "allow"}\n')   # Cursor blocks on empty output
        return 0
    return hook.main(argv)


if __name__ == "__main__":
    sys.exit(main())
