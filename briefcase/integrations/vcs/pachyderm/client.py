"""Pachyderm client for container-native data versioning.

Requires ``pachyderm-sdk``: ``pip install briefcase-ai[vcs-pachyderm]`` or
``pip install pachyderm-sdk``. Without it the constructor raises ``ImportError``.

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
        mock: bool = False,
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
            mock: Opt into the offline stub (placeholder reads and version ids).
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
            mock=mock,
            **extra
        )

        if self.mock:
            return
        try:
            from pachyderm_sdk import Client as PachClient
        except ImportError as e:
            raise ImportError(
                f"PachydermClient requires pachyderm_sdk. {_PACHYDERM_INSTALL_HINT} "
                "Pass mock=True for an offline stub."
            ) from e
        self._provider_client = PachClient.from_pachd_address(self.endpoint)
        self._has_provider = True

    def _read_object_impl(self, path: str) -> bytes:
        """Read an object. Placeholder bytes only with ``mock=True``."""
        if self.mock:
            return b"Mock Pachyderm content: " + path.encode()
        raise self._not_implemented("read_object")

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """Write an object. A no-op only with ``mock=True``."""
        if self.mock:
            logger.info(f"Mock Pachyderm: Would put {len(data)} bytes to {path}")
            return
        raise self._not_implemented("write_object")

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create a version. A placeholder id only with ``mock=True``."""
        if self.mock:
            return f"pachyderm-{self.branch}-mock-commit"
        raise self._not_implemented("create_version")
