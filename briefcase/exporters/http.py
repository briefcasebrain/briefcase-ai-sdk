"""HTTP exporter: POSTs batches of decision records as JSON.

Standard library only (``urllib``); no extra is needed. Records are buffered
and sent as ``{"records": [...]}`` when the buffer reaches ``batch_size`` or
``flush_interval`` seconds have passed, whichever comes first. A batch that
fails with a 5xx, a 429, or a network error is retried with jittered
exponential backoff and, if it still fails, kept for the next flush (up to
``max_buffer`` records). Any other 4xx is not retryable, so the batch is
dropped. Both outcomes are logged at WARNING.

Transport security: the URL must be https. Plain http is accepted only for
loopback hosts, or anywhere with ``allow_insecure=True``; records can carry
decision content and the bearer token would otherwise travel in cleartext.
Redirects are never followed, so a redirect can neither downgrade the
request nor forward the token to another host.

Example:
    import briefcase
    briefcase.observe("https://collector.example.com/v1/decisions")

    # or, with auth and tuning:
    from briefcase.exporters import HTTPExporter
    briefcase.observe(HTTPExporter(url, token="...", batch_size=50))
"""

from __future__ import annotations

import ipaddress
import json
import random
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from briefcase._exit_state import exiting, run_blocking
from briefcase._logging import get_logger
from briefcase.exporters.base import BaseExporter

logger = get_logger(__name__)


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses to follow any redirect."""

    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
        return None


def _post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> Tuple[int, Optional[str]]:
    """POST ``body`` and return ``(status, Retry-After header or None)``.

    HTTP error statuses are returned, not raised; network failures raise.
    """
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    opener = urllib.request.build_opener(_RefuseRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, None
    except urllib.error.HTTPError as err:
        try:
            return err.code, err.headers.get("Retry-After") if err.headers else None
        finally:
            err.close()


def _to_jsonable(decision: Any) -> Any:
    if isinstance(decision, dict):
        return decision
    try:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(decision) and not isinstance(decision, type):
            return asdict(decision)
    except Exception:
        pass
    return getattr(decision, "__dict__", None) or {"repr": repr(decision)}


class HTTPExporter(BaseExporter):
    """POST decision records to an HTTP endpoint in JSON batches.

    Args:
        url: Destination. https, or http to a loopback host; any other http
            URL needs ``allow_insecure=True``.
        token: Bearer token, sent as ``Authorization: Bearer <token>``.
        headers: Extra request headers (override the defaults).
        timeout: Per-request timeout in seconds (default 10).
        batch_size: Send when this many records are buffered (default 100).
        flush_interval: Also send every this many seconds while records are
            buffered (default 5). ``0`` or ``None`` disables the timer; then
            only ``batch_size``, ``flush()`` and ``close()`` send.
        max_retries: Extra attempts for a batch that fails with 5xx, 429,
            or a network error (default 3).
        backoff_base: Delay before the first retry in seconds; doubles per
            attempt, with jitter (default 0.5). A numeric ``Retry-After``
            response header is honoured when larger.
        backoff_max: Upper bound on one retry delay (default 30).
        max_buffer: Most records held in memory, counting batches kept
            after a failed send (default 10000). The oldest are dropped
            past it.
        allow_insecure: Permit plain http to a non-loopback host.

    ``export()`` returns True once the record is buffered (or sent). Failed
    and dropped batches are counted in ``failed_batches`` / ``dropped_records``.
    """

    def __init__(
        self,
        url: str,
        *,
        token: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 10.0,
        batch_size: int = 100,
        flush_interval: Optional[float] = 5.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        max_buffer: int = 10_000,
        allow_insecure: bool = False,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"HTTPExporter needs an http(s) URL, got {url!r}")
        if (
            parsed.scheme == "http"
            and not allow_insecure
            and not _is_loopback_host(parsed.hostname)
        ):
            raise ValueError(
                "HTTPExporter URL uses http with a non-loopback host; records "
                "and any bearer token would be sent in cleartext. Use https, "
                "or pass allow_insecure=True."
            )

        self._url = url
        self._log_url = f"{parsed.scheme}://{parsed.hostname}" + (
            f":{parsed.port}" if parsed.port else ""
        ) + parsed.path
        self._headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "briefcase-ai-http-exporter",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        if headers:
            self._headers.update(headers)
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        self._flush_interval = flush_interval if flush_interval and flush_interval > 0 else None
        self._max_retries = max(0, max_retries)
        self._backoff_base = max(0.0, backoff_base)
        self._backoff_max = max(0.0, backoff_max)
        self._max_buffer = max(self._batch_size, max_buffer)

        self._buffer: List[Any] = []
        self._buffer_lock = threading.Lock()
        # Serialises sends so batches leave in order and a timer flush never
        # overlaps a size-triggered one.
        self._send_lock = threading.Lock()
        self._timer: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._auth_warned = False
        self._had_token = "Authorization" in self._headers

        self.sent_records = 0
        self.failed_batches = 0
        self.dropped_records = 0

    def _after_fork_in_child(self) -> None:
        """Called by the export pipeline in a forked child. The buffer copy
        holds the parent's unsent records (the parent sends those), the
        locks may have been held by a thread that does not exist here, and
        the periodic-flush thread did not survive the fork."""
        self._buffer = []
        self._buffer_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._timer = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Buffering
    # ------------------------------------------------------------------

    def _append(self, record: Any) -> int:
        with self._buffer_lock:
            self._buffer.append(record)
            self._trim_locked()
            return len(self._buffer)

    def _trim_locked(self) -> None:
        overflow = len(self._buffer) - self._max_buffer
        if overflow > 0:
            del self._buffer[:overflow]
            self.dropped_records += overflow
            logger.warning(
                "HTTPExporter buffer full (%d records); dropped the %d oldest. "
                "%d dropped so far.", self._max_buffer, overflow, self.dropped_records,
            )

    def _take_batch(self) -> List[Any]:
        with self._buffer_lock:
            batch = self._buffer[: self._batch_size]
            del self._buffer[: self._batch_size]
            return batch

    def _requeue(self, batch: List[Any]) -> None:
        with self._buffer_lock:
            self._buffer[:0] = batch
            self._trim_locked()

    def _ensure_timer(self) -> None:
        # No new thread once the exit-time drain began: it cannot be joined,
        # and the drain flushes what is buffered itself.
        if self._flush_interval is None or self._timer is not None or exiting():
            return
        with self._buffer_lock:
            if self._timer is not None:
                return
            self._timer = threading.Thread(
                target=self._timer_loop, args=(self._stop,),
                name="briefcase-http-flush", daemon=True,
            )
            self._timer.start()

    def _sleep(self, seconds: float) -> bool:
        """Back off between attempts; True when close() cut the wait short."""
        return self._stop.wait(seconds)

    def _timer_loop(self, stop: threading.Event) -> None:
        interval = self._flush_interval or 0.0
        while not stop.wait(interval):
            try:
                self._flush_sync()
            except Exception:  # pragma: no cover - _flush_sync never raises
                logger.warning("HTTPExporter periodic flush failed", exc_info=True)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _retry_delay(self, attempt: int, retry_after: Optional[str]) -> float:
        ceiling = min(self._backoff_max, self._backoff_base * (2 ** attempt))
        delay = random.uniform(ceiling / 2, ceiling) if ceiling > 0 else 0.0
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), self._backoff_max))
            except ValueError:
                pass  # HTTP-date form: fall back to the computed backoff
        return delay

    def _warn_auth_rejected(self, status: int, count: int) -> None:
        """One WARNING per exporter for a 401/403 (every later batch would
        be rejected the same way; a line each would flood the log)."""
        if self._auth_warned:
            logger.debug("HTTPExporter: %s rejected %d record(s) with HTTP %d (dropped)",
                         self._log_url, count, status)
            return
        self._auth_warned = True
        sent = ("The request carried an Authorization header, so check that the token is valid "
                "for this endpoint." if self._had_token else
                "No bearer token was sent: set BRIEFCASE_EXPORTER_TOKEN (or pass token=...).")
        logger.warning(
            "HTTPExporter: %s answered HTTP %d (authentication failed), so decision records "
            "are being dropped, not delivered; this warning is shown once. %s "
            "BRIEFCASE_EXPORTER_TOKEN is the bearer token briefcase.observe() sends to an "
            "http(s) destination.", self._log_url, status, sent,
        )

    def _send_batch(self, batch: List[Any]) -> str:
        """Send one batch. Returns "sent", "retry" (transient failure after
        all attempts) or "rejected" (non-retryable status)."""
        body = json.dumps({"records": batch}, default=str).encode("utf-8")
        attempts = 1 + self._max_retries
        problem = ""
        for attempt in range(attempts):
            retry_after: Optional[str] = None
            try:
                status, retry_after = _post(self._url, body, self._headers, self._timeout)
            except Exception as exc:
                problem = f"{type(exc).__name__}: {exc}"
            else:
                if 200 <= status < 300:
                    return "sent"
                problem = f"HTTP {status}"
                if status in (401, 403):
                    self._warn_auth_rejected(status, len(batch))
                    return "rejected"
                if status != 429 and status < 500:
                    logger.warning(
                        "HTTPExporter: %s rejected a batch of %d record(s) with %s; "
                        "not retryable, batch dropped",
                        self._log_url, len(batch), problem,
                    )
                    return "rejected"
            if attempt + 1 < attempts:
                if self._sleep(self._retry_delay(attempt, retry_after)) is True:
                    break  # close() was called mid-backoff
        logger.warning(
            "HTTPExporter: sending %d record(s) to %s failed (%s) after %d attempt(s); "
            "keeping them for the next flush",
            len(batch), self._log_url, problem, attempt + 1,
        )
        return "retry"

    def _flush_sync(self) -> bool:
        """Send everything buffered. True when the buffer was emptied."""
        with self._send_lock:
            while True:
                batch = self._take_batch()
                if not batch:
                    return True
                outcome = self._send_batch(batch)
                if outcome == "sent":
                    self.sent_records += len(batch)
                    continue
                self.failed_batches += 1
                if outcome == "rejected":
                    self.dropped_records += len(batch)
                else:
                    # Only dropped once it has actually been delivered.
                    self._requeue(batch)
                return False

    # ------------------------------------------------------------------
    # BaseExporter interface
    # ------------------------------------------------------------------

    async def export(self, decision: Any) -> bool:
        """Buffer a record; sends a batch (off the event loop) when full."""
        size = self._append(_to_jsonable(decision))
        self._ensure_timer()
        if size >= self._batch_size:
            await run_blocking(self._flush_sync)
        return True

    async def flush(self) -> None:
        """Send buffered records now."""
        await run_blocking(self._flush_sync)

    async def close(self) -> None:
        """Stop the flush timer and send what is still buffered. The exporter
        stays usable; a later export restarts the timer."""
        self._stop.set()
        timer, self._timer = self._timer, None
        if timer is not None and timer is not threading.current_thread():
            await run_blocking(timer.join, self._timeout + 1.0)
        # Backoff sleeps return immediately while _stop is set, so this final
        # flush makes one quick pass instead of waiting out retries.
        await run_blocking(self._flush_sync)
        self._stop = threading.Event()

    @property
    def pending(self) -> int:
        """Records buffered and not yet sent."""
        with self._buffer_lock:
            return len(self._buffer)


__all__ = ["HTTPExporter"]
