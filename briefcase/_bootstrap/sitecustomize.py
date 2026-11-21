"""Zero-code bootstrap: imported by Python at interpreter start when this
directory is on ``PYTHONPATH`` (``briefcase exec -- <command>`` arranges that).

Does nothing unless ``BRIEFCASE_AUTO=1``. When it is set:

1. ``briefcase.observe()`` picks the destination from the environment
   (``BRIEFCASE_EXPORTER``, the ``OTEL_EXPORTER_OTLP_*`` variables, else the
   console on stderr).
2. ``briefcase.auto(lazy=True)`` instruments every installed framework and
   provider SDK. Each one is patched when the application first imports it,
   so nothing heavy is imported here.

Optional variables:

- ``BRIEFCASE_CAPTURE_CONTENT``: ``none`` (default here), ``hash`` or
  ``full``. Applies to frameworks and providers alike, so a program run
  through ``briefcase exec`` records no prompt or completion text unless
  this is set.
- ``BRIEFCASE_AUTO_SKIP``: comma-separated integration names to leave alone
  (for example ``langchain,bedrock``).

Records are exported on briefcase's background queue, which drains at
interpreter exit (an ``atexit`` hook, up to 5 s), so short-lived programs
keep their records.

A ``sitecustomize`` module that would have been imported without this one
(further along ``sys.path``) is run afterwards. Nothing here raises into
the program: a failure is reported in one line on stderr and the program
runs uninstrumented.
"""

import os
import sys


def _briefcase_report(message):
    try:
        sys.stderr.write("briefcase: %s\n" % (message,))
    except Exception:
        pass


def _briefcase_bootstrap():
    if os.environ.get("BRIEFCASE_AUTO") != "1":
        return
    try:
        try:
            import briefcase
        except ImportError:
            # Run from a source checkout: the package sits two levels up and
            # is not otherwise importable from the child's working directory.
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if root in sys.path:
                raise
            sys.path.append(root)
            import briefcase

        briefcase.observe()

        content = (os.environ.get("BRIEFCASE_CAPTURE_CONTENT") or "none").strip().lower()
        if content not in ("none", "hash", "full"):
            _briefcase_report(
                "ignoring BRIEFCASE_CAPTURE_CONTENT=%r (expected none, hash or full); "
                "content capture stays off" % (content,)
            )
            content = "none"
        skip = [
            name.strip()
            for name in (os.environ.get("BRIEFCASE_AUTO_SKIP") or "").split(",")
            if name.strip()
        ]
        briefcase.auto(lazy=True, capture_content=content, skip=skip)
    except BaseException as exc:  # never break the user's program
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _briefcase_report(
            "auto-instrumentation is disabled for this process (%s: %s)"
            % (type(exc).__name__, exc)
        )


def _briefcase_chain():
    """Run the sitecustomize this one shadows, if there is one."""
    try:
        import importlib.machinery
        import importlib.util

        here = os.path.dirname(os.path.abspath(__file__))
        search = []
        for entry in sys.path:
            try:
                if os.path.abspath(entry or os.getcwd()) == here:
                    continue
            except Exception:
                pass
            search.append(entry)
        spec = importlib.machinery.PathFinder.find_spec("sitecustomize", search)
        if spec is None or spec.loader is None:
            return
        origin = getattr(spec, "origin", None)
        if origin and os.path.abspath(origin) == os.path.abspath(__file__):
            return
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        # Same contract as site.py: report, do not abort startup.
        _briefcase_report(
            "error in the chained sitecustomize (%s: %s)" % (type(exc).__name__, exc)
        )


# Only when Python imported this file as *the* sitecustomize; importing it
# as briefcase._bootstrap.sitecustomize (tooling that walks the package)
# must not run anything.
if __name__ == "sitecustomize":
    _briefcase_bootstrap()
    _briefcase_chain()
