"""CrewAI event-bus integration for Briefcase.

Subclasses BaseEventListener to capture crew, agent, task, tool, LLM,
knowledge, memory, guardrail, and flow events with start/end pairing.
Completed records export through the configured exporter.

Usage (instantiating auto-registers on the global event bus):
    from briefcase.integrations.frameworks import CrewAIEventListener
    listener = CrewAIEventListener(context_version="v2")

Usage (explicit registration):
    from crewai.events import crewai_event_bus
    listener = CrewAIEventListener()
    listener.setup_listeners(crewai_event_bus)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from briefcase._export_mixin import ExportMixin
from briefcase._logging import get_logger
from briefcase.integrations.frameworks._capture_state import (
    DEFAULT_INFLIGHT_TTL_SECONDS,
    DEFAULT_MAX_INFLIGHT,
    DEFAULT_MAX_RECORDS,
    CaptureOptions,
    InflightStore,
    LLMRunScopes,
    bounded_records,
)

logger = get_logger(__name__)

_INSTALL_HINT = (
    "crewai is required for CrewAIEventListener. "
    "Install with: pip install crewai  or  pip install briefcase-ai[crewai]"
)

# Optional dependency guard. The base class and event types are needed at
# class-definition time, so this import runs at module import with a fallback
# that keeps the module importable; the constructor raises when crewai is
# absent.

try:
    # crewai >= 1.x exposes the event surface at crewai.events; earlier
    # releases used crewai.utilities.events. Try modern first.
    try:
        from crewai.events import (  # type: ignore[attr-defined]
            BaseEventListener,
            crewai_event_bus as _crewai_bus,
            CrewKickoffStartedEvent,
            CrewKickoffCompletedEvent,
            CrewKickoffFailedEvent,
            AgentExecutionStartedEvent,
            AgentExecutionCompletedEvent,
            AgentExecutionErrorEvent,
            TaskStartedEvent,
            TaskCompletedEvent,
            TaskFailedEvent,
            ToolUsageStartedEvent,
            ToolUsageFinishedEvent,
            ToolUsageErrorEvent,
            LLMCallStartedEvent,
            LLMCallCompletedEvent,
            LLMCallFailedEvent,
        )
    except ImportError:
        from crewai.utilities.events import BaseEventListener, crewai_event_bus as _crewai_bus

        from crewai.utilities.events.base_events import (  # type: ignore[attr-defined]
            CrewKickoffStartedEvent,
            CrewKickoffCompletedEvent,
            CrewKickoffFailedEvent,
            AgentExecutionStartedEvent,
            AgentExecutionCompletedEvent,
            AgentExecutionErrorEvent,
            TaskStartedEvent,
            TaskCompletedEvent,
            TaskFailedEvent,
            ToolUsageStartedEvent,
            ToolUsageFinishedEvent,
            ToolUsageErrorEvent,
            LLMCallStartedEvent,
            LLMCallCompletedEvent,
            LLMCallFailedEvent,
        )
    _CREWAI_AVAILABLE = True
    _ListenerBase = BaseEventListener
except ImportError:
    _CREWAI_AVAILABLE = False
    _ListenerBase = object  # fallback so the class body can be defined
    _crewai_bus = None

    # Placeholder event classes so the class body can reference them
    CrewKickoffStartedEvent = None  # type: ignore
    CrewKickoffCompletedEvent = None  # type: ignore
    CrewKickoffFailedEvent = None  # type: ignore
    AgentExecutionStartedEvent = None  # type: ignore
    AgentExecutionCompletedEvent = None  # type: ignore
    AgentExecutionErrorEvent = None  # type: ignore
    TaskStartedEvent = None  # type: ignore
    TaskCompletedEvent = None  # type: ignore
    TaskFailedEvent = None  # type: ignore
    ToolUsageStartedEvent = None  # type: ignore
    ToolUsageFinishedEvent = None  # type: ignore
    ToolUsageErrorEvent = None  # type: ignore
    LLMCallStartedEvent = None  # type: ignore
    LLMCallCompletedEvent = None  # type: ignore
    LLMCallFailedEvent = None  # type: ignore


class CrewAIEventListener(ExportMixin, _ListenerBase):  # type: ignore[valid-type,misc]
    """Briefcase BaseEventListener for the CrewAI event bus.

    Captures events across CrewAI execution phases:
    - Crew lifecycle (kickoff)
    - Agent execution
    - Task lifecycle
    - Tool usage
    - LLM calls
    - Knowledge retrieval
    - Memory operations
    - Guardrail evaluations
    - Flow steps

    Pairs start/completed/failed events with composite keys to compute
    execution_time_ms, then exports each completed record.
    """

    def __init__(
        self,
        context_version: Optional[str] = None,
        async_capture: bool = True,
        capture_crews: bool = True,
        capture_agents: bool = True,
        capture_tasks: bool = True,
        capture_tools: bool = True,
        capture_llm: bool = True,
        capture_knowledge: bool = True,
        capture_memory: bool = True,
        capture_guardrails: bool = True,
        capture_flows: bool = True,
        max_input_chars: int = 10000,
        max_output_chars: int = 10000,
        exporter: Any = None,
        capture_content: Union[bool, str, None] = "none",
        redact: Optional[Callable[[str], str]] = None,
        max_records: Optional[int] = DEFAULT_MAX_RECORDS,
        max_inflight: Optional[int] = DEFAULT_MAX_INFLIGHT,
        inflight_ttl_seconds: Optional[float] = DEFAULT_INFLIGHT_TTL_SECONDS,
    ):
        if not _CREWAI_AVAILABLE:
            raise ImportError(_INSTALL_HINT)

        self.context_version = context_version
        self.async_capture = async_capture
        self.capture_crews = capture_crews
        self.capture_agents = capture_agents
        self.capture_tasks = capture_tasks
        self.capture_tools = capture_tools
        self.capture_llm = capture_llm
        self.capture_knowledge = capture_knowledge
        self.capture_memory = capture_memory
        self.capture_guardrails = capture_guardrails
        self.capture_flows = capture_flows
        self.max_input_chars = max_input_chars
        self.max_output_chars = max_output_chars
        self._exporter = exporter

        self._capture_options = CaptureOptions(capture_content, redact)
        self.max_records = max_records
        # Completed records: a ring buffer that applies the capture options
        # to each record as it is appended.
        self._records = bounded_records(max_records, self._capture_options)
        # Inflight pairing: (category, key) to (start_record, start_time).
        # Capped and expiring, so a start event with no completion cannot leak.
        # Publishes open LLM calls to provider auto-instrumentation. Only
        # effective when the event bus runs this listener in the thread or
        # task that makes the model call.
        self._llm_scopes = LLMRunScopes("crewai")
        self._inflight = InflightStore(
            max_size=max_inflight,
            ttl_seconds=inflight_ttl_seconds,
            on_evict=self._on_inflight_evicted,
        )

        # Register with the global event bus
        if _crewai_bus is not None:
            self.setup_listeners(_crewai_bus)

    def setup_listeners(self, crewai_event_bus: Any) -> None:
        """Register all event handlers on the provided event bus."""
        if not _CREWAI_AVAILABLE:
            return

        if self.capture_crews:
            crewai_event_bus.on(CrewKickoffStartedEvent)(self._on_crew_kickoff_started)
            crewai_event_bus.on(CrewKickoffCompletedEvent)(self._on_crew_kickoff_completed)
            crewai_event_bus.on(CrewKickoffFailedEvent)(self._on_crew_kickoff_failed)

        if self.capture_agents:
            crewai_event_bus.on(AgentExecutionStartedEvent)(self._on_agent_started)
            crewai_event_bus.on(AgentExecutionCompletedEvent)(self._on_agent_completed)
            crewai_event_bus.on(AgentExecutionErrorEvent)(self._on_agent_error)

        if self.capture_tasks:
            crewai_event_bus.on(TaskStartedEvent)(self._on_task_started)
            crewai_event_bus.on(TaskCompletedEvent)(self._on_task_completed)
            crewai_event_bus.on(TaskFailedEvent)(self._on_task_failed)

        if self.capture_tools:
            crewai_event_bus.on(ToolUsageStartedEvent)(self._on_tool_started)
            crewai_event_bus.on(ToolUsageFinishedEvent)(self._on_tool_finished)
            crewai_event_bus.on(ToolUsageErrorEvent)(self._on_tool_error)

        if self.capture_llm:
            crewai_event_bus.on(LLMCallStartedEvent)(self._on_llm_started)
            crewai_event_bus.on(LLMCallCompletedEvent)(self._on_llm_completed)
            crewai_event_bus.on(LLMCallFailedEvent)(self._on_llm_failed)

        # Optional event families; absent in some crewai versions
        if self.capture_knowledge:
            self._register_optional_events(crewai_event_bus, [
                ("KnowledgeRetrievalStartedEvent", self._on_knowledge_started),
                ("KnowledgeRetrievalCompletedEvent", self._on_knowledge_completed),
                ("KnowledgeQueryStartedEvent", self._on_knowledge_query_started),
                ("KnowledgeQueryCompletedEvent", self._on_knowledge_query_completed),
                ("KnowledgeQueryFailedEvent", self._on_knowledge_query_failed),
                ("KnowledgeSearchQueryFailedEvent", self._on_knowledge_search_failed),
            ])

        if self.capture_memory:
            self._register_optional_events(crewai_event_bus, [
                ("MemoryQueryStartedEvent", self._on_memory_query_started),
                ("MemoryQueryCompletedEvent", self._on_memory_query_completed),
                ("MemoryQueryFailedEvent", self._on_memory_query_failed),
                ("MemorySaveStartedEvent", self._on_memory_save_started),
                ("MemorySaveCompletedEvent", self._on_memory_save_completed),
                ("MemorySaveFailedEvent", self._on_memory_save_failed),
                ("MemoryRetrievalStartedEvent", self._on_memory_retrieval_started),
                ("MemoryRetrievalCompletedEvent", self._on_memory_retrieval_completed),
            ])

        if self.capture_guardrails:
            self._register_optional_events(crewai_event_bus, [
                ("LLMGuardrailStartedEvent", self._on_guardrail_started),
                ("LLMGuardrailCompletedEvent", self._on_guardrail_completed),
            ])

        if self.capture_flows:
            self._register_optional_events(crewai_event_bus, [
                ("FlowCreatedEvent", self._on_flow_created),
                ("FlowStartedEvent", self._on_flow_started),
                ("FlowFinishedEvent", self._on_flow_finished),
                ("FlowPlotEvent", self._on_flow_plot),
                ("MethodExecutionStartedEvent", self._on_method_started),
                ("MethodExecutionFinishedEvent", self._on_method_finished),
                ("MethodExecutionFailedEvent", self._on_method_failed),
            ])

    def _register_optional_events(
        self,
        bus: Any,
        event_handlers: List[Tuple[str, Any]],
    ) -> None:
        """Register optional event types that exist only in some crewai versions."""
        try:
            import crewai.events as _ev_module  # type: ignore[import-not-found]
        except ImportError:
            try:
                import crewai.utilities.events.base_events as _ev_module
            except ImportError:
                return
        for event_name, handler in event_handlers:
            try:
                event_cls = getattr(_ev_module, event_name, None)
                if event_cls is not None:
                    bus.on(event_cls)(handler)
            except Exception as exc:
                logger.warning(
                    "crewai: could not subscribe to optional event %s; records for it will not be captured: %s",
                    event_name, exc,
                )

    def _on_inflight_evicted(self, key: Any, entry: Any, reason: str) -> None:
        """Record a start event whose completion never arrived as abandoned."""
        record, _start_time = entry
        record["ended_at"] = datetime.now(timezone.utc).isoformat()
        record["error"] = f"abandoned: no completion event ({reason})"
        self._records.append(record)
        self._trigger_export(record)

    # Public API

    def get_records(self) -> List[Dict[str, Any]]:
        """Return all captured decision records."""
        return list(self._records)

    def clear(self) -> None:
        """Clear all captured records and inflight state."""
        self._records.clear()
        self._inflight.clear()

    @property
    def decision_count(self) -> int:
        """Number of captured decision records."""
        return len(self._records)

    # Crew event handlers

    def _on_crew_kickoff_started(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "crew_id", "crew", "name")
            record = self._build_start_record("crew_kickoff", key, event)
            self._inflight[("crew", key)] = (record, datetime.now(timezone.utc))
        except Exception as exc:
            _warn_lost("start", "crew_kickoff", exc)

    def _on_crew_kickoff_completed(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "crew_id", "crew", "name")
            self._complete("crew", key, event, "crew_kickoff")
        except Exception as exc:
            _warn_lost("completion", "crew_kickoff", exc)

    def _on_crew_kickoff_failed(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "crew_id", "crew", "name")
            self._fail("crew", key, event, "crew_kickoff")
        except Exception as exc:
            _warn_lost("failure", "crew_kickoff", exc)

    # Agent event handlers

    def _on_agent_started(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "agent_id", "agent", "role")
            record = self._build_start_record("agent_execution", key, event)
            self._inflight[("agent", key)] = (record, datetime.now(timezone.utc))
        except Exception as exc:
            _warn_lost("start", "agent_execution", exc)

    def _on_agent_completed(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "agent_id", "agent", "role")
            self._complete("agent", key, event, "agent_execution")
        except Exception as exc:
            _warn_lost("completion", "agent_execution", exc)

    def _on_agent_error(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "agent_id", "agent", "role")
            self._fail("agent", key, event, "agent_execution")
        except Exception as exc:
            _warn_lost("failure", "agent_execution", exc)

    # Task event handlers

    def _on_task_started(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "task_id", "task", "description")
            record = self._build_start_record("task", key, event)
            self._inflight[("task", key)] = (record, datetime.now(timezone.utc))
        except Exception as exc:
            _warn_lost("start", "task", exc)

    def _on_task_completed(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "task_id", "task", "description")
            self._complete("task", key, event, "task")
        except Exception as exc:
            _warn_lost("completion", "task", exc)

    def _on_task_failed(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "task_id", "task", "description")
            self._fail("task", key, event, "task")
        except Exception as exc:
            _warn_lost("failure", "task", exc)

    # Tool event handlers

    def _on_tool_started(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "tool_name", "tool", "name")
            record = self._build_start_record("tool_usage", key, event)
            self._inflight[("tool", key)] = (record, datetime.now(timezone.utc))
        except Exception as exc:
            _warn_lost("start", "tool_usage", exc)

    def _on_tool_finished(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "tool_name", "tool", "name")
            self._complete("tool", key, event, "tool_usage")
        except Exception as exc:
            _warn_lost("completion", "tool_usage", exc)

    def _on_tool_error(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "tool_name", "tool", "name")
            self._fail("tool", key, event, "tool_usage")
        except Exception as exc:
            _warn_lost("failure", "tool_usage", exc)

    # LLM event handlers

    def _on_llm_started(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "call_id", "model", "model_name")
            record = self._build_start_record("llm_call", key, event)
            self._inflight[("llm", key)] = (record, datetime.now(timezone.utc))
            self._llm_scopes.start(key)
        except Exception as exc:
            _warn_lost("start", "llm_call", exc)

    def _on_llm_completed(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "call_id", "model", "model_name")
            if self._llm_scopes.finish(key):
                # auto(dedupe="framework"): a provider record stands for this call.
                self._inflight.pop(("llm", key), None)
                return
            self._complete("llm", key, event, "llm_call")
        except Exception as exc:
            _warn_lost("completion", "llm_call", exc)

    def _on_llm_failed(self, event: Any) -> None:
        try:
            key = _extract_event_key(event, "call_id", "model", "model_name")
            if self._llm_scopes.finish(key):
                self._inflight.pop(("llm", key), None)
                return
            self._fail("llm", key, event, "llm_call")
        except Exception as exc:
            _warn_lost("failure", "llm_call", exc)

    # Knowledge event handlers

    def _on_knowledge_started(self, event: Any) -> None:
        self._generic_start("knowledge", "knowledge_retrieval", event, "query_id", "query")

    def _on_knowledge_completed(self, event: Any) -> None:
        self._generic_complete("knowledge", "knowledge_retrieval", event)

    def _on_knowledge_query_started(self, event: Any) -> None:
        self._generic_start("knowledge_query", "knowledge_query", event, "query_id", "query")

    def _on_knowledge_query_completed(self, event: Any) -> None:
        self._generic_complete("knowledge_query", "knowledge_query", event)

    def _on_knowledge_query_failed(self, event: Any) -> None:
        self._generic_fail("knowledge_query", "knowledge_query", event)

    def _on_knowledge_search_failed(self, event: Any) -> None:
        try:
            record = self._build_event_record("knowledge_search_failed", event)
            self._records.append(record)
            self._trigger_export(record)
        except Exception as exc:
            logger.warning(
                "crewai: knowledge_search_failed record was not captured: %s", exc,
            )

    # Memory event handlers

    def _on_memory_query_started(self, event: Any) -> None:
        self._generic_start("memory_query", "memory_query", event, "query_id", "query")

    def _on_memory_query_completed(self, event: Any) -> None:
        self._generic_complete("memory_query", "memory_query", event)

    def _on_memory_query_failed(self, event: Any) -> None:
        self._generic_fail("memory_query", "memory_query", event)

    def _on_memory_save_started(self, event: Any) -> None:
        self._generic_start("memory_save", "memory_save", event, "item_id", "key")

    def _on_memory_save_completed(self, event: Any) -> None:
        self._generic_complete("memory_save", "memory_save", event)

    def _on_memory_save_failed(self, event: Any) -> None:
        self._generic_fail("memory_save", "memory_save", event)

    def _on_memory_retrieval_started(self, event: Any) -> None:
        self._generic_start("memory_retrieval", "memory_retrieval", event, "query_id", "query")

    def _on_memory_retrieval_completed(self, event: Any) -> None:
        self._generic_complete("memory_retrieval", "memory_retrieval", event)

    # Guardrail event handlers

    def _on_guardrail_started(self, event: Any) -> None:
        self._generic_start("guardrail", "llm_guardrail", event, "guardrail_id", "name")

    def _on_guardrail_completed(self, event: Any) -> None:
        self._generic_complete("guardrail", "llm_guardrail", event)

    # Flow event handlers

    def _on_flow_created(self, event: Any) -> None:
        try:
            record = self._build_event_record("flow_created", event)
            self._records.append(record)
        except Exception as exc:
            logger.warning(
                "crewai: flow_created record was not captured: %s", exc,
            )

    def _on_flow_started(self, event: Any) -> None:
        self._generic_start("flow", "flow", event, "flow_id", "name")

    def _on_flow_finished(self, event: Any) -> None:
        self._generic_complete("flow", "flow", event)

    def _on_flow_plot(self, event: Any) -> None:
        try:
            record = self._build_event_record("flow_plot", event)
            self._records.append(record)
        except Exception as exc:
            logger.warning(
                "crewai: flow_plot record was not captured: %s", exc,
            )

    def _on_method_started(self, event: Any) -> None:
        self._generic_start("method", "method_execution", event, "method_id", "method_name")

    def _on_method_finished(self, event: Any) -> None:
        self._generic_complete("method", "method_execution", event)

    def _on_method_failed(self, event: Any) -> None:
        self._generic_fail("method", "method_execution", event)

    # Generic pairing helpers

    def _generic_start(
        self, category: str, decision_type: str, event: Any, *key_attrs: str
    ) -> None:
        try:
            key = _extract_event_key(event, *key_attrs)
            record = self._build_start_record(decision_type, key, event)
            self._inflight[(category, key)] = (record, datetime.now(timezone.utc))
        except Exception as exc:
            _warn_lost("start", decision_type, exc)

    def _generic_complete(
        self, category: str, decision_type: str, event: Any, *key_attrs: str
    ) -> None:
        try:
            # Build the key from the same attrs as start, or scan inflight
            key = _extract_event_key(event)
            if (category, key) not in self._inflight:
                candidates = [k for k in self._inflight if k[0] == category]
                if candidates:
                    key = candidates[0][1]
            self._complete(category, key, event, decision_type)
        except Exception as exc:
            _warn_lost("completion", decision_type, exc)

    def _generic_fail(
        self, category: str, decision_type: str, event: Any
    ) -> None:
        try:
            key = _extract_event_key(event)
            if (category, key) not in self._inflight:
                candidates = [k for k in self._inflight if k[0] == category]
                if candidates:
                    key = candidates[0][1]
            self._fail(category, key, event, decision_type)
        except Exception as exc:
            _warn_lost("failure", decision_type, exc)

    def _complete(
        self, category: str, key: str, event: Any, decision_type: str
    ) -> None:
        """Pop the inflight entry, finalize the record, and append."""
        inflight_key = (category, key)
        inflight = self._inflight.pop(inflight_key, None)

        if inflight is None:
            # No matching start event; create a standalone record
            record = self._build_event_record(decision_type, event, key)
            self._records.append(record)
            self._trigger_export(record)
            return

        record, start_time = inflight
        end_time = datetime.now(timezone.utc)
        record["ended_at"] = end_time.isoformat()
        record["execution_time_ms"] = (end_time - start_time).total_seconds() * 1000
        record["outputs"] = _extract_outputs(event, self.max_output_chars)
        self._records.append(record)
        self._trigger_export(record)

    def _fail(
        self, category: str, key: str, event: Any, decision_type: str
    ) -> None:
        """Pop the inflight entry, mark it as an error, and append."""
        inflight_key = (category, key)
        inflight = self._inflight.pop(inflight_key, None)

        if inflight is None:
            record = self._build_event_record(decision_type, event, key)
            record["error"] = _safe_str(getattr(event, "error", str(event)))
            self._records.append(record)
            self._trigger_export(record)
            return

        record, start_time = inflight
        end_time = datetime.now(timezone.utc)
        record["ended_at"] = end_time.isoformat()
        record["execution_time_ms"] = (end_time - start_time).total_seconds() * 1000
        record["error"] = _safe_str(getattr(event, "error", str(event)))
        self._records.append(record)
        self._trigger_export(record)

    # Record builders

    def _build_start_record(
        self, decision_type: str, key: str, event: Any
    ) -> Dict[str, Any]:
        """Build an in-progress record when a start event fires."""
        record: Dict[str, Any] = {
            "decision_id": str(uuid.uuid4()),
            "decision_type": decision_type,
            "function_name": key,
            "inputs": _extract_inputs(event, self.max_input_chars),
            "outputs": {},
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.context_version is not None:
            record["context_version"] = self.context_version
        return record

    def _build_event_record(
        self, decision_type: str, event: Any, key: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build a standalone record for an event with no paired start.

        Carries started_at (the capture instant) but no ended_at or
        execution_time_ms, since the true duration is unknown.
        """
        record: Dict[str, Any] = {
            "decision_id": str(uuid.uuid4()),
            "decision_type": decision_type,
            "function_name": key if key is not None else _extract_event_key(event),
            "inputs": _extract_inputs(event, self.max_input_chars),
            "outputs": {},
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.context_version is not None:
            record["context_version"] = self.context_version
        return record


# Module-level helpers

def require_crewai() -> None:
    """Raise ImportError with an install hint when crewai is absent."""
    if not _CREWAI_AVAILABLE:
        raise ImportError(_INSTALL_HINT)


def _warn_lost(phase: str, kind: str, exc: BaseException) -> None:
    """Say which record a swallowed handler error cost.

    A lost start means the event never becomes a record; a lost completion
    or failure leaves the record missing, or in flight until it is reported
    as abandoned.
    """
    if phase == "start":
        consequence = "no record will be produced for it"
    else:
        consequence = "its record is missing or will be reported as abandoned"
    logger.warning(
        "crewai: lost the %s of a %s event; %s: %s", phase, kind, consequence, exc,
    )


def _safe_str(obj: Any, max_chars: int = 10000) -> str:
    """Convert an object to a truncated string; never raises."""
    try:
        return str(obj)[:max_chars]
    except Exception:
        return "<unserializable>"


def _extract_event_key(event: Any, *preferred_attrs: str) -> str:
    """Extract a stable string key from an event for inflight pairing."""
    for attr in preferred_attrs:
        val = getattr(event, attr, None)
        if val is not None:
            try:
                return str(val)
            except Exception as exc:
                # Optional probe: an id-like attribute that cannot be stringified
                # is skipped and the next candidate is tried.
                logger.debug(
                    "crewai: event attribute %r is not usable as a pairing key, trying the next: %s",
                    attr, exc,
                )
    # Fall back to event id fields, then a fresh uuid
    for attr in ("id", "event_id", "_id"):
        val = getattr(event, attr, None)
        if val is not None:
            try:
                return str(val)
            except Exception as exc:
                # Optional probe: an id-like attribute that cannot be stringified
                # is skipped and the next candidate is tried.
                logger.debug(
                    "crewai: event attribute %r is not usable as a pairing key, trying the next: %s",
                    attr, exc,
                )
    return str(uuid.uuid4())


def _extract_inputs(event: Any, max_chars: int) -> Dict[str, Any]:
    """Extract meaningful input fields from a CrewAI event."""
    result: Dict[str, Any] = {}
    for attr in ("crew_id", "crew", "agent_id", "agent", "task_id", "task",
                 "tool_name", "tool", "model", "model_name", "call_id",
                 "query", "query_id", "name", "description", "role",
                 "inputs", "input", "messages", "key", "flow_id",
                 "method_name", "method_id"):
        val = getattr(event, attr, None)
        if val is not None:
            result[attr] = _safe_str(val, max_chars)
    return result


def _extract_outputs(event: Any, max_chars: int) -> Dict[str, Any]:
    """Extract meaningful output fields from a CrewAI completion event."""
    result: Dict[str, Any] = {}
    for attr in ("output", "result", "results", "response", "content",
                 "usage", "token_usage", "nodes", "score", "value"):
        val = getattr(event, attr, None)
        if val is not None:
            result[attr] = _safe_str(val, max_chars)
    return result
