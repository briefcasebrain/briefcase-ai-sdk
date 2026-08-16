"""
Artifact lineage client for lakeFS commit-aware workflows.

This client handles workflow artifact versioning (upload + commit),
separate from the read-oriented ``VersionedClient``. Live mode shells out
to the ``lakectl`` CLI, so it needs no Python dependency beyond the
standard library; simulate mode records deterministic commits in a local
JSON state file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Dict, Iterable, List, Optional, Union


@dataclass
class ArtifactLineageConfig:
    """Configuration for artifact lineage operations."""

    repository: str
    branch: str = "main"
    mode: str = "auto"  # auto | live | simulate
    storage_namespace: Optional[str] = None
    base_uri: Optional[str] = None
    config_path: Optional[str] = None
    local_state_dir: Path = field(default_factory=lambda: Path("tmp/lakefs_lineage"))


@dataclass
class ArtifactCommitInfo:
    """Metadata for a completed artifact commit."""

    commit_id: str
    repository: str
    branch: str
    mode: str
    message: str
    files: Dict[str, str]
    metadata: Dict[str, str]
    timestamp_utc: str


class ArtifactLineageError(RuntimeError):
    """Raised on lakeFS artifact lineage errors."""


class ArtifactLineageClient:
    """Commit files to lakeFS with a local deterministic fallback mode."""

    def __init__(self, config: ArtifactLineageConfig):
        mode = config.mode.lower().strip()
        if mode not in {"auto", "live", "simulate"}:
            raise ValueError(f"Unsupported mode '{config.mode}'. Use auto/live/simulate.")

        self.config = config
        self.config.mode = mode

    @classmethod
    def from_env(
        cls,
        repository: str,
        branch: str = "main",
        local_state_dir: Optional[Path] = None,
    ) -> "ArtifactLineageClient":
        """
        Build configuration from environment variables.

        Supported keys:
        - LAKEFS_MODE=auto|live|simulate
        - LAKEFS_STORAGE_NAMESPACE=s3://bucket/prefix
        - LAKEFS_BASE_URI=https://lakefs.example.com
        - LAKEFS_CONFIG_PATH=/path/to/lakectl.yaml
        """
        config = ArtifactLineageConfig(
            repository=repository,
            branch=branch,
            mode=os.getenv("LAKEFS_MODE", "auto"),
            storage_namespace=os.getenv("LAKEFS_STORAGE_NAMESPACE"),
            base_uri=os.getenv("LAKEFS_BASE_URI"),
            config_path=os.getenv("LAKEFS_CONFIG_PATH"),
            local_state_dir=local_state_dir or Path("tmp/lakefs_lineage"),
        )
        return cls(config)

    @property
    def mode(self) -> str:
        """Return effective run mode."""
        if self.config.mode == "simulate":
            return "simulated"
        if self.config.mode == "live":
            return "live"
        return "live" if self._can_use_lakefs_live() else "simulated"

    def version_files(
        self,
        files: Dict[str, Union[str, Path]],
        message: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> ArtifactCommitInfo:
        """Upload files and create a commit (or simulated commit)."""
        normalized: Dict[str, Path] = {
            object_path: Path(local_path) for object_path, local_path in files.items()
        }
        metadata = metadata or {}

        if self.mode == "live":
            try:
                return self._version_files_live(normalized, message, metadata)
            except ArtifactLineageError:
                if self.config.mode == "live":
                    raise
                # auto mode falls back to a simulated commit

        return self._version_files_simulated(normalized, message, metadata)

    def object_uri(self, object_path: str, commit_or_branch: Optional[str] = None) -> str:
        """Build a stable lakeFS URI for an object."""
        ref = commit_or_branch or self.config.branch
        clean_path = object_path.lstrip("/")
        return f"lakefs://{self.config.repository}/{ref}/{clean_path}"

    def _version_files_live(
        self,
        files: Dict[str, Path],
        message: str,
        metadata: Dict[str, str],
    ) -> ArtifactCommitInfo:
        self._ensure_repo_and_branch_live()

        uploaded: Dict[str, str] = {}
        for object_path, local_path in sorted(files.items()):
            if not local_path.exists():
                raise ArtifactLineageError(f"Missing local source file: {local_path}")

            destination_uri = self.object_uri(object_path)
            cmd = self._lakectl_cmd(
                [
                    "fs",
                    "upload",
                    destination_uri,
                    "--source",
                    str(local_path),
                    "--no-progress",
                ]
            )
            self._run_checked(cmd)
            uploaded[object_path] = str(local_path)

        commit_cmd = self._lakectl_cmd(
            ["commit", f"lakefs://{self.config.repository}/{self.config.branch}", "-m", message]
        )
        for key, value in metadata.items():
            commit_cmd.extend(["--meta", f"{key}={value}"])

        output = self._run_checked(commit_cmd)
        commit_id = self._extract_commit_id(output) or self._head_commit_id_live()
        if not commit_id:
            raise ArtifactLineageError("Could not resolve lakeFS commit id after commit.")

        return ArtifactCommitInfo(
            commit_id=commit_id,
            repository=self.config.repository,
            branch=self.config.branch,
            mode="live",
            message=message,
            files=uploaded,
            metadata=metadata,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )

    def _version_files_simulated(
        self,
        files: Dict[str, Path],
        message: str,
        metadata: Dict[str, str],
    ) -> ArtifactCommitInfo:
        state = self._load_state()
        previous_head = str(state.get("head_commit", ""))
        hash_parts: List[str] = [previous_head, message, json.dumps(metadata, sort_keys=True)]
        normalized_files: Dict[str, str] = {}

        for object_path, local_path in sorted(files.items()):
            if not local_path.exists():
                raise ArtifactLineageError(f"Missing local source file: {local_path}")

            hash_parts.append(object_path)
            hash_parts.append(self._file_sha256(local_path))
            normalized_files[object_path] = str(local_path)

        hash_parts.append(datetime.now(timezone.utc).isoformat())
        commit_id = hashlib.sha256("|".join(hash_parts).encode("utf-8")).hexdigest()

        info = ArtifactCommitInfo(
            commit_id=commit_id,
            repository=self.config.repository,
            branch=self.config.branch,
            mode="simulated",
            message=message,
            files=normalized_files,
            metadata=metadata,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )

        commits = state.setdefault("commits", [])
        if isinstance(commits, list):
            commits.append(asdict(info))
        state["head_commit"] = commit_id
        self._save_state(state)
        return info

    def _ensure_repo_and_branch_live(self) -> None:
        repository_uri = f"lakefs://{self.config.repository}"

        if self.config.storage_namespace:
            create_repo_cmd = self._lakectl_cmd(
                [
                    "repo",
                    "create",
                    repository_uri,
                    self.config.storage_namespace,
                    "--default-branch",
                    "main",
                ]
            )
            self._run_checked(create_repo_cmd, ignore_errors=("already exists",))

        if self.config.branch != "main":
            branch_uri = f"lakefs://{self.config.repository}/{self.config.branch}"
            source_uri = f"lakefs://{self.config.repository}/main"
            create_branch_cmd = self._lakectl_cmd(["branch", "create", branch_uri, "-s", source_uri])
            self._run_checked(create_branch_cmd, ignore_errors=("already exists",))

    def _head_commit_id_live(self) -> Optional[str]:
        cmd = self._lakectl_cmd(
            ["log", f"lakefs://{self.config.repository}/{self.config.branch}", "--amount", "1", "--limit"]
        )
        output = self._run_checked(cmd)
        return self._extract_commit_id(output)

    def _can_use_lakefs_live(self) -> bool:
        if shutil.which("lakectl") is None:
            return False
        try:
            self._run_checked(self._lakectl_cmd(["repo", "list", "--amount", "1"]))
            return True
        except ArtifactLineageError:
            return False

    def _lakectl_cmd(self, args: Iterable[str]) -> List[str]:
        cmd = ["lakectl"]
        if self.config.base_uri:
            cmd.extend(["--base-uri", self.config.base_uri])
        if self.config.config_path:
            cmd.extend(["--config", self.config.config_path])
        cmd.extend(list(args))
        return cmd

    def _run_checked(
        self,
        cmd: List[str],
        ignore_errors: tuple[str, ...] = (),
    ) -> str:
        try:
            completed = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
            return f"{completed.stdout}\n{completed.stderr}".strip()
        except subprocess.CalledProcessError as exc:
            combined = f"{exc.stdout}\n{exc.stderr}".strip()
            lowered = combined.lower()
            if ignore_errors and any(token in lowered for token in ignore_errors):
                return combined
            raise ArtifactLineageError(f"lakectl command failed: {' '.join(cmd)}\n{combined}") from exc

    def _load_state(self) -> Dict[str, object]:
        state_file = self._state_file
        if not state_file.exists():
            return {
                "repository": self.config.repository,
                "branch": self.config.branch,
                "head_commit": "",
                "commits": [],
            }
        return json.loads(state_file.read_text(encoding="utf-8"))

    def _save_state(self, state: Dict[str, object]) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    @property
    def _state_file(self) -> Path:
        stem = f"{self.config.repository.replace('/', '_')}__{self.config.branch}.json"
        return self.config.local_state_dir / stem

    @staticmethod
    def _extract_commit_id(text: str) -> Optional[str]:
        for token in _split_token_candidates(text):
            if len(token) == 64 and all(ch in "0123456789abcdef" for ch in token):
                return token
        return None

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()


def _split_token_candidates(text: str) -> List[str]:
    separators = ["\n", "\t", " ", ",", ";", ":", "[", "]", "(", ")"]
    parts = [text]
    for separator in separators:
        next_parts: List[str] = []
        for part in parts:
            next_parts.extend(part.split(separator))
        parts = next_parts
    return [part.strip() for part in parts if part.strip()]
