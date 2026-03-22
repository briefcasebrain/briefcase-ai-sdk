"""
briefcase.auto — Single-function global framework instrumentation.

Framework-specific auto-instrumentation (LangChain, LlamaIndex, CrewAI,
OpenAI Agents, AutoGen, AG2, PageIndex) is available in the enterprise
package: briefcase-ai-enterprise.

This module provides the interface stub so that calling code gets a clear
error message rather than an ImportError.
"""

from typing import Any, Optional


def auto(framework: str, **kwargs: Any) -> Any:
    """Instrument a framework globally.

    Requires briefcase-ai-enterprise for pre-built framework integrations.
    """
    raise ImportError(
        f"Auto-instrumentation for {framework!r} requires the enterprise package.\n"
        "Install it with: pip install briefcase-ai-enterprise"
    )


def undo(framework: Optional[str] = None) -> None:
    """Remove patches for one or all frameworks.

    Requires briefcase-ai-enterprise for pre-built framework integrations.
    """
    raise ImportError(
        "Auto-instrumentation requires the enterprise package.\n"
        "Install it with: pip install briefcase-ai-enterprise"
    )
