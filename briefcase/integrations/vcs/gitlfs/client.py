"""Git LFS client for large file versioning in Git repositories.

Stdlib only: the client shells out to the ``git`` and ``git lfs``
command-line tools; no pip extra is needed. Without a Git LFS
installation and an LFS-configured repository every operation raises
``RuntimeError``. ``mock=True`` opts into an offline stub (placeholder
reads and version ids, tagged ``"mock": True`` in every metadata dict).
"""

from typing import Optional, Dict, List, Sequence
import os
import subprocess

from briefcase._logging import get_logger
from briefcase.integrations.vcs.base import (
    DEFAULT_COMMAND_TIMEOUT,
    VcsClientBase,
    git_commit_version,
)

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
        mock: bool = False,
        track_paths: Optional[Sequence[str]] = None,
        add_all: bool = False,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
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
            mock: Opt into the offline stub (placeholder reads and version ids).
            track_paths: Extra paths (relative to ``repo_path``) that
                ``create_version`` commits besides those written through
                ``write_object``.
            add_all: Commit the whole working tree (``git add -A``) instead
                of only the tracked paths.
            command_timeout: Timeout in seconds for each git command.
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
            mock=mock,
            **extra
        )

        self.repo_path = repo_path or "."
        self.add_all = add_all
        self.command_timeout = command_timeout
        self._track_paths: List[str] = list(track_paths or [])
        self._pending_paths: List[str] = list(self._track_paths)
        self._has_provider = False if self.mock else self._detect_provider()

    def _require_provider(self, operation: str) -> None:
        if not self._has_provider:
            raise RuntimeError(
                f"GitLFSClient.{operation}: Git LFS is not installed or "
                f"{self.repo_path!r} has no 'filter=lfs' entry in "
                ".gitattributes. Pass mock=True for an offline stub."
            )

    def _detect_provider(self) -> bool:
        """Detect whether Git LFS is installed and configured for this repo path."""
        try:
            result = subprocess.run(
                ["git", "lfs", "version"],
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
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
                "Git LFS not configured in %s (.gitattributes missing).",
                self.repo_path,
            )
            return False
        except Exception as e:
            logger.warning("Failed to inspect Git LFS config in %s: %s", self.repo_path, e)
            return False

    def _read_object_impl(self, path: str) -> bytes:
        """Read a large file from the Git LFS repository."""
        if self.mock:
            return b"Mock Git LFS file: " + path.encode()
        self._require_provider("read_object")

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
        if self.mock:
            logger.info(f"Mock Git LFS: Would track {len(data)} bytes at {path}")
            return
        self._require_provider("write_object")

        try:
            full_path = f"{self.repo_path}/{path}"
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(data)
            if path not in self._pending_paths:
                self._pending_paths.append(path)
            logger.info(f"Git LFS: Wrote {len(data)} bytes to {path}")
        except Exception as e:
            logger.error(f"Failed to write Git LFS object: {e}")
            raise

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create a Git LFS version via Git commit; returns the commit SHA.

        Commits the paths written through ``write_object`` plus
        ``track_paths`` (or the whole tree with ``add_all=True``). Raises
        ``NothingToCommitError`` when there is no change to commit.
        """
        if self.mock:
            return f"gitlfs-{self.branch}-mock-commit"
        self._require_provider("create_version")

        try:
            sha = git_commit_version(
                self.repo_path,
                message,
                paths=None if self.add_all else self._commit_paths(),
                timeout=self.command_timeout,
            )
        except Exception as e:
            logger.error(f"Failed to create Git LFS version: {e}")
            raise
        self._pending_paths = [p for p in self._pending_paths if p in self._track_paths]
        return sha

    def _commit_paths(self) -> List[str]:
        if not self._pending_paths:
            raise ValueError(
                "GitLFSClient.create_version: no paths to commit. Write through "
                "write_object, pass track_paths=[...], or pass add_all=True to "
                "commit the whole working tree."
            )
        return list(self._pending_paths)
