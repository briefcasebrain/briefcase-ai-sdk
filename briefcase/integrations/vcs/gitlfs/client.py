"""Git LFS client for large file versioning in Git repositories.

Stdlib only: the client shells out to the ``git`` and ``git lfs``
command-line tools; no pip extra is needed. Without a Git LFS
installation and an LFS-configured repository it runs in mock mode
(reads return placeholder bytes, writes and versions are recorded in
metadata only).
"""

from typing import Optional, Dict
import os
import subprocess

from briefcase._logging import get_logger
from briefcase.integrations.vcs.base import VcsClientBase

logger = get_logger(__name__)


class GitLFSClient(VcsClientBase):
    """
    Git LFS client for managing large files in Git repositories.

    Git LFS extends Git with support for large files by replacing them
    with lightweight pointers and storing actual file content externally.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Environment variables (GIT_LFS_ENDPOINT)
        3. Remote Git repository

    Usage:
        client = GitLFSClient(
            repository="https://github.com/user/ml-datasets",
            branch="main",
            briefcase_client=briefcase_client,
            repo_path="/tmp/repo"
        )
        model = client.read_object("models/model.pkl")
        client.create_version("Updated model weights")
    """

    def __init__(
        self,
        repository: str,
        branch: str = "main",
        briefcase_client=None,
        endpoint: Optional[str] = None,
        repo_path: Optional[str] = None,
        **extra
    ):
        """
        Initialize Git LFS client.

        Args:
            repository: Git repository URL or name
            branch: Git branch name (default: "main")
            briefcase_client: Optional BriefcaseClient for instrumentation
            endpoint: Git LFS server endpoint
            repo_path: Local repository path
            **extra: Additional Git LFS configuration
        """
        resolved_endpoint = (
            endpoint or
            os.getenv("GIT_LFS_ENDPOINT") or
            repository
        )

        super().__init__(
            provider_type="gitlfs",
            repository=repository,
            branch=branch,
            briefcase_client=briefcase_client,
            endpoint=resolved_endpoint,
            **extra
        )

        self.repo_path = repo_path or "."
        self._has_provider = self._detect_provider()

    def _detect_provider(self) -> bool:
        """Detect whether Git LFS is installed and configured for this repo path."""
        try:
            result = subprocess.run(
                ["git", "lfs", "version"],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                return False
        except Exception as e:
            logger.warning(f"Git LFS not available: {e}")
            return False

        attributes_path = os.path.join(self.repo_path, ".gitattributes")
        try:
            with open(attributes_path, "r", encoding="utf-8") as attributes_file:
                return "filter=lfs" in attributes_file.read()
        except FileNotFoundError:
            logger.debug(
                "Git LFS not configured in %s (.gitattributes missing). Using mock mode.",
                self.repo_path,
            )
            return False
        except Exception as e:
            logger.warning("Failed to inspect Git LFS config in %s: %s", self.repo_path, e)
            return False

    def _read_object_impl(self, path: str) -> bytes:
        """Read a large file from the Git LFS repository."""
        if not self._has_provider:
            return b"Mock Git LFS file: " + path.encode()

        try:
            full_path = f"{self.repo_path}/{path}"
            with open(full_path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"Git LFS file not found: {path}")

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """Write a large file into the Git LFS repository."""
        if not self._has_provider:
            logger.info(f"Mock Git LFS: Would track {len(data)} bytes at {path}")
            return

        try:
            full_path = f"{self.repo_path}/{path}"
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(data)
            logger.info(f"Git LFS: Wrote {len(data)} bytes to {path}")
        except Exception as e:
            logger.error(f"Failed to write Git LFS object: {e}")
            raise

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create a Git LFS version via Git commit; returns the commit SHA."""
        if not self._has_provider:
            return f"gitlfs-{self.branch}-mock-commit"

        try:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.repo_path,
                check=True,
                capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self.repo_path,
                capture_output=True
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
            logger.error(f"Failed to create Git LFS version: {e}")
            raise
