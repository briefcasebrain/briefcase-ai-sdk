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
import atexit
import os
import queue
import threading
import time
import weakref
from typing import Any, Callable, Dict, Optional

from briefcase._logging import get_logger

logger = get_logger(__name__)

# Ceiling on how long a synchronous export may hold the caller's thread when
# that thread is already running an event loop. Past it the record is dropped
# rather than freezing the loop behind a slow or hung exporter.
SYNC_EXPORT_TIMEOUT_SECONDS = 5.0

# Defaults for the background pipeline; change them with configure_export().
DEFAULT_MAX_QUEUE_SIZE = 10_000
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.1
MAX_RETRY_BACKOFF_SECONDS = 5.0
DEFAULT_EXPORT_TIMEOUT_SECONDS = 30.0
# How long the atexit hook waits for queued records and exporter flushes.
ATEXIT_FLUSH_TIMEOUT_SECONDS = 5.0
# Minimum spacing between repeated "dropped" / "failed" warnings.
WARNING_INTERVAL_SECONDS = 5.0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r", name, raw)
        return default


class _Settings:
    def __init__(self) -> None:
        self.max_queue_size = _env_int("BRIEFCASE_EXPORT_QUEUE_SIZE", DEFAULT_MAX_QUEUE_SIZE)
        self.max_retries = DEFAULT_MAX_RETRIES
        self.retry_backoff_seconds = DEFAULT_RETRY_BACKOFF_SECONDS
        self.export_timeout_seconds: Optional[float] = DEFAULT_EXPORT_TIMEOUT_SECONDS


class _Stats:
    """Pipeline counters. Guarded by a lock: the worker and callers both write."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.exported = 0
        self.failed = 0
        self.dropped = 0

    def add(self, name: str) -> int:
        with self._lock:
            value = getattr(self, name) + 1
            setattr(self, name, value)
            return value


class _RateLimitedWarning:
    """Emit a WARNING at most once per interval, reporting how many
    occurrences were suppressed in between."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = float("-inf")
        self._suppressed = 0

    def warn(self, message: str, *args: Any, exc_info: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last < WARNING_INTERVAL_SECONDS:
                self._suppressed += 1
                return
            suppressed, self._suppressed = self._suppressed, 0
            self._last = now
        if suppressed:
            message += " (%d similar warnings suppressed)"
            args = args + (suppressed,)
        logger.warning(message, *args, exc_info=exc_info)

    def reset(self) -> None:
        with self._lock:
            self._last = float("-inf")
            self._suppressed = 0


_settings = _Settings()
_stats = _Stats()
_drop_warning = _RateLimitedWarning()
_failure_warning = _RateLimitedWarning()

# Background exports run on one shared daemon worker with a bounded queue,
# started lazily and restarted if it ever dies. One thread instead of one per
# record keeps the enqueue path in the low microseconds. When the queue is
# full (a slow or hung exporter) new records are dropped, counted, and
# reported at WARNING rather than growing memory without bound. An atexit
# hook drains the queue for up to ATEXIT_FLUSH_TIMEOUT_SECONDS; call
# briefcase.shutdown() yourself when a longer wait matters.
_worker_state_lock = threading.Lock()
_export_queue: "Optional[queue.Queue]" = None
_export_worker: "Optional[threading.Thread]" = None
_atexit_registered = False
# Exporters that received at least one record, so flush()/shutdown() can
# reach per-handler exporters and not only the global one. Weak: an exporter
# nobody else holds needs no flushing.
_seen_exporters: "weakref.WeakSet[Any]" = weakref.WeakSet()


def configure_export(
    *,
    max_queue_size: Optional[int] = None,
    max_retries: Optional[int] = None,
    retry_backoff_seconds: Optional[float] = None,
    export_timeout_seconds: Optional[float] = None,
) -> None:
    """Tune the background export pipeline. Omitted arguments are unchanged.

    Args:
        max_queue_size: Records that may wait for the background worker
            (default 10000, or ``BRIEFCASE_EXPORT_QUEUE_SIZE``). When the
            queue is full, new records are dropped and counted; see
            :func:`export_stats`. ``0`` means unbounded.
        max_retries: Extra attempts after an exporter raises (default 2).
            Applies to background exports only; a synchronous export
            (``async_capture=False``) is attempted once.
        retry_backoff_seconds: Delay before the first retry; doubles per
            attempt up to 5 s (default 0.1).
        export_timeout_seconds: Ceiling on one awaitable export on the
            worker (default 30). ``0`` disables the ceiling. A timed-out
            export is not retried.
    """
    if max_queue_size is not None:
        if max_queue_size < 0:
            raise ValueError("max_queue_size must be >= 0")
        _settings.max_queue_size = max_queue_size
        with _worker_state_lock:
            q = _export_queue
        if q is not None:
            with q.mutex:
                q.maxsize = max_queue_size
                q.not_full.notify_all()
    if max_retries is not None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        _settings.max_retries = max_retries
    if retry_backoff_seconds is not None:
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be >= 0")
        _settings.retry_backoff_seconds = retry_backoff_seconds
    if export_timeout_seconds is not None:
        if export_timeout_seconds < 0:
            raise ValueError("export_timeout_seconds must be >= 0")
        _settings.export_timeout_seconds = export_timeout_seconds or None


def export_stats() -> Dict[str, int]:
    """Counters for the export pipeline since process start.

    ``exported``: background exports that completed. ``failed``: exports
    that raised (after retries), timed out, or returned ``False``.
    ``dropped``: records discarded because the queue was full. ``queued``:
    records waiting right now. ``max_queue_size``: the current bound.
    """
    with _worker_state_lock:
        q = _export_queue
    return {
        "exported": _stats.exported,
        "failed": _stats.failed,
        "dropped": _stats.dropped,
        "queued": q.qsize() if q is not None else 0,
        "max_queue_size": _settings.max_queue_size,
    }


def dropped_export_count() -> int:
    """Number of records dropped because the export queue was full."""
    return _stats.dropped


def _export_with_retry(loop: asyncio.AbstractEventLoop, exporter: Any, record: Any) -> None:
    """Run one export on the worker, retrying when the exporter raises."""
    attempts = 1 + max(0, _settings.max_retries)
    for attempt in range(attempts):
        try:
            result = exporter.export(record)
            if asyncio.iscoroutine(result):
                timeout = _settings.export_timeout_seconds
                if timeout:
                    result = asyncio.wait_for(result, timeout)
                result = loop.run_until_complete(result)
        except asyncio.TimeoutError:
            _stats.add("failed")
            _failure_warning.warn(
                "Background export to %s timed out after %.1fs; decision record dropped",
                type(exporter).__name__, _settings.export_timeout_seconds or 0.0,
            )
            return
        except Exception:
            if attempt + 1 < attempts:
                delay = min(
                    _settings.retry_backoff_seconds * (2 ** attempt),
                    MAX_RETRY_BACKOFF_SECONDS,
                )
                logger.debug(
                    "Background export failed (attempt %d/%d); retrying in %.2fs",
                    attempt + 1, attempts, delay, exc_info=True,
                )
                if delay > 0:
                    time.sleep(delay)
                continue
            _stats.add("failed")
            _failure_warning.warn(
                "Background export to %s failed after %d attempt(s); decision record dropped",
                type(exporter).__name__, attempts, exc_info=True,
            )
            return
        if result is False:
            _stats.add("failed")
            _failure_warning.warn(
                "Exporter %s reported failure (returned False); decision record dropped",
                type(exporter).__name__,
            )
        else:
            _stats.add("exported")
        return


def _worker_loop(q: "queue.Queue") -> None:
    loop = asyncio.new_event_loop()
    try:
        while True:
            exporter, record, done = q.get()
            try:
                if exporter is not None:
                    _export_with_retry(loop, exporter, record)
            except Exception:  # pragma: no cover - _export_with_retry never raises
                logger.warning("Background export failed", exc_info=True)
            finally:
                if done is not None:
                    done.set()
    finally:  # pragma: no cover - daemon thread dies with the process
        loop.close()


def _ensure_worker() -> "queue.Queue":
    global _export_queue, _export_worker, _atexit_registered
    with _worker_state_lock:
        q = _export_queue
        if _export_worker is None or not _export_worker.is_alive() or q is None:
            q = queue.Queue(maxsize=_settings.max_queue_size)
            _export_queue = q
            _export_worker = threading.Thread(
                target=_worker_loop,
                args=(q,),
                name="briefcase-export",
                daemon=True,
            )
            _export_worker.start()
        if not _atexit_registered:
            atexit.register(_atexit_flush)
            _atexit_registered = True
    return q


def _remember_exporter(exporter: Any) -> None:
    try:
        _seen_exporters.add(exporter)
    except TypeError:  # not weak-referenceable (e.g. __slots__ without __weakref__)
        pass


def _enqueue_export(exporter, record, done=None) -> bool:
    """Hand a record to the background worker. Returns False when the queue
    is full and the record was dropped."""
    q = _ensure_worker()
    _remember_exporter(exporter)
    try:
        q.put_nowait((exporter, record, done))
    except queue.Full:
        dropped = _stats.add("dropped")
        _drop_warning.warn(
            "Export queue full (%d records); dropping decision record. "
            "%d dropped so far. The exporter is slower than capture; raise "
            "the bound with briefcase.configure_export(max_queue_size=...) "
            "or use a faster exporter.",
            q.maxsize, dropped,
        )
        if done is not None:
            done.set()
        return False
    return True


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
    deadline = time.monotonic() + timeout
    done = threading.Event()
    try:
        # A full queue is waited on, never dropped: the sentinel must land.
        q.put((None, None, done), timeout=max(0.0, timeout))
    except queue.Full:
        return False
    return done.wait(max(0.0, deadline - time.monotonic()))


def _run_with_deadline(fn: Callable[[], Any], timeout: float) -> bool:
    """Run ``fn`` (sync, or returning a coroutine) on a helper thread and wait
    at most ``timeout`` seconds. True when it finished without raising."""
    ok = []

    def _run() -> None:
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(result)
                finally:
                    loop.close()
            ok.append(True)
        except Exception:
            logger.warning("Exporter flush/close failed", exc_info=True)

    t = threading.Thread(target=_run, name="briefcase-flush", daemon=True)
    t.start()
    t.join(timeout=max(0.0, timeout))
    return bool(ok)


def _known_exporters() -> list:
    exporters = list(_seen_exporters)
    try:
        from briefcase.config import BriefcaseConfig

        configured = BriefcaseConfig.get().exporter
    except Exception:
        configured = None
    if configured is not None and not any(e is configured for e in exporters):
        exporters.append(configured)
    return exporters


def _drain(timeout: float, method: str) -> bool:
    deadline = time.monotonic() + timeout
    ok = wait_for_pending_exports(timeout)
    for exporter in _known_exporters():
        fn = getattr(exporter, method, None)
        if not callable(fn):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        ok = _run_with_deadline(fn, remaining) and ok
    return ok


def flush(timeout: float = 5.0) -> bool:
    """Drain queued background exports, then ``flush()`` every exporter that
    has received records (and the globally configured one).

    Returns True when everything finished within ``timeout`` seconds.
    """
    return _drain(timeout, "flush")


def shutdown(timeout: float = 5.0) -> bool:
    """Drain queued background exports, then ``close()`` every exporter that
    has received records (and the globally configured one).

    Call before process exit when records must not be lost. An atexit hook
    already does this with a 5 s budget; call it yourself for a longer wait
    or an earlier, deterministic drain. Returns True when everything
    finished within ``timeout`` seconds. Capture keeps working afterwards
    for exporters that reopen on demand.
    """
    return _drain(timeout, "close")


def _atexit_flush() -> None:
    try:
        if not shutdown(ATEXIT_FLUSH_TIMEOUT_SECONDS):
            logger.warning(
                "Export queue did not drain within %.1fs at interpreter exit; "
                "some decision records may be lost. Call briefcase.shutdown("
                "timeout=...) before exiting.",
                ATEXIT_FLUSH_TIMEOUT_SECONDS,
            )
    except Exception:  # pragma: no cover - never raise during interpreter exit
        pass


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
        blocked; if that worker's bounded queue is full the record is
        dropped and counted (see export_stats()). Never raises: a failed
        export, or an exporter returning False, is logged at WARNING.
        """
        try:
            if exporter is None:
                exporter = self._resolve_exporter()
            if exporter is None:
                return

            if getattr(self, "async_capture", True):
                _enqueue_export(exporter, record)
            else:
                _remember_exporter(exporter)
                result = exporter.export(record)
                if asyncio.iscoroutine(result):
                    result = self._run_coroutine_sync(result)
                if result is False:
                    _stats.add("failed")
                    logger.warning(
                        "Exporter %s reported failure (returned False); "
                        "decision record dropped", type(exporter).__name__,
                    )
        except Exception:
            _stats.add("failed")
            logger.warning("Export failed, decision record dropped", exc_info=True)

    @staticmethod
    def _run_coroutine_sync(coro) -> Any:
        """Run an export coroutine to completion and return its result (None
        when the wait timed out). When the
        calling thread already has a running event loop, the coroutine runs on
        a short-lived helper thread with its own loop and is joined for at most
        SYNC_EXPORT_TIMEOUT_SECONDS, so a slow exporter cannot stall the loop.
        Past the timeout the thread is left running, so the export still
        completes unless the process exits first."""
        def _run() -> Any:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _run()

        error = []
        value = []

        def _guarded() -> None:
            try:
                value.append(_run())
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
            return None
        if error:
            raise error[0]
        return value[0] if value else None
