"""
Decorators for lakeFS versioned functions.
"""

import functools
from typing import Callable, Optional
import logging
import threading

from briefcase.integrations.lakefs.context import versioned_context

logger = logging.getLogger(__name__)

# Thread-local storage for passing briefcase client
_thread_local = threading.local()


def versioned(
    repository: str,
    branch: str = "main",
    commit: str = "latest",
    client_param: str = "versioned_client"
):
    """
    Decorator that provides versioned context to a function.

    The decorated function receives a VersionedClient as a parameter.
    All file accesses within the function are automatically versioned.

    Args:
        repository: Repository name
        branch: Branch name (default: "main")
        commit: Commit SHA or "latest" (default: "latest")
        client_param: Parameter name for injected client (default: "versioned_client")

    Usage:
        @versioned(repository="acme", branch="main")
        def evaluate_claim(claim_data, versioned_client):
            policy = versioned_client.read_object("policies/policy.pdf")
            return evaluate(claim_data, policy)

        # Call normally - versioned_client is injected
        result = evaluate_claim(claim_data)
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Get Briefcase client from thread-local or kwargs
            briefcase_client = _get_briefcase_client(kwargs)

            if briefcase_client is None:
                logger.warning(
                    "versioned decorator requires a Briefcase client. "
                    "Either pass 'briefcase_client' as kwarg or use within "
                    "a Briefcase instrumented context. Continuing without versioning."
                )
                # Continue without versioning - just call the function
                return func(*args, **kwargs)

            # Use versioned_context to inject client
            with versioned_context(
                briefcase_client,
                repository,
                branch,
                commit
            ) as versioned_client:
                # Inject as keyword argument
                kwargs[client_param] = versioned_client
                return func(*args, **kwargs)

        return wrapper
    return decorator


# Backwards compatibility alias
lakefs_versioned = versioned


def _get_briefcase_client(kwargs: dict):
    """Get Briefcase client from kwargs or thread-local."""
    # Try kwargs first
    if "briefcase_client" in kwargs:
        return kwargs.pop("briefcase_client")

    # Try thread-local
    try:
        return getattr(_thread_local, 'briefcase_client', None)
    except:
        return None


def set_briefcase_client(client):
    """Set the thread-local briefcase client for decorators."""
    _thread_local.briefcase_client = client


def clear_briefcase_client():
    """Clear the thread-local briefcase client."""
    if hasattr(_thread_local, 'briefcase_client'):
        delattr(_thread_local, 'briefcase_client')
