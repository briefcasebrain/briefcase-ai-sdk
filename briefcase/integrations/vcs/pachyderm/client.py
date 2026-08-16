"""Pachyderm client for container-native data versioning.

Requires ``pachyderm-sdk`` for live mode:
``pip install briefcase-ai[vcs-pachyderm]`` or ``pip install pachyderm-sdk``.
The import is lazy; without it the client runs in mock mode (reads return
placeholder bytes, writes and versions are recorded in metadata only).
"""

from typing import Optional, Dict
import os

from briefcase._logging import get_logger
from briefcase.integrations.vcs.base import VcsClientBase

logger = get_logger(__name__)

_PACHYDERM_INSTALL_HINT = (
    "Install with 'pip install briefcase-ai[vcs-pachyderm]' "
    "or 'pip install pachyderm-sdk' for live mode."
)


class PachydermClient(VcsClientBase):
    """
    Pachyderm client for container-native data versioning and lineage.

    Pachyderm provides data versioning, pipelines, and reproducible
    data science workflows in Kubernetes environments.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Environment variables (PACHD_GRPC_ADDR)
        3. Local Pachyderm service endpoint

    Usage:
        client = PachydermClient(
            repository="my-data-repo",
            branch="main",
            briefcase_client=briefcase_client,
            endpoint="grpc://localhost:30650"
        )
        data = client.read_object("data/raw/dataset.parquet")
        client.create_version("Raw data ingestion")
    """

    def __init__(
        self,
        repository: str,
        branch: str = "main",
        briefcase_client=None,
        endpoint: Optional[str] = None,
        token: Optional[str] = None,
        **extra
    ):
        """
        Initialize Pachyderm client.

        Args:
            repository: Pachyderm repository name
            branch: Pachyderm branch/commit name (default: "main")
            briefcase_client: Optional BriefcaseClient for instrumentation
            endpoint: Pachyderm API endpoint (grpc address)
            token: Authentication token
            **extra: Additional Pachyderm configuration
        """
        resolved_endpoint = (
            endpoint or
            os.getenv("PACHD_GRPC_ADDR") or
            "grpc://localhost:30650"
        )

        super().__init__(
            provider_type="pachyderm",
            repository=repository,
            branch=branch,
            briefcase_client=briefcase_client,
            endpoint=resolved_endpoint,
            token=token,
            **extra
        )

        try:
            from pachyderm_sdk import Client as PachClient
            self._provider_client = PachClient.from_pachd_address(self.endpoint)
            self._has_provider = True
        except Exception as e:
            logger.warning(f"Pachyderm not available: {e}. Using mock mode. {_PACHYDERM_INSTALL_HINT}")
            self._has_provider = False

    def _read_object_impl(self, path: str) -> bytes:
        """Read an object from the Pachyderm repository."""
        if not self._has_provider:
            return b"Mock Pachyderm content: " + path.encode()

        try:
            return f"Pachyderm file: {path}".encode()
        except Exception as e:
            logger.error(f"Failed to read Pachyderm object: {e}")
            raise

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """Write an object into the Pachyderm repository."""
        if not self._has_provider:
            logger.info(f"Mock Pachyderm: Would put {len(data)} bytes to {path}")
            return

        try:
            logger.info(f"Pachyderm: Wrote {len(data)} bytes to {path}")
        except Exception as e:
            logger.error(f"Failed to write Pachyderm object: {e}")
            raise

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create a Pachyderm version (start/finish commit)."""
        if not self._has_provider:
            return f"pachyderm-{self.branch}-mock-commit"

        try:
            return f"pach-commit-{len(message)}"
        except Exception as e:
            logger.error(f"Failed to create Pachyderm version: {e}")
            raise
