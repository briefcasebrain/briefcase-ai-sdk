"""Iceberg client for Apache Iceberg table versioning.

Requires ``pyiceberg``: ``pip install briefcase-ai[vcs-iceberg]`` or
``pip install pyiceberg``. Without it the constructor raises ``ImportError``.

The object operations (``read_object``, ``write_object``,
``create_version``) have no live implementation yet and raise
``NotImplementedError``; subclass and override the ``_*_impl`` hooks to
wire them. ``mock=True`` opts into an offline stub that returns placeholder
bytes and version ids, tagged ``"mock": True`` in every metadata dict.
"""

from typing import Optional, Dict
import os

from briefcase._logging import get_logger
from briefcase.integrations.vcs.base import VcsClientBase

logger = get_logger(__name__)

_PYICEBERG_INSTALL_HINT = (
    "Install with 'pip install briefcase-ai[vcs-iceberg]' "
    "or 'pip install pyiceberg' for live mode."
)


class IcebergClient(VcsClientBase):
    """
    Iceberg client for Apache Iceberg table format versioning.

    Apache Iceberg provides schema evolution, partition evolution,
    and hidden partitioning for reliable data lakes at scale.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Environment variables (ICEBERG_CATALOG, ICEBERG_WAREHOUSE)
        3. Default Iceberg catalog

    Usage:
        client = IcebergClient(
            repository="data-catalog",
            branch="main",
            briefcase_client=briefcase_client,
            warehouse="/data/warehouse"
        )
        table_data = client.read_object("events.events_table")
        client.create_version("Backfilled missing events")
    """

    def __init__(
        self,
        repository: str,
        branch: str = "main",
        briefcase_client=None,
        endpoint: Optional[str] = None,
        warehouse: Optional[str] = None,
        mock: bool = False,
        **extra
    ):
        """
        Initialize Iceberg client.

        Args:
            repository: Iceberg catalog or warehouse name
            branch: Table branch/tag name (default: "main")
            briefcase_client: Optional BriefcaseClient for instrumentation
            endpoint: Metastore or catalog endpoint
            warehouse: Warehouse path for Iceberg tables
            mock: Opt into the offline stub (placeholder reads and version ids).
            **extra: Additional Iceberg configuration
        """
        resolved_endpoint = (
            endpoint or
            os.getenv("ICEBERG_CATALOG") or
            "file:///tmp/iceberg-warehouse"
        )

        super().__init__(
            provider_type="iceberg",
            repository=repository,
            branch=branch,
            briefcase_client=briefcase_client,
            endpoint=resolved_endpoint,
            mock=mock,
            **extra
        )

        self.warehouse = warehouse or os.getenv("ICEBERG_WAREHOUSE", "/tmp/iceberg-warehouse")

        if self.mock:
            return
        try:
            from pyiceberg.catalog import load_catalog
        except ImportError as e:
            raise ImportError(
                f"IcebergClient requires pyiceberg. {_PYICEBERG_INSTALL_HINT} "
                "Pass mock=True for an offline stub."
            ) from e
        self._provider_client = load_catalog(
            self.repository,
            uri=self.endpoint,
            warehouse=self.warehouse,
        )
        self._has_provider = True

    def _read_object_impl(self, path: str) -> bytes:
        """Read an object. Placeholder bytes only with ``mock=True``."""
        if self.mock:
            return b"Mock Iceberg table: " + path.encode()
        raise self._not_implemented("read_object")

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """Write an object. A no-op only with ``mock=True``."""
        if self.mock:
            logger.info(f"Mock Iceberg: Would insert {len(data)} rows into {path}")
            return
        raise self._not_implemented("write_object")

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create a version. A placeholder id only with ``mock=True``."""
        if self.mock:
            return f"iceberg-snapshot-{self.branch}"
        raise self._not_implemented("create_version")
