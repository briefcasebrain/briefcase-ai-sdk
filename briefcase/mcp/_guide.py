"""Locate the usage guide (``llms-full.txt``) served by the MCP server.

Kept free of the ``mcp`` dependency so it can be imported and tested without
the extra installed.
"""

from __future__ import annotations

from pathlib import Path

GUIDE_NAME = "llms-full.txt"
GUIDE_URL = (
    "https://github.com/briefcasebrain/briefcase-ai-sdk/blob/main/llms-full.txt"
)


def _packaged_guide() -> Path:
    # Next to this module: the copy shipped inside the wheel (listed in
    # [tool.maturin] include). Resolved from __file__ rather than
    # importlib.resources so that importing the briefcase.mcp package, which
    # needs the mcp extra, is not required just to find a data file.
    return Path(__file__).resolve().parent / GUIDE_NAME


def llms_full_text() -> str:
    """Return the usage guide.

    Looks for the copy packaged with ``briefcase.mcp`` first, then the one at
    the repository root (source checkouts), and falls back to a pointer to
    the online guide if neither can be read.
    """
    candidates = (
        _packaged_guide(),
        Path(__file__).resolve().parents[2] / GUIDE_NAME,
    )
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            return text
    return f"briefcase usage guide: {GUIDE_URL}"
