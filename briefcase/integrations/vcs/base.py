"""
Base class for VCS client implementations.

Provides common functionality for all VCS providers including:
- Object read/write operations
- Version creation and tracking
- Metadata capture
- Context manager support
- OpenTelemetry instrumentation
"""

from typing import Optional, Dict, Any, Tuple, Union
from datetime import datetime
from briefcase._logging import get_logger

from briefcase._otel import trace, HAS_OTEL

logger = get_logger(__name__)


class VcsClientBase:
    """
    Base class for all VCS client implementations.

    Provides common interface for reading, writing, and versioning objects
    across different VCS providers. Supports context manager protocol and
    OpenTelemetry instrumentation.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Briefcase client configuration
        3. Environment variables
        4. Provider defaults

    Attributes:
        provider_type: Type of VCS provider (e.g., "dvc", "nessie", "pachyderm")
        briefcase_client: Optional BriefcaseClient for instrumentation
        endpoint: API endpoint for VCS provider
        repository: Repository or bucket name
        branch: Branch or version name
        version: Current version identifier (SHA, tag, etc.)
    """

    def __init__(
        self,
        provider_type: str,
        repository: str,
        branch: str = "main",
        briefcase_client=None,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        token: Optional[str] = None,
        **extra
    ):
        """
        Initialize VCS client with provider-specific configuration.

        Args:
            provider_type: Type of VCS provider
            repository: Repository/bucket name
            branch: Branch or version name (default: "main")
            briefcase_client: Optional BriefcaseClient for instrumentation
            endpoint: API endpoint (env var or default)
            access_key: Access key for authentication
            secret_key: Secret key for authentication
            token: Bearer token for authentication
            **extra: Provider-specific configuration options
        """
        self.provider_type = provider_type
        self.repository = repository
        self.branch = branch
        self.briefcase_client = briefcase_client
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token
        self.extra = extra

        # Version tracking
        self.version = None
        self._version_metadata = {}

        # Initialize provider-specific client (to be implemented by subclasses)
        self._provider_client = None
        self._has_provider = False

    def read_object(
        self,
        path: str,
        return_metadata: bool = False
    ) -> Union[bytes, Tuple[bytes, Dict]]:
        """
        Read an object from VCS with automatic instrumentation.

        Args:
            path: Object path (e.g., "data/file.csv")
            return_metadata: If True, return (content, metadata) tuple

        Returns:
            Object content as bytes, optionally with metadata dict
        """
        start_time = datetime.now()
        content = b""
        metadata = {
            "path": path,
            "provider": self.provider_type,
            "repository": self.repository,
            "branch": self.branch,
            "version": self.version,
        }

        try:
            # Provider-specific implementation
            content = self._read_object_impl(path)
            metadata["size"] = len(content)
            metadata["status"] = "success"
        except Exception as e:
            logger.error(f"Failed to read {path} from {self.provider_type}: {e}")
            metadata["status"] = "error"
            metadata["error"] = str(e)

        # Record access if instrumentation available
        if self.briefcase_client:
            self._record_access(path, metadata, start_time)

        if return_metadata:
            return content, metadata
        return content

    def write_object(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/octet-stream"
    ) -> Dict[str, Any]:
        """
        Write an object to VCS with automatic instrumentation.

        Args:
            path: Object path
            data: Object content as bytes
            content_type: Content type (default: "application/octet-stream")

        Returns:
            Metadata dict with write result
        """
        start_time = datetime.now()
        metadata = {
            "path": path,
            "provider": self.provider_type,
            "repository": self.repository,
            "branch": self.branch,
            "size": len(data),
        }

        try:
            # Provider-specific implementation
            self._write_object_impl(path, data, content_type)
            metadata["status"] = "success"
        except Exception as e:
            logger.error(f"Failed to write {path} to {self.provider_type}: {e}")
            metadata["status"] = "error"
            metadata["error"] = str(e)

        # Record write if instrumentation available
        if self.briefcase_client:
            self._record_write(path, metadata, start_time)

        return metadata

    def create_version(
        self,
        message: str,
        metadata: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Create a new version (commit, tag, branch point) in VCS.

        Args:
            message: Version message/commit message
            metadata: Optional metadata to attach to version

        Returns:
            Version metadata dict with identifier
        """
        start_time = datetime.now()
        version_metadata = {
            "provider": self.provider_type,
            "repository": self.repository,
            "branch": self.branch,
            "message": message,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {}
        }

        try:
            # Provider-specific implementation
            version_id = self._create_version_impl(message, metadata)
            version_metadata["version_id"] = version_id
            version_metadata["status"] = "success"
            self.version = version_id
            self._version_metadata = version_metadata
        except Exception as e:
            logger.error(f"Failed to create version in {self.provider_type}: {e}")
            version_metadata["status"] = "error"
            version_metadata["error"] = str(e)

        # Record version creation
        if self.briefcase_client:
            self._record_version_creation(version_metadata, start_time)

        return version_metadata

    def capture_metadata(self) -> Dict[str, Any]:
        """
        Capture current VCS metadata snapshot.

        Returns:
            Dictionary containing:
            - provider_type: VCS provider type
            - endpoint: API endpoint
            - repository: Repository name
            - branch: Branch name
            - version: Current version identifier
            - timestamp: Capture timestamp
            - version_metadata: Full version metadata if available
        """
        return {
            "provider_type": self.provider_type,
            "endpoint": self.endpoint,
            "repository": self.repository,
            "branch": self.branch,
            "version": self.version,
            "timestamp": datetime.now().isoformat(),
            "version_metadata": self._version_metadata,
        }

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False

    def close(self):
        """Close provider-specific client connection."""
        try:
            if self._provider_client and hasattr(self._provider_client, 'close'):
                self._provider_client.close()
        except Exception as e:
            logger.warning(f"Failed to close {self.provider_type} client: {e}")

    # Protected methods for subclasses to override

    def _read_object_impl(self, path: str) -> bytes:
        """
        Provider-specific object read implementation.

        Must be overridden by subclasses.

        Args:
            path: Object path

        Returns:
            Object content as bytes
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement _read_object_impl")

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """
        Provider-specific object write implementation.

        Must be overridden by subclasses.

        Args:
            path: Object path
            data: Object content as bytes
            content_type: Content type
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement _write_object_impl")

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """
        Provider-specific version creation implementation.

        Must be overridden by subclasses.

        Args:
            message: Version message
            metadata: Version metadata

        Returns:
            Version identifier (SHA, tag, etc.)
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement _create_version_impl")

    # Instrumentation methods

    def _record_access(
        self,
        path: str,
        metadata: Dict[str, Any],
        start_time: datetime
    ) -> None:
        """Record object access in OpenTelemetry trace."""
        if not HAS_OTEL:
            return

        try:
            current_span = trace.get_current_span()
            if not current_span or not current_span.is_recording():
                return

            # Set provider-level attributes
            current_span.set_attribute("vcs.provider", self.provider_type)
            current_span.set_attribute("vcs.repository", self.repository)
            current_span.set_attribute("vcs.branch", self.branch)
            if self.version:
                current_span.set_attribute("vcs.version", self.version)

            # Record file access event
            current_span.add_event(
                f"{self.provider_type}.file_accessed",
                attributes={
                    "vcs.file_path": path,
                    "vcs.file_size": metadata.get("size", 0),
                    "duration_ms": (datetime.now() - start_time).total_seconds() * 1000
                }
            )
        except Exception as e:
            logger.warning(f"Failed to record access instrumentation: {e}")

    def _record_write(
        self,
        path: str,
        metadata: Dict[str, Any],
        start_time: datetime
    ) -> None:
        """Record object write in OpenTelemetry trace."""
        if not HAS_OTEL:
            return

        try:
            current_span = trace.get_current_span()
            if not current_span or not current_span.is_recording():
                return

            # Set provider-level attributes
            current_span.set_attribute("vcs.provider", self.provider_type)
            current_span.set_attribute("vcs.repository", self.repository)
            current_span.set_attribute("vcs.branch", self.branch)

            # Record write event
            current_span.add_event(
                f"{self.provider_type}.file_written",
                attributes={
                    "vcs.file_path": path,
                    "vcs.file_size": metadata.get("size", 0),
                    "vcs.status": metadata.get("status", "unknown"),
                    "duration_ms": (datetime.now() - start_time).total_seconds() * 1000
                }
            )
        except Exception as e:
            logger.warning(f"Failed to record write instrumentation: {e}")

    def _record_version_creation(
        self,
        metadata: Dict[str, Any],
        start_time: datetime
    ) -> None:
        """Record version creation in OpenTelemetry trace."""
        if not HAS_OTEL:
            return

        try:
            current_span = trace.get_current_span()
            if not current_span or not current_span.is_recording():
                return

            # Set version attributes
            current_span.set_attribute("vcs.provider", self.provider_type)
            if metadata.get("version_id"):
                current_span.set_attribute("vcs.version_id", metadata["version_id"])

            # Record version creation event
            current_span.add_event(
                f"{self.provider_type}.version_created",
                attributes={
                    "vcs.message": metadata.get("message", ""),
                    "vcs.status": metadata.get("status", "unknown"),
                    "duration_ms": (datetime.now() - start_time).total_seconds() * 1000
                }
            )
        except Exception as e:
            logger.warning(f"Failed to record version creation instrumentation: {e}")
