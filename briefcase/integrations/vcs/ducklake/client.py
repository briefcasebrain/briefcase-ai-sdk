"""DuckLake client for DuckDB plus lakeFS integration.

Requires ``duckdb``: ``pip install briefcase-ai[vcs-ducklake]`` or
``pip install duckdb``. Without it the constructor raises ``ImportError``.

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

_DUCKDB_INSTALL_HINT = (
    "Install with 'pip install briefcase-ai[vcs-ducklake]' "
    "or 'pip install duckdb' for live mode."
)


class DuckLakeClient(VcsClientBase):
    """
    DuckLake client combining the DuckDB query engine with lakeFS versioning.

    DuckLake integrates DuckDB's columnar query performance with
    lakeFS's data versioning for efficient data lake operations.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Environment variables (DUCKLAKE_ENDPOINT, DUCKLAKE_DB_PATH)
        3. In-memory DuckDB database

    Usage:
        client = DuckLakeClient(
            repository="analytics-lake",
            branch="main",
            briefcase_client=briefcase_client,
            db_path="duckdb.db"
        )
        results = client.read_object("queries/daily_report.parquet")
        client.create_version("Daily analytics refresh")
    """

    def __init__(
        self,
        repository: str,
        branch: str = "main",
        briefcase_client=None,
        endpoint: Optional[str] = None,
        db_path: Optional[str] = None,
        mock: bool = False,
        **extra
    ):
        """
        Initialize DuckLake client.

        Args:
            repository: DuckDB/lakeFS repository name
            branch: lakeFS branch (default: "main")
            briefcase_client: Optional BriefcaseClient for instrumentation
            endpoint: lakeFS endpoint for remote storage
            db_path: Path to DuckDB database file
            mock: Opt into the offline stub (placeholder reads and version ids).
            **extra: Additional DuckLake configuration
        """
        resolved_endpoint = (
            endpoint or
            os.getenv("DUCKLAKE_ENDPOINT") or
            "http://localhost:8000"
        )

        super().__init__(
            provider_type="ducklake",
            repository=repository,
            branch=branch,
            briefcase_client=briefcase_client,
            endpoint=resolved_endpoint,
            mock=mock,
            **extra
        )

        self.db_path = db_path or os.getenv("DUCKLAKE_DB_PATH", ":memory:")

        if self.mock:
            return
        try:
            import duckdb
        except ImportError as e:
            raise ImportError(
                f"DuckLakeClient requires duckdb. {_DUCKDB_INSTALL_HINT} "
                "Pass mock=True for an offline stub."
            ) from e
        self._provider_client = duckdb.connect(self.db_path)
        self._has_provider = True

    def _read_object_impl(self, path: str) -> bytes:
        """Read an object. Placeholder bytes only with ``mock=True``."""
        if self.mock:
            return b"Mock DuckLake result: " + path.encode()
        raise self._not_implemented("read_object")

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """Write an object. A no-op only with ``mock=True``."""
        if self.mock:
            logger.info(f"Mock DuckLake: Would insert {len(data)} bytes to {path}")
            return
        raise self._not_implemented("write_object")

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create a version. A placeholder id only with ``mock=True``."""
        if self.mock:
            return f"ducklake-{self.branch}-v1"
        raise self._not_implemented("create_version")
