"""
Wrapped lakeFS client that automatically captures commit SHAs.
"""

from typing import Optional, Dict, Any, Tuple, Union
from datetime import datetime, timezone
from briefcase._logging import get_logger
import os

from briefcase._otel import trace, HAS_OTEL
from briefcase.semantic_conventions import lakefs as lakefs_attrs

logger = get_logger(__name__)


class VersionedClient:
    """
    Wrapper around lakeFS client that automatically captures version metadata.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Environment variables (LAKEFS_ENDPOINT, LAKEFS_ACCESS_KEY, LAKEFS_PRIVATE_KEY)

    Construction requires an endpoint and credentials and raises when either
    is missing or the underlying client cannot be created. Every live
    operation raises on failure rather than returning placeholder data, so a
    commit SHA or object body reaching a decision record is always real.

    ``mock=True`` opts into an offline stub for tests: no lakeFS connection
    is made, every returned metadata and listing dict carries ``"mock": True``,
    and ``get_commit()`` returns a fixed placeholder SHA (a plain string, so
    it carries no tag; check ``client.mock`` before trusting it).
    ``require_live=True`` (or ``BRIEFCASE_LAKEFS_REQUIRE_LIVE``) rejects
    ``mock=True``, so a mock cannot slip into a production path.

    Usage:
        # Using explicit parameters
        client = VersionedClient(
            repository="acme-workspace",
            branch="main",
            briefcase_client=briefcase_client,
            lakefs_endpoint="https://lakefs.example.com/api/v1",
            lakefs_access_key="key",
            lakefs_secret_key="secret"
        )

        # Using environment variables
        # export LAKEFS_ENDPOINT="https://lakefs.example.com/api/v1"
        # export LAKEFS_ACCESS_KEY="key"
        # export LAKEFS_PRIVATE_KEY="secret"
        client = VersionedClient(
            repository="acme-workspace",
            branch="main",
            briefcase_client=briefcase_client
        )

        # All operations automatically tracked
        content = client.read_object("policies/policy.pdf")
    """

    def __init__(
        self,
        repository: str,
        branch: str,
        commit: str = "latest",
        briefcase_client=None,
        lakefs_endpoint: Optional[str] = None,
        lakefs_access_key: Optional[str] = None,
        lakefs_secret_key: Optional[str] = None,
        require_live: bool = False,
        mock: bool = False,
    ):
        self.repository = repository
        self.branch = branch
        self.commit = commit
        self.briefcase_client = briefcase_client
        self.require_live = require_live or self._is_truthy(
            os.getenv("BRIEFCASE_LAKEFS_REQUIRE_LIVE")
        )
        self.mock = mock
        if mock and self.require_live:
            raise ValueError("mock=True conflicts with require_live")

        self._lakefs = None
        self._lakefs_client = None
        self._has_lakefs = False
        self._branch_manager = None
        self._endpoint = None

        if not mock:
            # Resolve configuration with priority: param > env var
            resolved_access_key = lakefs_access_key or os.getenv("LAKEFS_ACCESS_KEY")
            resolved_secret_key = lakefs_secret_key or os.getenv("LAKEFS_PRIVATE_KEY")
            if not resolved_access_key or not resolved_secret_key:
                raise ValueError(
                    "missing credentials (set LAKEFS_ACCESS_KEY and LAKEFS_PRIVATE_KEY)"
                )
            resolved_endpoint = lakefs_endpoint or os.getenv("LAKEFS_ENDPOINT")
            if not resolved_endpoint:
                raise ValueError(
                    "missing endpoint (set LAKEFS_ENDPOINT or pass lakefs_endpoint)"
                )
            self._endpoint = self._normalize_endpoint(resolved_endpoint)
            try:
                import lakefs
                self._lakefs = lakefs
                self._lakefs_client = lakefs.Client(
                    host=self._endpoint,
                    username=resolved_access_key,
                    password=resolved_secret_key,
                )
                self._has_lakefs = True
            except Exception as e:
                raise RuntimeError(
                    "Failed to initialize lakeFS client. "
                    "Install `lakefs>=0.14.0` and provide valid lakeFS credentials."
                ) from e

        # Resolve commit SHA if "latest" requested
        if self.commit == "latest":
            self.commit = self._resolve_latest_commit()

        # Cache commit metadata
        self._commit_metadata = self._fetch_commit_metadata()

    def _resolve_latest_commit(self) -> str:
        """Resolve 'latest' to actual commit SHA."""
        if not self._has_lakefs or not self._lakefs_client:
            # Mock mode: return fake SHA
            return "abc123def456789012345678901234567890abcd"

        try:
            branch_ref = self._repository().branch(self.branch)
            return branch_ref.get_commit().id
        except Exception as e:
            raise RuntimeError(
                f"Failed to resolve latest commit for {self.repository}/{self.branch}"
            ) from e

    def _fetch_commit_metadata(self) -> Dict[str, Any]:
        """Fetch and cache commit metadata."""
        if not self._has_lakefs or not self._lakefs_client:
            # Mock mode: return fake metadata, tagged so it is never
            # mistaken for real provenance
            return {
                "sha": self.commit,
                "message": "Mock commit message",
                "author": "mock-author",
                "timestamp": datetime.now().isoformat(),
                "metadata": {},
                "mock": True,
            }

        try:
            commit = self._repository().ref(self.commit).get_commit()
            return {
                "sha": commit.id,
                "message": commit.message,
                "author": commit.committer,
                "timestamp": self._format_timestamp(commit.creation_date),
                "metadata": commit.metadata or {}
            }
        except Exception as e:
            raise RuntimeError(
                f"Failed to fetch commit metadata for {self.repository}@{self.commit}"
            ) from e

    def read_object(
        self,
        path: str,
        return_metadata: bool = False
    ) -> Union[bytes, Tuple[bytes, Dict]]:
        """
        Read an object from lakeFS with automatic instrumentation.

        Args:
            path: Object path (e.g., "policies/policy.pdf")
            return_metadata: If True, return (content, metadata) tuple

        Returns:
            Object content as bytes, optionally with metadata dict
        """
        start_time = datetime.now()

        if not self._has_lakefs or not self._lakefs_client:
            # Mock mode: return fake content
            content = b"Mock file content for " + path.encode()
            etag = "mock-etag-12345"
            content_type = "application/octet-stream"
            last_modified = None
        else:
            try:
                # Read from lakeFS using repository/ref/object model
                ref = self._repository().ref(self.commit)
                obj = ref.object(path)
                with obj.reader("rb") as reader:
                    content = reader.read()

                stats = obj.stat()
                etag = stats.checksum
                content_type = stats.content_type or "application/octet-stream"
                last_modified = self._format_timestamp(stats.mtime)
            except Exception as e:
                raise RuntimeError(
                    f"Failed to read object {path} from {self.repository}@{self.commit}"
                ) from e

        # Build metadata
        metadata = {
            "path": path,
            "size": len(content),
            "content_type": content_type,
            "etag": etag,
            "last_modified": last_modified,
            "commit_sha": self._commit_metadata.get("sha", self.commit),
            "commit_metadata": self._commit_metadata
        }
        if self.mock:
            metadata["mock"] = True

        # Instrument if Briefcase client available
        if self.briefcase_client:
            self._record_access(path, metadata, start_time)

        if return_metadata:
            return content, metadata
        return content

    def _record_access(
        self,
        path: str,
        metadata: Dict,
        start_time: datetime
    ):
        """Record file access in current Briefcase span."""
        if not HAS_OTEL:
            logger.debug("OpenTelemetry not available, skipping instrumentation")
            return

        try:
            current_span = trace.get_current_span()
            if not current_span or not current_span.is_recording():
                return

            # Set commit-level attributes (once per span)
            current_span.set_attribute(
                lakefs_attrs.LAKEFS_COMMIT_SHA,
                self._commit_metadata["sha"]
            )
            current_span.set_attribute(
                lakefs_attrs.LAKEFS_COMMIT_BRANCH,
                self.branch
            )
            current_span.set_attribute(
                lakefs_attrs.LAKEFS_COMMIT_TIMESTAMP,
                self._commit_metadata["timestamp"]
            )
            current_span.set_attribute(
                lakefs_attrs.LAKEFS_REPOSITORY,
                self.repository
            )

            # Record file access event
            current_span.add_event(
                "lakefs.file_accessed",
                attributes={
                    lakefs_attrs.LAKEFS_FILE_PATH: path,
                    lakefs_attrs.LAKEFS_FILE_SIZE: metadata["size"],
                    lakefs_attrs.LAKEFS_FILE_MODIFIED: metadata["last_modified"] or "unknown",
                    lakefs_attrs.LAKEFS_FILE_HASH: metadata["etag"],
                    lakefs_attrs.LAKEFS_ACCESS_TIME: datetime.now().isoformat(),
                    "duration_ms": (datetime.now() - start_time).total_seconds() * 1000
                }
            )

            # Set per-artifact attribute for this specific file
            artifact_attr = f"{lakefs_attrs.LAKEFS_ARTIFACT_PREFIX}{path}"
            current_span.set_attribute(artifact_attr, self.commit)
        except Exception as e:
            logger.error(f"Failed to record access: {e}")

    def list_objects(self, prefix: str = "") -> list:
        """List objects in lakeFS with optional prefix filter."""
        if not self._has_lakefs or not self._lakefs_client:
            # Mock mode: return fake list, tagged so it is never mistaken
            # for a real listing
            mock_results = [
                {"path": f"{prefix}file1.txt", "mock": True},
                {"path": f"{prefix}file2.txt", "mock": True},
            ]
            # Still emit OTel events in mock mode for testing
            if self.briefcase_client and HAS_OTEL:
                try:
                    current_span = trace.get_current_span()
                    if current_span and current_span.is_recording():
                        current_span.add_event(
                            "lakefs.objects_listed",
                            attributes={
                                "prefix": prefix,
                                "count": len(mock_results),
                                lakefs_attrs.LAKEFS_COMMIT_SHA: self.commit
                            }
                        )
                except Exception:
                    pass
            return mock_results

        try:
            results = []
            for obj in self._repository().ref(self.commit).objects(prefix=prefix):
                path = getattr(obj, "path", None)
                if not path:
                    continue
                entry = {"path": path}
                size = getattr(obj, "size_bytes", None)
                checksum = getattr(obj, "checksum", None)
                if size is not None:
                    entry["size_bytes"] = size
                if checksum:
                    entry["checksum"] = checksum
                results.append(entry)
        except Exception as e:
            raise RuntimeError(
                f"Failed to list objects from {self.repository}@{self.commit}"
            ) from e

        # Instrument listing operation
        if self.briefcase_client and HAS_OTEL:
            try:
                current_span = trace.get_current_span()
                if current_span and current_span.is_recording():
                    current_span.add_event(
                        "lakefs.objects_listed",
                        attributes={
                            "prefix": prefix,
                            "count": len(results),
                            lakefs_attrs.LAKEFS_COMMIT_SHA: self.commit
                        }
                    )
            except Exception as e:
                logger.error(f"Failed to instrument list operation: {e}")

        return results

    def get_commit(self) -> str:
        """Get the current commit SHA."""
        return self._commit_metadata.get("sha", self.commit)

    def object_exists(self, path: str) -> bool:
        """Check if an object exists in lakeFS."""
        if not self._has_lakefs or not self._lakefs_client:
            # Mock mode: always return True
            return True

        try:
            return self._repository().ref(self.commit).object(path).exists()
        except Exception as e:
            raise RuntimeError(
                f"Failed object existence check for {path} on {self.repository}@{self.commit}"
            ) from e

    def upload_object(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/octet-stream"
    ):
        """Upload an object to lakeFS."""
        if not self._has_lakefs or not self._lakefs_client:
            logger.debug("Mock mode: would upload %d bytes to %s", len(data), path)
            return

        try:
            obj = self._repository().branch(self.branch).object(path)
            with obj.writer(mode="wb", content_type=content_type) as writer:
                writer.write(data)
        except Exception as e:
            raise RuntimeError(
                f"Failed to upload object {path} to {self.repository}/{self.branch}"
            ) from e

    @staticmethod
    def _is_truthy(value: Optional[str]) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        normalized = endpoint.rstrip("/")
        if normalized.endswith("/api/v1"):
            normalized = normalized[:-7]
        return normalized

    @staticmethod
    def _format_timestamp(value: Any) -> str:
        if isinstance(value, (int, float)):
            # lakeFS SDK emits mtime/creation dates as epoch milliseconds
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).isoformat()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        if value is None:
            return datetime.now(tz=timezone.utc).isoformat()
        return str(value)

    @property
    def branch_manager(self):
        """Lazily-created ``BranchManager`` bound to this client's repository.

        The manager reuses this client's lakeFS connection; in mock mode it
        runs in the manager's own mock mode (no connection).
        """
        if self._branch_manager is None:
            from briefcase.integrations.lakefs.branches import BranchManager
            self._branch_manager = BranchManager(
                repository=self.repository,
                lakefs_client=self._lakefs_client,
                briefcase_client=self.briefcase_client,
            )
        return self._branch_manager

    def _repository(self):
        if not self._has_lakefs or not self._lakefs_client or not self._lakefs:
            raise RuntimeError("lakeFS client is not initialized")
        return self._lakefs.Repository(self.repository, client=self._lakefs_client)


# Backwards compatibility alias
InstrumentedLakeFSClient = VersionedClient
