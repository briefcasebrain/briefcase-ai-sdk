"""Nessie client for Apache Iceberg catalog versioning.

Stdlib only: this client captures Nessie endpoint, branch and version
provenance and runs in mock mode. Nessie speaks a plain REST API; wiring
a live server means subclassing and overriding ``_read_object_impl``,
``_write_object_impl`` and ``_create_version_impl`` with HTTP calls
against the configured endpoint.
"""

from typing import Optional, Dict
import os

from briefcase._logging import get_logger
from briefcase.integrations.vcs.base import VcsClientBase

logger = get_logger(__name__)


class NessieClient(VcsClientBase):
    """
    Nessie client for Apache Iceberg metadata versioning.

    Nessie is a metadata versioning system for Apache Iceberg tables,
    enabling Git-like semantics for data lake operations.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Environment variables (NESSIE_ENDPOINT)
        3. Default Nessie server endpoint

    Usage:
        client = NessieClient(
            repository="my-iceberg-catalog",
            branch="main",
            briefcase_client=briefcase_client,
            endpoint="https://nessie.example.com/api/v1"
        )
        client.create_version("Snapshot of training tables")
        tables = client.read_object("catalog.json")
    """

    def __init__(
        self,
        repository: str,
        branch: str = "main",
        briefcase_client=None,
        endpoint: Optional[str] = None,
        token: Optional[str] = None,
        api_version: str = "v1",
        **extra
    ):
        """
        Initialize Nessie client.

        Args:
            repository: Nessie catalog/warehouse name
            branch: Nessie branch name (default: "main")
            briefcase_client: Optional BriefcaseClient for instrumentation
            endpoint: Nessie API endpoint
            token: Authentication token
            api_version: Nessie API version (default: "v1")
            **extra: Additional Nessie configuration
        """
        resolved_endpoint = (
            endpoint or
            os.getenv("NESSIE_ENDPOINT") or
            "http://localhost:19120/api/v1"
        )

        super().__init__(
            provider_type="nessie",
            repository=repository,
            branch=branch,
            briefcase_client=briefcase_client,
            endpoint=resolved_endpoint,
            token=token,
            **extra
        )

        self.api_version = api_version

        # No bundled Nessie client: provenance bookkeeping works without
        # one, so the client runs in mock mode until a subclass wires the
        # REST API.
        self._has_provider = False

    def _read_object_impl(self, path: str) -> bytes:
        """Read catalog metadata for a path."""
        if not self._has_provider:
            return b"Mock Nessie catalog: " + path.encode()

        try:
            return f"Nessie catalog metadata for {path}".encode()
        except Exception as e:
            logger.error(f"Failed to read Nessie object: {e}")
            raise

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """Write object metadata into the Nessie catalog."""
        if not self._has_provider:
            logger.info(f"Mock Nessie: Would update {path} with {len(data)} bytes")
            return

        try:
            logger.info(f"Nessie: Updated {path} ({len(data)} bytes)")
        except Exception as e:
            logger.error(f"Failed to write Nessie object: {e}")
            raise

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create a Nessie version (commit to branch)."""
        if not self._has_provider:
            return f"nessie-{self.branch}-mock-sha"

        try:
            return f"nessie-commit-{len(message)}"
        except Exception as e:
            logger.error(f"Failed to create Nessie version: {e}")
            raise
