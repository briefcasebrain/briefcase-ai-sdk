"""briefcase MCP server (optional; requires the ``mcp`` extra).

    pip install briefcase-ai[mcp]
    briefcase-ai-mcp            # or: python -m briefcase.mcp
"""

__all__ = ["build_server", "main"]


def __getattr__(name):
    # Loaded on first use so `briefcase.mcp.__main__` can explain a missing `mcp` extra in one line.
    if name in __all__:
        from briefcase.mcp import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
