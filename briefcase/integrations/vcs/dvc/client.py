"""DVC client for data versioning and artifact tracking.

Requires ``dvc``: ``pip install briefcase-ai[vcs-dvc]`` or
``pip install dvc``. Without it the constructor raises ``ImportError``.
``mock=True`` opts into an offline stub (placeholder reads and version
ids, tagged ``"mock": True`` in every metadata dict).
"""

from typing import Optional, Dict, List, Sequence
import os

from briefcase._logging import get_logger
from briefcase.integrations.vcs.base import (
    DEFAULT_COMMAND_TIMEOUT,
    VcsClientBase,
    git_commit_version,
)

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
        mock: bool = False,
        track_paths: Optional[Sequence[str]] = None,
        add_all: bool = False,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
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
            mock: Opt into the offline stub (placeholder reads and version ids).
            track_paths: Extra paths (relative to ``repo_path``) that
                ``create_version`` commits besides those written through
                ``write_object``.
            add_all: Commit the whole working tree (``git add -A``) instead
                of only the tracked paths.
            command_timeout: Timeout in seconds for each git command.
            **extra: Additional DVC configuration options
        """
        super().__init__(
            provider_type="dvc",
            repository=repository,
            branch=branch,
            briefcase_client=briefcase_client,
            endpoint=remote,
            mock=mock,
            **extra
        )

        self.repo_path = repo_path or os.getenv("DVC_REPO_PATH", ".")
        self.remote = remote or os.getenv("DVC_REMOTE")

        self.add_all = add_all
        self.command_timeout = command_timeout
        self._track_paths: List[str] = list(track_paths or [])
        self._pending_paths: List[str] = list(self._track_paths)

        if self.mock:
            return
        try:
            import dvc.repo
        except ImportError as e:
            raise ImportError(
                f"DvcClient requires dvc. {_DVC_INSTALL_HINT} "
                "Pass mock=True for an offline stub."
            ) from e
        # The DVC repository handle validates repo_path; a path that is not
        # a DVC repository raises here.
        self._provider_client = dvc.repo.Repo(self.repo_path)
        self._has_provider = True

    def _read_object_impl(self, path: str) -> bytes:
        """Read a DVC-tracked file from the working tree."""
        if self.mock:
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
        if self.mock:
            logger.info(f"Mock DVC: Would write {len(data)} bytes to {path}")
            return

        try:
            full_path = f"{self.repo_path}/{path}"
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(data)
            if path not in self._pending_paths:
                self._pending_paths.append(path)
            logger.info(f"Wrote {len(data)} bytes to {path}")
        except Exception as e:
            logger.error(f"Failed to write DVC object: {e}")
            raise

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Create a DVC version via Git commit; returns the commit SHA.

        Commits the paths written through ``write_object`` plus
        ``track_paths`` (or the whole tree with ``add_all=True``). Raises
        ``NothingToCommitError`` when there is no change to commit.
        """
        if self.mock:
            return f"dvc-{self.branch}-mock-version"

        try:
            sha = git_commit_version(
                self.repo_path,
                message,
                paths=None if self.add_all else self._commit_paths(),
                timeout=self.command_timeout,
            )
        except Exception as e:
            logger.error(f"Failed to create DVC version: {e}")
            raise
        self._pending_paths = [p for p in self._pending_paths if p in self._track_paths]
        return sha

    def _commit_paths(self) -> List[str]:
        if not self._pending_paths:
            raise ValueError(
                "DvcClient.create_version: no paths to commit. Write through "
                "write_object, pass track_paths=[...], or pass add_all=True to "
                "commit the whole working tree."
            )
        return list(self._pending_paths)
