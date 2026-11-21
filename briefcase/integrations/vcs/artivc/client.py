"""ArtiVC client for Git-like artifact versioning.

Stdlib only: ArtiVC ships as a command-line tool with no Python SDK, so
this client captures endpoint and branch configuration only. The object
operations raise ``NotImplementedError`` until a subclass overrides
``_read_object_impl``, ``_write_object_impl`` and ``_create_version_impl``.
``mock=True`` opts into an offline stub that returns placeholder bytes and
version ids, tagged ``"mock": True`` in every metadata dict.
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
        mock: bool = False,
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
            mock: Opt into the offline stub (placeholder reads and version ids).
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
            mock=mock,
            **extra
        )

        # ArtiVC has no Python SDK: without mock=True every operation raises
        # NotImplementedError until a subclass wires a live backend.
        self._provider_client = None
        self._has_provider = False

    def _read_object_impl(self, path: str) -> bytes:
        """Read an object. Placeholder bytes only with ``mock=True``."""
        if self.mock:
            return b"Mock ArtiVC artifact: " + path.encode()
        raise self._not_implemented("read_object")

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """Write an object. A no-op only with ``mock=True``."""
        if self.mock:
            logger.info(f"Mock ArtiVC: Would push {len(data)} bytes to {path}")
            return
        raise self._not_implemented("write_object")

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create a version. A placeholder id only with ``mock=True``."""
        if self.mock:
            return f"artivc-{self.branch}-v1"
        raise self._not_implemented("create_version")
