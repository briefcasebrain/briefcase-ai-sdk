"""Google Cloud Logging exporter for decision records.

Ships decision records to Google Cloud Logging as structured log entries.
Records are buffered and written in batches; ``close()`` flushes anything
still buffered.

Install
-------
Requires ``google-cloud-logging``:
``pip install briefcase-ai[gcp-logging]`` or
``pip install google-cloud-logging``. The import is lazy, so this module
loads without the package installed and fails with a clear error on
construction.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from briefcase.exporters.base import BaseExporter


_GCP_INSTALL_HINT = (
    "google-cloud-logging is required. Install with "
    "'pip install briefcase-ai[gcp-logging]' "
    "or 'pip install google-cloud-logging'."
)

logger = logging.getLogger(__name__)


class GCPCloudLoggingExporter(BaseExporter):
    """Export decision records to Google Cloud Logging as structured entries.

    Args:
        project: GCP project ID.
        log_name: Log name within the project.
        credentials_path: Path to a service account JSON file. Falls back
            to Application Default Credentials when omitted.
        batch_size: Flush when the buffer reaches this size.
    """

    def __init__(
        self,
        project: str,
        log_name: str = "briefcase-ai-decisions",
        credentials_path: Optional[str] = None,
        batch_size: int = 100,
    ) -> None:
        try:
            from google.cloud import logging as gcp_logging
        except ImportError as exc:
            raise ImportError(_GCP_INSTALL_HINT) from exc

        self._project = project
        self._log_name = log_name
        self._batch_size = max(1, batch_size)
        self._buffer: List[Dict[str, Any]] = []

        if credentials_path:
            try:
                from google.oauth2 import service_account as gcp_service_account
            except ImportError as exc:
                raise ImportError(_GCP_INSTALL_HINT) from exc
            credentials = gcp_service_account.Credentials.from_service_account_file(
                credentials_path
            )
            self._client = gcp_logging.Client(project=project, credentials=credentials)
        else:
            self._client = gcp_logging.Client(project=project)

        self._logger = self._client.logger(log_name)

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

    async def export(self, decision: Any) -> bool:
        """Buffer a decision. Auto-flushes when batch_size is reached."""
        try:
            record = self._decision_to_dict(decision)
            self._buffer.append(record)
            if len(self._buffer) >= self._batch_size:
                await self.flush()
            return True
        except Exception as e:
            logger.debug("GCPCloudLoggingExporter: export error: %s", e)
            return False

    async def flush(self) -> None:
        """Send buffered records as structured log entries."""
        if not self._buffer:
            return
        records = list(self._buffer)
        self._buffer.clear()
        self._send(records)

    async def close(self) -> None:
        """Flush remaining records."""
        await self.flush()

    def _send(self, records: List[Dict[str, Any]]) -> None:
        """Write structured log entries to Google Cloud Logging."""
        for record in records:
            self._logger.log_struct(record, severity="INFO")
