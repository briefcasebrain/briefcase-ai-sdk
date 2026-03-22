"""
Wrapped lakeFS client that automatically captures commit SHAs.
"""

from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone
import logging
import os

try:
    from opentelemetry import trace
    HAS_OTEL = True
except ImportError:
    trace = None  # type: ignore[assignment]
    HAS_OTEL = False

from briefcase.semantic_conventions import lakefs as lakefs_attrs

logger = logging.getLogger(__name__)


class VersionedClient:
    """
    Wrapper around lakeFS client that automatically captures version metadata.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Environment variables (LAKEFS_ENDPOINT, LAKEFS_ACCESS_KEY, LAKEFS_PRIVATE_KEY)
        3. Default endpoint (https://briefcaseai.us-east-1.lakefscloud.io/api/v1)

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
    ):
        self.repository = repository
        self.branch = branch
        self.commit = commit
        self.briefcase_client = briefcase_client
        self.require_live = require_live or self._is_truthy(
            os.getenv("BRIEFCASE_LAKEFS_REQUIRE_LIVE")
        )

        # Resolve configuration with priority: param > env var > default
        resolved_endpoint = (
            lakefs_endpoint or
            os.getenv("LAKEFS_ENDPOINT") or
            "https://briefcaseai.us-east-1.lakefscloud.io/api/v1"
        )
        self._endpoint = self._normalize_endpoint(resolved_endpoint)
        resolved_access_key = lakefs_access_key or os.getenv("LAKEFS_ACCESS_KEY")
        resolved_secret_key = lakefs_secret_key or os.getenv("LAKEFS_PRIVATE_KEY")

        # Initialize underlying lakeFS client
        self._lakefs = None
        self._lakefs_client = None
        self._has_lakefs = False
        self._branch_manager = None
        try:
            import lakefs
            self._lakefs = lakefs
            if not resolved_access_key or not resolved_secret_key:
                raise ValueError(
                    "missing credentials (set LAKEFS_ACCESS_KEY and LAKEFS_PRIVATE_KEY)"
                )
            self._lakefs_client = lakefs.Client(
                host=self._endpoint,
                username=resolved_access_key,
                password=resolved_secret_key,
            )
            self._has_lakefs = True
        except (ImportError, Exception) as e:
            if self.require_live:
                raise RuntimeError(
                    "Failed to initialize live lakeFS client in strict mode. "
                    "Install `lakefs>=0.14.0` and provide valid lakeFS credentials."
                ) from e
            logger.warning(f"lakeFS client not available: {e}. Using mock mode.")

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
            if self.require_live:
                raise RuntimeError(
                    f"Failed to resolve latest commit for {self.repository}/{self.branch}"
                ) from e
            logger.error(f"Failed to resolve latest commit: {e}")
            # Return mock SHA on error
            return "abc123def456789012345678901234567890abcd"

    def _fetch_commit_metadata(self) -> Dict[str, Any]:
        """Fetch and cache commit metadata."""
        if not self._has_lakefs or not self._lakefs_client:
            # Mock mode: return fake metadata
            return {
                "sha": self.commit,
                "message": "Mock commit message",
                "author": "mock-author",
                "timestamp": datetime.now().isoformat(),
                "metadata": {}
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
            if self.require_live:
                raise RuntimeError(
                    f"Failed to fetch commit metadata for {self.repository}@{self.commit}"
                ) from e
            logger.error(f"Failed to fetch commit metadata: {e}")
            # Return mock metadata on error
            return {
                "sha": self.commit,
                "message": "Error fetching commit",
                "author": "unknown",
                "timestamp": datetime.now().isoformat(),
                "metadata": {}
            }

    def read_object(
        self,
        path: str,
        return_metadata: bool = False
    ) -> bytes | Tuple[bytes, Dict]:
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
                if self.require_live:
                    raise RuntimeError(
                        f"Failed to read object {path} from {self.repository}@{self.commit}"
                    ) from e
                logger.error(f"Failed to read object {path}: {e}")
                # Return mock content on error
                content = b"Error reading file"
                etag = "error-etag"
                content_type = "application/octet-stream"
                last_modified = None

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
            logger.warning("OpenTelemetry not available, skipping instrumentation")
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
            # Mock mode: return fake list
            mock_results = [{"path": f"{prefix}file1.txt"}, {"path": f"{prefix}file2.txt"}]
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
            if self.require_live:
                raise RuntimeError(
                    f"Failed to list objects from {self.repository}@{self.commit}"
                ) from e
            logger.error(f"Failed to list objects: {e}")
            # Return mock list on error
            return [{"path": f"{prefix}error.txt"}]

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
            if self.require_live:
                raise RuntimeError(
                    f"Failed object existence check for {path} on {self.repository}@{self.commit}"
                ) from e
            return False

    def upload_object(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/octet-stream"
    ):
        """Upload an object to lakeFS."""
        if not self._has_lakefs or not self._lakefs_client:
            logger.info(f"Mock mode: Would upload {len(data)} bytes to {path}")
            return

        try:
            obj = self._repository().branch(self.branch).object(path)
            with obj.writer(mode="wb", content_type=content_type) as writer:
                writer.write(data)
        except Exception as e:
            logger.error(f"Failed to upload object {path}: {e}")
            if self.require_live:
                raise RuntimeError(
                    f"Failed to upload object {path} to {self.repository}/{self.branch}"
                ) from e
            raise

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
        """Lazily-created branch manager.

        Requires the enterprise package (``briefcase-ai-enterprise``)
        which provides the ``BranchManager`` class.

        Raises:
            ImportError: If ``briefcase-ai-enterprise`` is not installed.
        """
        if self._branch_manager is None:
            try:
                from briefcase.integrations.lakefs.branches import BranchManager  # noqa: PLC0415
            except ImportError:
                raise ImportError(
                    "BranchManager requires the enterprise package.\n"
                    "Install it with: pip install briefcase-ai-enterprise"
                )
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
