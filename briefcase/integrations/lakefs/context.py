"""
Context manager for lakeFS versioned operations.
"""

from typing import Optional
from contextlib import contextmanager
from briefcase._logging import get_logger

from briefcase._otel import trace, HAS_OTEL
from briefcase.integrations.lakefs.client import VersionedClient

logger = get_logger(__name__)


class VersionedContextManager:
    """
    Context manager for versioned knowledge base access.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. briefcase_client.config dict
        3. Environment variables (LAKEFS_ENDPOINT, LAKEFS_ACCESS_KEY, LAKEFS_PRIVATE_KEY)

    ``mock=True`` yields the offline stub client instead of connecting; see
    ``VersionedClient``.

    Usage:
        with versioned_context(client, "acme", "main") as versioned:
            policy = versioned.read_object("policies/policy.pdf")
    """

    def __init__(
        self,
        briefcase_client,
        repository: str,
        branch: str = "main",
        commit: str = "latest",
        lakefs_endpoint: Optional[str] = None,
        lakefs_access_key: Optional[str] = None,
        lakefs_secret_key: Optional[str] = None,
        require_live: bool = False,
        mock: bool = False,
    ):
        self.briefcase_client = briefcase_client
        self.repository = repository
        self.branch = branch
        self.commit = commit

        # Get lakeFS credentials from config if not provided
        if hasattr(briefcase_client, 'config'):
            if lakefs_endpoint is None:
                lakefs_endpoint = briefcase_client.config.get("lakefs_endpoint")
            if lakefs_access_key is None:
                lakefs_access_key = briefcase_client.config.get("lakefs_access_key")
            if lakefs_secret_key is None:
                lakefs_secret_key = briefcase_client.config.get("lakefs_secret_key")

        self.lakefs_endpoint = lakefs_endpoint
        self.lakefs_access_key = lakefs_access_key
        self.lakefs_secret_key = lakefs_secret_key
        self.require_live = require_live
        self.mock = mock

        self._lakefs_client = None
        self._span = None
        self._span_token = None

    def __enter__(self):
        """Enter context - create span and lakeFS client."""
        if HAS_OTEL:
            try:
                # Start a span for this versioned context
                tracer = trace.get_tracer(__name__)
                self._span = tracer.start_as_current_span(
                    "lakefs.versioned_context",
                    attributes={
                        "lakefs.repository": self.repository,
                        "lakefs.branch": self.branch,
                        "lakefs.commit.requested": self.commit
                    }
                )
                # Enter the span context
                self._span_token = self._span.__enter__()
            except Exception as e:
                logger.debug("Failed to create OTel span: %s", e, exc_info=True)

        # Create instrumented lakeFS client
        self._lakefs_client = VersionedClient(
            repository=self.repository,
            branch=self.branch,
            commit=self.commit,
            briefcase_client=self.briefcase_client,
            lakefs_endpoint=self.lakefs_endpoint,
            lakefs_access_key=self.lakefs_access_key,
            lakefs_secret_key=self.lakefs_secret_key,
            require_live=self.require_live,
            mock=self.mock,
        )

        return self._lakefs_client

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context - end span."""
        if self._span:
            try:
                self._span.__exit__(exc_type, exc_val, exc_tb)
            except Exception as e:
                logger.debug("Failed to exit OTel span: %s", e, exc_info=True)
        return False


@contextmanager
def versioned_context(
    briefcase_client,
    repository: str,
    branch: str = "main",
    commit: str = "latest",
    **kwargs
):
    """
    Convenience function for versioned_context as a context manager.

    Usage:
        with versioned_context(client, "acme", "main") as versioned:
            data = versioned.read_object("file.pdf")
    """
    ctx = VersionedContextManager(
        briefcase_client,
        repository,
        branch,
        commit,
        **kwargs
    )

    with ctx as versioned_client:
        yield versioned_client


# Backwards compatibility aliases
LakeFSContextManager = VersionedContextManager
lakefs_context = versioned_context
