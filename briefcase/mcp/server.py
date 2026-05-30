"""briefcase MCP server — exposes safe SDK operations to AI agents.

Lets MCP-capable tools (Cursor, Claude Code, Codex, Replit, …) use briefcase
directly: sanitize PII, estimate model cost, analyze output drift, and read the
usage guide. Run with ``briefcase-mcp`` or ``python -m briefcase.mcp``.

Requires the ``mcp`` extra: ``pip install briefcase-ai[mcp]``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "briefcase.mcp requires the 'mcp' extra. "
        "Install it with: pip install briefcase-ai[mcp]"
    ) from exc


def _llms_full_text() -> str:
    """Return the bundled usage guide, or a pointer if it is not on disk."""
    candidate = Path(__file__).resolve().parents[2] / "llms-full.txt"
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return (
        "briefcase usage guide: "
        "https://github.com/briefcasebrain/briefcase-ai-sdk/blob/main/llms-full.txt"
    )


def build_server() -> FastMCP:
    """Construct the briefcase FastMCP server with its tools and resources."""
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
    def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> Dict[str, Any]:
        """Estimate the USD cost of an LLM call for a model and token counts."""
        from briefcase.cost import CostCalculator

        est = CostCalculator().estimate_cost(model, input_tokens, output_tokens)
        return {
            "model": model,
            "input_cost": est.input_cost,
            "output_cost": est.output_cost,
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
