"""Zero-code bootstrap: imported by Python at interpreter start when this
directory is on ``PYTHONPATH`` (``briefcase-ai exec -- <command>`` arranges that).

Does nothing unless ``BRIEFCASE_AUTO=1``. When it is set:

1. The destination is resolved from the environment
   (``BRIEFCASE_EXPORTER``, the ``OTEL_EXPORTER_OTLP_*`` variables, else the
   console on stderr), strictly: a value that cannot be honoured (a typo,
   a malformed endpoint, ``otlp`` without the OpenTelemetry packages)
   disables instrumentation for the process with one line on stderr. It
   never quietly falls back to another destination.
2. ``briefcase.auto(lazy=True)`` instruments every installed framework and
   provider SDK. Each one is patched when the application first imports it,
   so nothing heavy is imported here.
3. briefcase's WARNING messages (an SDK whose layout does not match, a
   full export queue) are shown on stderr, unless ``BRIEFCASE_LOG_LEVEL``
   already configures logging.

Optional variables:

- ``BRIEFCASE_CAPTURE_CONTENT``: ``none`` (default here), ``hash`` or
  ``full``. Applies to frameworks and providers alike, so a program run
  through ``briefcase-ai exec`` records no prompt or completion text unless
  this is set.
- ``BRIEFCASE_AUTO_SKIP``: comma-separated integration names to leave alone
  (for example ``langchain,bedrock``). Unknown names are reported.
- ``BRIEFCASE_AUTO_DEDUPE``: ``none`` (default), ``provider`` or
  ``framework`` (see ``briefcase.auto``).

Child processes: the two variables above are inherited, so an interpreter
the program starts (``subprocess``, ``multiprocessing`` *spawn*) runs this
file itself and is instrumented once. A *forked* child (``os.fork``,
gunicorn, ``multiprocessing`` *fork*) inherits the patches already applied,
again once. Export in a forked child is handled by the export pipeline
itself, for every process and not only under ``briefcase-ai exec`` (see the
at-fork hooks in ``briefcase._export_mixin``): locks are recreated, the
worker thread is restarted on the child's first record, and
``multiprocessing`` fork children (which leave through ``os._exit``) drain
their queue on the way out.

Records are exported on briefcase's background queue, which drains at
interpreter exit (an ``atexit`` hook, up to 5 s), so short-lived programs
keep their records.

A ``sitecustomize`` module that would have been imported without this one
(further along ``sys.path``) is run afterwards. Nothing here raises into
the program: a failure is reported in one line on stderr and the program
runs uninstrumented. If briefcase cannot be imported by this interpreter
(another Python version, another virtualenv) that is all that happens:
nothing is added to its ``sys.path``.
"""

import os
import sys


def _briefcase_report(message):
    try:
        sys.stderr.write("briefcase: %s\n" % (message,))
        sys.stderr.flush()
    except Exception:
        pass


def _briefcase_import():
    """``import briefcase``, also from a source checkout that is not
    installed. The checkout root is put on ``sys.path`` only while the
    import runs: left there, it would make every other top-level package in
    that directory importable by the program. For an installed briefcase
    the directory two levels up is ``site-packages``, possibly of another
    interpreter, and is never added."""
    try:
        import briefcase
        return briefcase
    except ImportError:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if root in sys.path or not os.path.isfile(os.path.join(root, "pyproject.toml")):
            raise
        sys.path.insert(0, root)
        try:
            import briefcase
            return briefcase
        finally:
            try:
                sys.path.remove(root)
            except ValueError:
                pass


def _briefcase_bootstrap():
    if os.environ.get("BRIEFCASE_AUTO") != "1":
        return
    try:
        briefcase = _briefcase_import()
        from briefcase._observe import _exporter_from_env

        if not (os.environ.get("BRIEFCASE_LOG_LEVEL") or "").strip():
            # Make "this call path is not recorded" visible; the library is
            # silent by default.
            briefcase.enable_logging("WARNING")
        briefcase.observe(_exporter_from_env(strict=True))

        content = (os.environ.get("BRIEFCASE_CAPTURE_CONTENT") or "none").strip().lower()
        if content not in ("none", "hash", "full"):
            _briefcase_report(
                "ignoring BRIEFCASE_CAPTURE_CONTENT=%r (expected none, hash or full); "
                "content capture stays off" % (content,)
            )
            content = "none"
        dedupe = (os.environ.get("BRIEFCASE_AUTO_DEDUPE") or "").strip().lower() or None
        if dedupe not in (None, "none", "provider", "framework"):
            _briefcase_report(
                "ignoring BRIEFCASE_AUTO_DEDUPE=%r (expected none, provider or framework)"
                % (dedupe,)
            )
            dedupe = None
        from briefcase.auto import _SUPPORTED

        skip = []
        for name in (os.environ.get("BRIEFCASE_AUTO_SKIP") or "").split(","):
            name = name.strip()
            if not name:
                continue
            if name not in _SUPPORTED:
                _briefcase_report(
                    "ignoring unknown name %r in BRIEFCASE_AUTO_SKIP (known: %s)"
                    % (name, ", ".join(sorted(_SUPPORTED)))
                )
                continue
            skip.append(name)
        # Forked workers (gunicorn, Celery prefork, multiprocessing) are
        # handled by the export pipeline itself (briefcase._export_mixin).
        briefcase.auto(lazy=True, capture_content=content, skip=skip, dedupe=dedupe)
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

        here = os.path.dirname(os.path.realpath(__file__))
        search = []
        for entry in sys.path:
            try:
                if os.path.realpath(entry or os.getcwd()) == here:
                    continue
            except Exception:
                pass
            search.append(entry)
        spec = importlib.machinery.PathFinder.find_spec("sitecustomize", search)
        if spec is None or spec.loader is None:
            return
        origin = getattr(spec, "origin", None)
        if origin and os.path.realpath(origin) == os.path.realpath(__file__):
            return
        module = importlib.util.module_from_spec(spec)
        # From here on ``import sitecustomize`` gives the environment's own
        # module, as it would without this bootstrap.
        previous = sys.modules.get("sitecustomize")
        sys.modules["sitecustomize"] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            if previous is not None:
                sys.modules["sitecustomize"] = previous
            raise
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
