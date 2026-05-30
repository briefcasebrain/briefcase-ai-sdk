"""
Shared ExportMixin for Briefcase decision-record exporters.

Provides a single ``_trigger_export()`` implementation so any object that
produces decision records (the ``@capture`` decorator, integration handlers,
or custom code) can export them through one consistent, error-tolerant path
instead of re-implementing the sync/async + background-thread logic.

Usage in a handler class:

    class MyHandler(ExportMixin):
        def __init__(self, ..., exporter=None, async_capture=True):
            self._exporter = exporter       # ExportMixin reads this
            self.async_capture = async_capture  # ExportMixin reads this

    # Calling _trigger_export(record) will use self._exporter if set,
    # otherwise falls back to BriefcaseConfig.get().exporter.
"""

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)


class ExportMixin:
    """
    Mixin providing _trigger_export() for Briefcase framework handlers.

    Requires the concrete class to define:
      - self._exporter    optional per-instance exporter (may be None)
      - self.async_capture  bool controlling sync vs background export

    The mixin has no __init__, so it is safe to use with multiple inheritance
    (MRO-neutral). All errors are swallowed  _trigger_export never raises.
    """

    _exporter = None  # subclasses set via exporter= constructor arg

    def _resolve_exporter(self):
        """Return the exporter to use: per-instance first, then global config."""
        if self._exporter is not None:
            return self._exporter
        try:
            from briefcase.config import BriefcaseConfig
            return BriefcaseConfig.get().exporter
        except Exception:
            logger.debug("Could not resolve global exporter from config", exc_info=True)
            return None

    def _trigger_export(self, record) -> None:
        """
        Export a decision record via the configured exporter.

        If async_capture is True (default), spawns a daemon thread so the
        caller is never blocked. On any error, silently returns.
        """
        try:
            exporter = self._resolve_exporter()
            if exporter is None:
                return

            if getattr(self, "async_capture", True):
                def _run() -> None:
                    try:
                        result = exporter.export(record)
                        if asyncio.iscoroutine(result):
                            loop = asyncio.new_event_loop()
                            try:
                                loop.run_until_complete(result)
                            finally:
                                loop.close()
                    except Exception:
                        logger.debug("Background export failed", exc_info=True)

                t = threading.Thread(target=_run, daemon=True)
                t.start()
            else:
                result = exporter.export(record)
                if asyncio.iscoroutine(result):
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(result)
                    finally:
                        loop.close()
        except Exception:
            logger.debug("Synchronous export failed", exc_info=True)
