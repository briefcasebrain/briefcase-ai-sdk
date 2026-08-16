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
import queue
from briefcase._logging import get_logger
import threading

logger = get_logger(__name__)

# Ceiling on how long a synchronous export may hold the caller's thread when
# that thread is already running an event loop. Past it the record is dropped
# rather than freezing the loop behind a slow or hung exporter.
SYNC_EXPORT_TIMEOUT_SECONDS = 5.0

# Background exports run on one shared daemon worker with a queue, started
# lazily and restarted if it ever dies. One thread instead of one per record
# keeps the enqueue path in the low microseconds; the queue is unbounded, so
# a hung exporter accumulates queued records the same way per-record threads
# would accumulate hung threads. Records queued at interpreter exit are lost
# either way; call wait_for_pending_exports() first when that matters.
_worker_state_lock = threading.Lock()
_export_queue: "queue.SimpleQueue | None" = None
_export_worker: "threading.Thread | None" = None


def _worker_loop(q: "queue.SimpleQueue") -> None:
    loop = asyncio.new_event_loop()
    try:
        while True:
            exporter, record, done = q.get()
            try:
                if exporter is not None:
                    result = exporter.export(record)
                    if asyncio.iscoroutine(result):
                        loop.run_until_complete(result)
            except Exception:
                logger.debug("Background export failed", exc_info=True)
            finally:
                if done is not None:
                    done.set()
    finally:  # pragma: no cover - daemon thread dies with the process
        loop.close()


def _enqueue_export(exporter, record, done=None) -> None:
    global _export_queue, _export_worker
    with _worker_state_lock:
        q = _export_queue
        if _export_worker is None or not _export_worker.is_alive() or q is None:
            q = queue.SimpleQueue()
            _export_queue = q
            _export_worker = threading.Thread(
                target=_worker_loop,
                args=(q,),
                name="briefcase-export",
                daemon=True,
            )
            _export_worker.start()
    q.put((exporter, record, done))


def wait_for_pending_exports(timeout: float = 5.0) -> bool:
    """Block until background exports enqueued so far have run.

    Returns True when the queue drained within ``timeout`` (or no background
    worker exists), False on timeout. Useful before process exit and in
    tests; the FIFO queue guarantees everything enqueued earlier has been
    handed to its exporter once the sentinel lands.
    """
    with _worker_state_lock:
        worker = _export_worker
        q = _export_queue
    if worker is None or not worker.is_alive() or q is None:
        return True
    done = threading.Event()
    q.put((None, None, done))
    return done.wait(timeout)


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

    def _trigger_export(self, record, exporter=None) -> None:
        """
        Export a decision record via the configured exporter.

        A caller that already resolved the exporter passes it to skip a
        second resolution. If async_capture is True (default), the record is
        enqueued to the shared background worker so the caller is never
        blocked. On any error, silently returns.
        """
        try:
            if exporter is None:
                exporter = self._resolve_exporter()
            if exporter is None:
                return

            if getattr(self, "async_capture", True):
                _enqueue_export(exporter, record)
            else:
                result = exporter.export(record)
                if asyncio.iscoroutine(result):
                    self._run_coroutine_sync(result)
        except Exception:
            logger.warning("Export failed, decision record dropped", exc_info=True)

    @staticmethod
    def _run_coroutine_sync(coro) -> None:
        """Run an export coroutine to completion before returning. When the
        calling thread already has a running event loop, the coroutine runs on
        a short-lived helper thread with its own loop and is joined for at most
        SYNC_EXPORT_TIMEOUT_SECONDS, so a slow exporter cannot stall the loop.
        Past the timeout the thread is left running, so the export still
        completes unless the process exits first."""
        def _run() -> None:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(coro)
            finally:
                loop.close()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            _run()
            return

        error = []

        def _guarded() -> None:
            try:
                _run()
            except BaseException as exc:
                error.append(exc)

        t = threading.Thread(target=_guarded, daemon=True)
        t.start()
        t.join(timeout=SYNC_EXPORT_TIMEOUT_SECONDS)
        if t.is_alive():
            logger.warning(
                "Synchronous export exceeded %.1fs while an event loop was running; "
                "no longer waiting. The export continues on a background daemon "
                "thread and is lost only if the process exits first. Use "
                "async_capture=True or a faster exporter.",
                SYNC_EXPORT_TIMEOUT_SECONDS,
            )
            return
        if error:
            raise error[0]
