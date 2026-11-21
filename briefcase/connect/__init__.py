"""``briefcase connect``: write each coding tool's config so it reports to briefcase.

The user-facing entry point is the CLI (``briefcase connect``); this package
holds the adapters, the key-level edit engine and the MCP install payloads.
"""

from briefcase.connect.adapters import DEFAULT_ENDPOINT, Adapter, Context, registry
from briefcase.connect.edits import Command, FileEdit, Manual

__all__ = ["Adapter", "Command", "Context", "DEFAULT_ENDPOINT", "FileEdit", "Manual", "registry"]
