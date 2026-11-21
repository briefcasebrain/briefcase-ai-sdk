"""briefcase MCP server — exposes safe SDK operations to AI agents.

Lets MCP-capable tools (Cursor, Claude Code, Codex, Replit, …) use briefcase
directly: sanitize PII, estimate model cost, analyze output drift, and read the
usage guide. Run with ``briefcase-mcp`` or ``python -m briefcase.mcp``.

Requires the ``mcp`` extra: ``pip install briefcase-ai[mcp]``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    # mcp 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:
    # mcp 2.0 renamed FastMCP to mcp.server.mcpserver.MCPServer. The surface
    # this module uses -- the constructor, the tool() and resource()
    # decorators, list_tools / list_resources / call_tool and run() -- is the
    # same in both (verified against mcp 1.x and mcp 2.3.0), so 2.x is
    # supported by importing the class under its new name.
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        # Distinguish "not installed" from "installed but unusable", so a
        # user who already has mcp is not told to install it.
        _installed: Optional[str]
        try:
            import mcp  # noqa: F401  (presence check only)
            from importlib.metadata import PackageNotFoundError, version

            try:
                _installed = version("mcp")
            except PackageNotFoundError:  # pragma: no cover - no metadata
                _installed = "unknown"
        except ImportError:
            _installed = None

        if _installed is None:
            raise ImportError(
                "briefcase.mcp requires the 'mcp' extra. "
                "Install it with: pip install briefcase-ai[mcp]"
            ) from exc
        raise ImportError(
            f"briefcase.mcp found mcp {_installed}, which provides neither "
            "mcp.server.fastmcp.FastMCP (mcp 1.x) nor "
            "mcp.server.mcpserver.MCPServer (mcp 2.x). Install a supported "
            'release with: pip install "briefcase-ai[mcp]"'
        ) from exc


from briefcase.mcp._guide import llms_full_text as _llms_full_text


def build_server() -> FastMCP:
    """Construct the briefcase MCP server with its tools and resources.

    The returned object is ``FastMCP`` on mcp 1.x and ``MCPServer`` on mcp 2.x.
    """
    server = FastMCP("briefcase")

    @server.tool()
    def sanitize_text(text: str) -> Dict[str, Any]:
        """Redact PII (emails, phones, SSNs, cards, API keys, IPs) from text.

        Returns the sanitized text and the list of redaction types found.
        """
        from briefcase.sanitize import Sanitizer

        result = Sanitizer().sanitize(text)
        return {
            "sanitized": result.sanitized,
            "redactions": [r.pii_type for r in result.redactions],
        }

    @server.tool()
    def estimate_cost(
        model: str,
        input_tokens: int,
        output_tokens: int,
        rate_card: str | None = None,
    ) -> Dict[str, Any]:
        """Estimate the USD cost of an LLM call for a model and token counts.

        Pass an optional ``rate_card`` (e.g. "batch", "bedrock:batch",
        "first_party:fast") to price under a platform/tier/modifier scheme;
        omit it for first-party standard pricing.
        """
        from briefcase.cost import CostCalculator

        est = CostCalculator().estimate_cost(
            model, input_tokens, output_tokens, rate_card=rate_card
        )
        return {
            "model": model,
            "rate_card": rate_card or "standard",
            "input_cost": est.input_cost,
            "output_cost": est.output_cost,
            "cache_cost": est.cache_cost,
            "total_cost": est.total_cost,
        }

    @server.tool()
    def analyze_drift(outputs: List[str]) -> Dict[str, Any]:
        """Analyze a list of model outputs for consistency / drift."""
        from briefcase.drift import DriftCalculator

        calc = DriftCalculator()
        metrics = calc.calculate_drift(outputs)
        return {
            "consistency_score": metrics.consistency_score,
            "agreement_rate": metrics.agreement_rate,
            "consensus_output": metrics.consensus_output,
            "status": metrics.get_status(calc),
        }

    @server.tool()
    def how_to(topic: str = "") -> str:
        """Return briefcase usage guidance.

        Pass a topic keyword (e.g. ``"export"``, ``"sanitize"``, ``"cost"``,
        ``"logging"``) to get matching sections, or empty for the full guide.
        """
        full = _llms_full_text()
        if not topic:
            return full
        sections = full.split("\n## ")
        matched = [s for s in sections if topic.lower() in s.lower()]
        return ("## " + "\n## ".join(matched)) if matched else full

    @server.resource("briefcase://llms-full.txt")
    def llms_full() -> str:
        """The full briefcase usage guide."""
        return _llms_full_text()

    return server


def main() -> None:
    """Entry point for the ``briefcase-mcp`` console script (stdio transport)."""
    build_server().run()


if __name__ == "__main__":
    main()
