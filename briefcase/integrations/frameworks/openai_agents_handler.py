"""OpenAI Agents SDK tracing integration for Briefcase.

Hooks into the openai-agents global tracing layer to capture agent runs,
tool calls, handoffs, guardrail evaluations, and model generations. Each
completed trace exports as one decision record with its spans attached.

Usage (global):
    from briefcase.integrations.frameworks import openai_agents_hook
    openai_agents_hook.install()

Usage (explicit):
    from briefcase.integrations.frameworks import OpenAIAgentsTracer
    tracer = OpenAIAgentsTracer(context_version="v2")
    from agents import add_trace_processor
    add_trace_processor(tracer)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from briefcase._export_mixin import ExportMixin
from briefcase._logging import get_logger

logger = get_logger(__name__)

_INSTALL_HINT = (
    "openai-agents is required for OpenAIAgentsTracer. "
    "Install with: pip install openai-agents  or  pip install briefcase-ai[openai-agents]"
)

# Optional dependency guard. The TracingProcessor base class is needed at
# class-definition time, so this import runs at module import with a fallback
# that keeps the module importable; the constructor raises when the package
# is absent.
try:
    from agents import (
        TracingProcessor,
        add_trace_processor as _agents_add_trace_processor,
        AgentSpanData,
        FunctionSpanData,
        HandoffSpanData,
        GuardrailSpanData,
        GenerationSpanData,
    )
    _AGENTS_AVAILABLE = True
    _TracingBase = TracingProcessor
except ImportError:
    _AGENTS_AVAILABLE = False
    _TracingBase = object  # fallback so the class body can be defined


# Global install tracking for idempotency
_INSTALLED_PROCESSOR: Optional["OpenAIAgentsTracer"] = None


def install(
    context_version: Optional[str] = None,
    async_capture: bool = True,
    exporter: Any = None,
) -> "OpenAIAgentsTracer":
    """Install OpenAIAgentsTracer globally into the agents tracing layer.

    Idempotent: calling install() again returns the same processor without
    double-registration.

    Args:
        context_version: Optional version tag added to all decision records.
        async_capture: If True (default), export is fire-and-forget.
        exporter: Briefcase exporter instance; falls back to the global config.

    Returns:
        The installed OpenAIAgentsTracer instance.

    Raises:
        ImportError: If openai-agents is not installed.
    """
    global _INSTALLED_PROCESSOR

    if not _AGENTS_AVAILABLE:
        raise ImportError(_INSTALL_HINT)

    if _INSTALLED_PROCESSOR is not None:
        return _INSTALLED_PROCESSOR

    processor = OpenAIAgentsTracer(
        context_version=context_version,
        async_capture=async_capture,
        exporter=exporter,
    )
    _agents_add_trace_processor(processor)
    _INSTALLED_PROCESSOR = processor
    return processor


class OpenAIAgentsTracer(ExportMixin, _TracingBase):  # type: ignore[valid-type,misc]
    """Briefcase TracingProcessor for the OpenAI Agents SDK.

    Captures:
    - Agent runs (AgentSpanData)
    - Tool calls (FunctionSpanData)
    - Agent handoffs (HandoffSpanData)
    - Guardrail evaluations (GuardrailSpanData)
    - Model generations (GenerationSpanData)

    Maps each trace to one decision record with nested spans, exported on
    trace completion. Never raises into agent execution; all errors are
    caught and logged at debug level.
    """

    def __init__(
        self,
        context_version: Optional[str] = None,
        async_capture: bool = True,
        exporter: Any = None,
    ):
        if not _AGENTS_AVAILABLE:
            raise ImportError(_INSTALL_HINT)

        self.context_version = context_version
        self.async_capture = async_capture
        self._exporter = exporter

        # Active trace records: trace_id to record dict
        self._traces: Dict[str, Dict[str, Any]] = {}
        # Trace start times: trace_id to datetime
        self._trace_starts: Dict[str, datetime] = {}
        # Span start times: span_id to datetime
        self._span_starts: Dict[str, datetime] = {}
        # Completed records (for inspection and testing)
        self._records: List[Dict[str, Any]] = []

    # TracingProcessor interface

    def on_trace_start(self, trace: Any) -> None:
        """Called when a new trace begins."""
        try:
            trace_id = trace.trace_id
            started = datetime.now(timezone.utc)
            record: Dict[str, Any] = {
                "decision_id": str(trace_id),
                "decision_type": "agent_trace",
                "function_name": trace.name,
                "inputs": {},
                "outputs": {},
                "started_at": started.isoformat(),
                "trace_id": trace_id,
                "name": trace.name,
                "spans": [],
            }
            if self.context_version:
                record["context_version"] = self.context_version
            self._traces[trace_id] = record
            self._trace_starts[trace_id] = started
        except Exception as e:
            logger.debug("OpenAIAgentsTracer: on_trace_start error: %s", e)

    def on_trace_end(self, trace: Any) -> None:
        """Called when a trace completes. Assembles the record and exports."""
        try:
            trace_id = trace.trace_id
            record = self._traces.pop(trace_id, None)
            started = self._trace_starts.pop(trace_id, None)
            if record is None:
                return

            ended = datetime.now(timezone.utc)
            record["ended_at"] = ended.isoformat()
            if started is not None:
                record["execution_time_ms"] = (ended - started).total_seconds() * 1000
            self._records.append(record)
            self._trigger_export(record)
        except Exception as e:
            logger.debug("OpenAIAgentsTracer: on_trace_end error: %s", e)

    def on_span_start(self, span: Any) -> None:
        """Called when a span begins. Records its start time."""
        try:
            self._span_starts[span.span_id] = datetime.now(timezone.utc)
        except Exception as e:
            logger.debug("OpenAIAgentsTracer: on_span_start error: %s", e)

    def on_span_end(self, span: Any) -> None:
        """Called when a span completes. Captures span data into the active trace."""
        try:
            span_id = span.span_id
            trace_id = span.trace_id
            started_at = self._span_starts.pop(span_id, None)
            ended_at = datetime.now(timezone.utc)

            span_record = self._build_span_record(span, started_at, ended_at)

            trace_record = self._traces.get(trace_id)
            if trace_record is not None:
                trace_record["spans"].append(span_record)
        except Exception as e:
            logger.debug("OpenAIAgentsTracer: on_span_end error: %s", e)

    def shutdown(self) -> None:
        """Clean up on application shutdown."""
        try:
            self._traces.clear()
            self._trace_starts.clear()
            self._span_starts.clear()
        except Exception as e:
            logger.debug("OpenAIAgentsTracer: shutdown error: %s", e)

    def force_flush(self) -> None:
        """No-op; this implementation keeps no internal buffer."""
        pass

    # Public inspection API

    def get_records(self) -> List[Dict[str, Any]]:
        """Return all completed trace records captured so far."""
        return list(self._records)

    def clear(self) -> None:
        """Clear all captured records and in-flight state."""
        self._records.clear()
        self._traces.clear()
        self._trace_starts.clear()
        self._span_starts.clear()

    # Internal helpers

    def _build_span_record(
        self,
        span: Any,
        started_at: Optional[datetime],
        ended_at: datetime,
    ) -> Dict[str, Any]:
        """Build a serializable dict from a completed span."""
        record: Dict[str, Any] = {
            "span_id": span.span_id,
            "trace_id": span.trace_id,
            "started_at": started_at.isoformat() if started_at else None,
            "ended_at": ended_at.isoformat(),
        }

        if started_at:
            delta = (ended_at - started_at).total_seconds()
            record["execution_time_ms"] = delta * 1000

        span_data = span.span_data
        if _AGENTS_AVAILABLE:
            if isinstance(span_data, AgentSpanData):
                record["type"] = "agent_run"
                record["agent_name"] = getattr(span_data, "name", None)
                record["tools"] = list(getattr(span_data, "tools", None) or [])
                record["handoffs"] = list(getattr(span_data, "handoffs", None) or [])
                record["output_type"] = getattr(span_data, "output_type", None)

            elif isinstance(span_data, FunctionSpanData):
                record["type"] = "tool_call"
                record["tool_name"] = getattr(span_data, "name", None)
                record["input"] = getattr(span_data, "input", None)
                record["output"] = getattr(span_data, "output", None)

            elif isinstance(span_data, HandoffSpanData):
                record["type"] = "handoff"
                record["from_agent"] = getattr(span_data, "from_agent", None)
                record["to_agent"] = getattr(span_data, "to_agent", None)

            elif isinstance(span_data, GuardrailSpanData):
                record["type"] = "guardrail"
                record["guardrail_name"] = getattr(span_data, "name", None)
                record["triggered"] = getattr(span_data, "triggered", False)

            elif isinstance(span_data, GenerationSpanData):
                record["type"] = "generation"
                record["model"] = getattr(span_data, "model", None)
                record["usage"] = getattr(span_data, "usage", None)
                record["input"] = getattr(span_data, "input", None)
                record["output"] = getattr(span_data, "output", None)

            else:
                record["type"] = getattr(span_data, "type", "unknown")

        error = getattr(span, "error", None)
        if error:
            record["error"] = str(error)

        return record
