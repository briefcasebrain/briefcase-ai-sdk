"""briefcase MCP server (optional; requires the ``mcp`` extra).

    pip install briefcase-ai[mcp]
    briefcase-mcp            # or: python -m briefcase.mcp
"""

from briefcase.mcp.server import build_server, main

__all__ = ["build_server", "main"]
