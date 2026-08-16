"""DVC client for data versioning and artifact tracking.

Requires ``dvc`` for live mode: ``pip install briefcase-ai[vcs-dvc]`` or
``pip install dvc``. The import is lazy; without it the client runs in
mock mode (reads return placeholder bytes, writes and versions are
recorded in metadata only).
"""

from typing import Optional, Dict
import os

from briefcase._logging import get_logger
from briefcase.integrations.vcs.base import VcsClientBase

logger = get_logger(__name__)

_DVC_INSTALL_HINT = (
    "Install with 'pip install briefcase-ai[vcs-dvc]' or 'pip install dvc' for live mode."
)


class DvcClient(VcsClientBase):
    """
    DVC (Data Version Control) client for managing versioned data.

    DVC versions data with Git and supports remote storage backends for
    large files and datasets. Reads and writes go through the local
    repository working tree; versions are Git commits.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Environment variables (DVC_REMOTE, DVC_REPO_PATH)
        3. Current directory

    Usage:
        client = DvcClient(
            repository="my-dvc-repo",
            branch="main",
            briefcase_client=briefcase_client,
            repo_path="/path/to/repo"
        )
        data = client.read_object("data/train.csv")
        client.create_version("Updated training dataset")
    """

    def __init__(
        self,
        repository: str,
        branch: str = "main",
        briefcase_client=None,
        repo_path: Optional[str] = None,
        remote: Optional[str] = None,
        **extra
    ):
        """
        Initialize DVC client.

        Args:
            repository: Repository name
            branch: Git branch name (default: "main")
            briefcase_client: Optional BriefcaseClient for instrumentation
            repo_path: Path to DVC repository root
            remote: DVC remote name or URL
            **extra: Additional DVC configuration options
        """
        super().__init__(
            provider_type="dvc",
            repository=repository,
            branch=branch,
            briefcase_client=briefcase_client,
            endpoint=remote,
            **extra
        )

        self.repo_path = repo_path or os.getenv("DVC_REPO_PATH", ".")
        self.remote = remote or os.getenv("DVC_REMOTE")

        # The DVC repository handle validates repo_path; without it the
        # client stays in mock mode.
        try:
            import dvc.repo
            self._provider_client = dvc.repo.Repo(self.repo_path)
            self._has_provider = True
        except Exception as e:
            logger.warning(f"DVC not available: {e}. Using mock mode. {_DVC_INSTALL_HINT}")
            self._has_provider = False

    def _read_object_impl(self, path: str) -> bytes:
        """Read a DVC-tracked file from the working tree."""
        if not self._has_provider:
            return b"Mock DVC content: " + path.encode()

        try:
            full_path = f"{self.repo_path}/{path}"
            with open(full_path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"DVC object not found: {path}")

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """Write a DVC-tracked file into the working tree."""
        if not self._has_provider:
            logger.info(f"Mock DVC: Would write {len(data)} bytes to {path}")
            return

        try:
            full_path = f"{self.repo_path}/{path}"
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(data)
            logger.info(f"Wrote {len(data)} bytes to {path}")
        except Exception as e:
            logger.error(f"Failed to write DVC object: {e}")
            raise

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create a DVC version via Git commit; returns the commit SHA."""
        if not self._has_provider:
            return f"dvc-{self.branch}-mock-version"

        try:
            import subprocess
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.repo_path,
                check=True,
                capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self.repo_path,
                capture_output=True,
                text=True
            )

            sha_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            return sha_result.stdout.strip()
        except Exception as e:
            logger.error(f"Failed to create DVC version: {e}")
            raise
