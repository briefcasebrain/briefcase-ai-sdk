"""Google Cloud Logging exporter for decision records.

Ships decision records to Google Cloud Logging as structured log entries.
Records are buffered and written in batches; ``close()`` flushes anything
still buffered. A record leaves the buffer only once Cloud Logging accepted
it: when a send fails, the unsent records are put back (up to
``max_buffer``) for the next flush and the failure is logged at WARNING.
The blocking client calls run on a worker thread, never on the event loop.

Install
-------
Requires ``google-cloud-logging``:
``pip install briefcase-ai[gcp-logging]`` or
``pip install google-cloud-logging``. The import is lazy, so this module
loads without the package installed and fails with a clear error on
construction.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from briefcase._exit_state import run_blocking
from briefcase._logging import get_logger
from briefcase.exporters.base import BaseExporter


_GCP_INSTALL_HINT = (
    "google-cloud-logging is required. Install with "
    "'pip install briefcase-ai[gcp-logging]' "
    "or 'pip install google-cloud-logging'."
)

logger = get_logger(__name__)


class GCPCloudLoggingExporter(BaseExporter):
    """Export decision records to Google Cloud Logging as structured entries.

    Args:
        project: GCP project ID.
        log_name: Log name within the project.
        credentials_path: Path to a service account JSON file. Falls back
            to Application Default Credentials when omitted.
        batch_size: Flush when the buffer reaches this size.
        max_buffer: Most records held in memory, counting those kept after
            a failed send (default 10000). The oldest are dropped past it
            and counted in ``dropped_records``.
    """

    def __init__(
        self,
        project: str,
        log_name: str = "briefcase-ai-decisions",
        credentials_path: Optional[str] = None,
        batch_size: int = 100,
        max_buffer: int = 10_000,
    ) -> None:
        self._project = project
        self._log_name = log_name
        self._batch_size = max(1, batch_size)
        self._max_buffer = max(self._batch_size, max_buffer)
        self._buffer: List[Dict[str, Any]] = []
        # Guards the buffer: export() is called from the background export
        # worker, user threads and the event loop alike.
        self._lock = threading.Lock()
        # Serialises sends so two flushes cannot interleave their entries.
        self._send_lock = threading.Lock()
        self.dropped_records = 0

        self._credentials_path = credentials_path
        self._client: Any = None
        self._logger: Any = None
        self._build_client()  # raises ImportError with the install hint when missing

    def _build_client(self) -> None:
        """Create the Cloud Logging client (imports are lazy; see the module
        docstring). Also used to rebuild it in a forked child."""
        try:
            from google.cloud import logging as gcp_logging
        except ImportError as exc:
            raise ImportError(_GCP_INSTALL_HINT) from exc

        if self._credentials_path:
            try:
                from google.oauth2 import service_account as gcp_service_account
            except ImportError as exc:
                raise ImportError(_GCP_INSTALL_HINT) from exc
            credentials = gcp_service_account.Credentials.from_service_account_file(
                self._credentials_path
            )
            self._client = gcp_logging.Client(project=self._project, credentials=credentials)
        else:
            self._client = gcp_logging.Client(project=self._project)
        self._logger = self._client.logger(self._log_name)

    def _after_fork_in_child(self) -> None:
        """Called by the export pipeline in a forked child. The buffer copy
        holds the parent's unsent records (the parent sends those), the
        locks may have been held by a thread that does not exist here, and
        the client's gRPC channel and its threads cannot be used across a
        fork: the child builds its own client on its first send."""
        self._buffer = []
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._client = None
        self._logger = None

    @staticmethod
    def _decision_to_dict(decision: Any) -> Dict[str, Any]:
        if isinstance(decision, dict):
            return decision
        try:
            from dataclasses import asdict, fields

            fields(decision)
            return asdict(decision)
        except TypeError:
            pass
        return getattr(decision, "__dict__", {"repr": repr(decision)})

    def _trim_locked(self) -> None:
        overflow = len(self._buffer) - self._max_buffer
        if overflow > 0:
            del self._buffer[:overflow]
            self.dropped_records += overflow
            logger.warning(
                "GCPCloudLoggingExporter: buffer full (%d records); dropped the "
                "%d oldest. %d dropped so far.",
                self._max_buffer, overflow, self.dropped_records,
            )

    async def export(self, decision: Any) -> bool:
        """Buffer a decision. Auto-flushes when batch_size is reached.

        Returns False when the record could not be buffered or the flush it
        triggered failed (the records stay buffered for the next flush).
        """
        try:
            record = self._decision_to_dict(decision)
            with self._lock:
                self._buffer.append(record)
                self._trim_locked()
                full = len(self._buffer) >= self._batch_size
            if full:
                return await run_blocking(self._flush_sync)
            return True
        except Exception as e:
            logger.warning("GCPCloudLoggingExporter: export error: %s", e, exc_info=True)
            return False

    async def flush(self) -> None:
        """Send buffered records as structured log entries."""
        await run_blocking(self._flush_sync)

    async def close(self) -> None:
        """Flush remaining records."""
        await self.flush()

    def _flush_sync(self) -> bool:
        """Send everything buffered; blocking. True when all of it was
        accepted. Unsent records go back to the front of the buffer."""
        with self._send_lock:
            with self._lock:
                records = list(self._buffer)
                self._buffer.clear()
            if not records:
                return True
            sent = 0
            try:
                for record in records:
                    self._send([record])
                    sent += 1
            except Exception as e:
                unsent = records[sent:]
                with self._lock:
                    self._buffer[:0] = unsent
                    self._trim_locked()
                logger.warning(
                    "GCPCloudLoggingExporter: sending to %s/%s failed after %d of "
                    "%d record(s) (%s: %s); keeping the rest for the next flush",
                    self._project, self._log_name, sent, len(records),
                    type(e).__name__, e,
                )
                return False
            return True

    def _send(self, records: List[Dict[str, Any]]) -> None:
        """Write structured log entries to Google Cloud Logging."""
        if self._logger is None:  # a forked child (callers hold _send_lock)
            self._build_client()
        for record in records:
            self._logger.log_struct(record, severity="INFO")
