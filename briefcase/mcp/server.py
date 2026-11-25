"""briefcase MCP server — exposes safe SDK operations to AI agents.

Lets MCP-capable tools (Cursor, Claude Code, Codex, Replit, …) use briefcase
directly: sanitize PII, estimate model cost, analyze output drift, and read the
usage guide. Run with ``briefcase-ai-mcp`` or ``python -m briefcase.mcp``.

It also answers questions about the user's own recorded decisions, read-only
(``briefcase.mcp.records``): search_decisions, get_decision, get_trace,
summarize_usage, explain_policy_decision, verify_records, replay_plan and
drift_check. None of them writes a file, changes configuration or calls a
model; recorded content is returned only with ``include_content=true``.

Requires the ``mcp`` extra: ``pip install briefcase-ai[mcp]``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from typing_extensions import Annotated

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


from pydantic import Field  # noqa: E402  (a dependency of mcp itself)


def _sdk_tool_error() -> Any:
    """The SDK's own tool error, which carries its message to the caller (mcp 2.x hides the
    text of any other exception). Same name in both majors."""
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:
        try:
            from mcp.server.mcpserver.exceptions import ToolError  # type: ignore[no-redef]
        except ImportError:  # pragma: no cover - a layout neither major ships
            return ValueError
    return ToolError


_SDKToolError: Any = _sdk_tool_error()

from briefcase.mcp._guide import llms_full_text as _llms_full_text  # noqa: E402


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

    _register_record_tools(server)

    @server.resource("briefcase://llms-full.txt")
    def llms_full() -> str:
        """The full briefcase usage guide."""
        return _llms_full_text()

    return server


#: The read-only tools over recorded decisions, in the order they are registered.
RECORD_TOOLS = (
    "search_decisions", "get_decision", "get_trace", "summarize_usage",
    "explain_policy_decision", "verify_records", "replay_plan", "drift_check",
)

_PATH_HELP = ("Optional records file (.jsonl). Default: the receiver's records "
              "(~/.briefcase/receiver/records.jsonl), BRIEFCASE_EXPORTER when it is a .jsonl file, and "
              "BRIEFCASE_MCP_RECORDS. Must be under the user's home or BRIEFCASE_MCP_ROOTS.")


# Module level: with ``from __future__ import annotations`` the MCP SDK evaluates the tool
# signatures' annotations in this module's globals.
Path_ = Annotated[Optional[str], Field(description=_PATH_HELP)]
Since = Annotated[Optional[str], Field(description="Start of the time range: ISO-8601 or an age like 24h, 7d.")]
Until = Annotated[Optional[str], Field(description="End of the time range (exclusive): ISO-8601 or an age.")]


@contextmanager
def _caller_errors() -> Iterator[None]:
    """A bad argument or refused path reaches the caller as a tool error with its message.
    Anything else is left to the SDK (mcp 2.x keeps an unexpected error's text on the server)."""
    from briefcase.mcp.records import ToolError

    try:
        yield
    except ToolError as exc:
        raise _SDKToolError(str(exc)) from None


def _register_record_tools(server: Any) -> None:
    from briefcase.mcp import records as R

    @server.tool()
    def search_decisions(
        since: Since = None,
        until: Until = None,
        decision_type: Annotated[Optional[str], Field(
            description="e.g. model_call, tool_call, policy.tool_call, user_prompt, llm, function. "
                        "Globs (*) allowed.")] = None,
        status: Annotated[Optional[str], Field(
            description="ok, error, rejected, refused, cancelled, no_result; for policy records allow, "
                        "ask or deny.")] = None,
        model: Annotated[Optional[str], Field(description="Model id; globs allowed (claude-*).")] = None,
        agent: Annotated[Optional[str], Field(
            description="Agent or source tool, e.g. claude_code, codex, cursor, langchain.")] = None,
        tool: Annotated[Optional[str], Field(description="Tool name of a tool call, e.g. Bash, Edit.")] = None,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        prompt_name: Optional[str] = None,
        prompt_version: Optional[str] = None,
        text: Annotated[Optional[str], Field(
            description="Substring matched against decision ids, function names, tool names and "
                        "trace/session/workflow ids (never against content).")] = None,
        limit: Annotated[int, Field(description="Rows per page, 1-200.", ge=1, le=200)] = 20,
        cursor: Annotated[Optional[str], Field(
            description="next_cursor from the previous page; omit for the first page.")] = None,
        path: Path_ = None,
    ) -> Dict[str, Any]:
        """Find recorded decisions (model calls, tool calls, policy decisions, captured functions).

        Returns compact rows, newest first: id, type, function, status, time, duration, model,
        tool, agent, session/trace/workflow ids, tokens, cost and whether content was captured.
        Never returns prompt or response content; use get_decision for one record.
        Page with next_cursor. All filters are optional and combine with AND.
        """
        with _caller_errors():
            return R.search_decisions(
                path=path, limit=limit, cursor=cursor, since=since, until=until, decision_type=decision_type,
                status=status, model=model, agent=agent, tool=tool, session_id=session_id, trace_id=trace_id,
                workflow_id=workflow_id, prompt_name=prompt_name, prompt_version=prompt_version, text=text)

    @server.tool()
    def get_decision(
        decision_id: Annotated[str, Field(description="The id from search_decisions.")],
        include_content: Annotated[bool, Field(
            description="Return the recorded inputs/outputs/error. Off by default: they can hold "
                        "prompts, responses and tool input. Only set it when the user asked to see "
                        "the content.")] = False,
        content_chars: Annotated[int, Field(
            description="Cap per string when include_content is true (100-20000).", ge=100, le=20000)] = 2000,
        path: Path_ = None,
    ) -> Dict[str, Any]:
        """One recorded decision: its metadata, and whether content was captured.

        Content (prompt, response, tool input/output) is returned only with include_content=true,
        and only if it was captured; otherwise the reply says so.
        """
        with _caller_errors():
            return R.get_decision(decision_id, path=path, include_content=include_content,
                                  content_chars=content_chars)

    @server.tool()
    def get_trace(
        id: Annotated[str, Field(description="A trace_id, session_id, workflow_id or decision id.")],
        kind: Annotated[str, Field(
            description="auto (try trace, session, workflow, then decision), trace, session, workflow "
                        "or decision.")] = "auto",
        max_nodes: Annotated[int, Field(description="Most nodes to return, 1-500.", ge=1, le=500)] = 200,
        path: Path_ = None,
    ) -> Dict[str, Any]:
        """All decisions of one trace, session, workflow or decision as an ordered tree.

        Each node has its offset and duration in ms, tokens and cost (recorded, or estimated from
        briefcase.pricing, labelled as such). A provider record nested under a framework LLM run
        (parent_run_id) is the same model call: totals count it once.
        """
        with _caller_errors():
            return R.get_trace(id, kind=kind, path=path, max_nodes=max_nodes)

    @server.tool()
    def summarize_usage(
        group_by: Annotated[str, Field(description="model, tool, day, type or agent.")] = "model",
        since: Since = None,
        until: Until = None,
        decision_type: Optional[str] = None,
        model: Optional[str] = None,
        agent: Optional[str] = None,
        tool: Optional[str] = None,
        limit: Annotated[int, Field(description="Most buckets to return, 1-200.", ge=1, le=200)] = 50,
        path: Path_ = None,
    ) -> Dict[str, Any]:
        """Count, errors, tokens and cost per model, tool, day, decision type or agent.

        Costs the records carry are reported as cost_recorded_usd; costs computed here from token
        counts with briefcase.pricing as cost_estimated_usd; calls that cannot be priced are
        counted as unpriced (with the models listed) and add nothing.
        """
        with _caller_errors():
            return R.summarize_usage(path=path, group_by=group_by, since=since, until=until, limit=limit,
                                     decision_type=decision_type, model=model, agent=agent, tool=tool)

    @server.tool()
    def explain_policy_decision(
        decision_id: Annotated[str, Field(description="Id of a policy.tool_call record.")],
        path: Path_ = None,
    ) -> Dict[str, Any]:
        """Why a coding tool's call was allowed, asked about or denied by `briefcase-ai policy`.

        Says which rule fired, from which policy file, its reason, and the mode (observe only
        logs). Compares with the policy on this machine today: whether the rule still exists and,
        when the call's target was captured, what today's policy would decide.
        """
        with _caller_errors():
            return R.explain_policy_decision(decision_id, path=path)

    @server.tool()
    def verify_records(
        path: Annotated[str, Field(
            description="A hash-chain .jsonl file (JsonlHashChainStore) or a bitemporal SQLite "
                        ".db/.sqlite store.")],
        records_path: Annotated[Optional[str], Field(
            description="Optional decision-records .jsonl whose payload hashes to check against "
                        "the chain.")] = None,
    ) -> Dict[str, Any]:
        """Check that stored records were not edited, removed or reordered.

        Recomputes every chain entry's hash and its link to the previous entry, and (with
        records_path) each record's payload hash. Replies with ok, a plain verdict and the first
        problems found. Read-only.
        """
        with _caller_errors():
            return R.verify_records(path, records_path=records_path)

    @server.tool()
    def replay_plan(
        path: Annotated[str, Field(description="Records .jsonl written with capture_content on.")],
        decision_ids: Annotated[Optional[List[str]], Field(
            description="Only these records; omit for all.")] = None,
        limit: Annotated[int, Field(description="Most calls to plan, 1-200.", ge=1, le=200)] = 50,
    ) -> Dict[str, Any]:
        """Plan a replay of recorded provider calls WITHOUT running it.

        For each record: replayable or not and why; for the plan: call count, models and an
        estimate of the tokens a live replay would use. Nothing is executed and no model is
        called; the user runs `briefcase-ai replay run` themselves.
        """
        with _caller_errors():
            return R.replay_plan(path, decision_ids=decision_ids, limit=limit)

    @server.tool()
    def drift_check(
        baseline: Annotated[str, Field(
            description="Name of a saved baseline (briefcase-ai drift baselines) or a profile .json path.")],
        since: Since = None,
        until: Until = None,
        last: Annotated[Optional[int], Field(description="Only the last N records.", ge=1)] = None,
        path: Path_ = None,
    ) -> Dict[str, Any]:
        """Compare recent records with a saved baseline profile (briefcase-ai drift).

        Returns each signal (latency, tokens, cost, error and refusal rates, model mix, ...) with
        its state (ok, attention, insufficient_data, unavailable) and a plain reading.
        """
        with _caller_errors():
            return R.drift_check(baseline, path=path, since=since, until=until, last=last)


def main() -> None:
    """Entry point for the ``briefcase-ai-mcp`` console script (stdio transport)."""
    build_server().run()


if __name__ == "__main__":
    main()
