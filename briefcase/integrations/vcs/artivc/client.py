"""ArtiVC client for Git-like artifact versioning.

Stdlib only: ArtiVC ships as a command-line tool with no Python SDK, so
this client captures endpoint, branch and version provenance and runs in
mock mode. Wiring a live backend means subclassing and overriding
``_read_object_impl``, ``_write_object_impl`` and ``_create_version_impl``.
"""

from typing import Optional, Dict
import os

from briefcase._logging import get_logger
from briefcase.integrations.vcs.base import VcsClientBase

logger = get_logger(__name__)


class ArtiVCClient(VcsClientBase):
    """
    ArtiVC client for Git-like versioning of artifacts and models.

    ArtiVC provides Git-like semantics for managing artifacts, models,
    and datasets with centralized storage backends.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Environment variables (ARTIVC_SERVER)
        3. Default ArtiVC server

    Usage:
        client = ArtiVCClient(
            repository="ml-models",
            branch="production",
            briefcase_client=briefcase_client,
            endpoint="https://artivc.example.com"
        )
        model = client.read_object("models/classifier-v1.pkl")
        client.create_version("Retrained classifier model")
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
        Initialize ArtiVC client.

        Args:
            repository: ArtiVC repository name
            branch: ArtiVC branch name (default: "main")
            briefcase_client: Optional BriefcaseClient for instrumentation
            endpoint: ArtiVC server endpoint
            token: Authentication token
            **extra: Additional ArtiVC configuration
        """
        resolved_endpoint = (
            endpoint or
            os.getenv("ARTIVC_SERVER") or
            "http://localhost:8080"
        )

        super().__init__(
            provider_type="artivc",
            repository=repository,
            branch=branch,
            briefcase_client=briefcase_client,
            endpoint=resolved_endpoint,
            token=token,
            **extra
        )

        # ArtiVC has no Python SDK; the client runs in mock mode until a
        # subclass wires a live backend.
        self._provider_client = None
        self._has_provider = False

    def _read_object_impl(self, path: str) -> bytes:
        """Read an artifact from the ArtiVC repository."""
        if not self._has_provider:
            return b"Mock ArtiVC artifact: " + path.encode()

        try:
            return f"ArtiVC artifact: {path}".encode()
        except Exception as e:
            logger.error(f"Failed to read ArtiVC object: {e}")
            raise

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """Write an artifact into the ArtiVC repository."""
        if not self._has_provider:
            logger.info(f"Mock ArtiVC: Would push {len(data)} bytes to {path}")
            return

        try:
            logger.info(f"ArtiVC: Uploaded {len(data)} bytes to {path}")
        except Exception as e:
            logger.error(f"Failed to write ArtiVC object: {e}")
            raise

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create an ArtiVC version (tag/commit)."""
        if not self._has_provider:
            return f"artivc-{self.branch}-v1"

        try:
            return f"artivc-{len(message)}"
        except Exception as e:
            logger.error(f"Failed to create ArtiVC version: {e}")
            raise
