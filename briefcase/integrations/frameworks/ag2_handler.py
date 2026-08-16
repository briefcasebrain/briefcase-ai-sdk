"""AG2 (community AutoGen fork) hook-based tracing integration for Briefcase.

Registers read-only hooks on ConversableAgent instances to capture message
sends, message context, state updates, and LLM/tool safeguard events. Every
hook returns its argument unmodified and never raises into agent execution.

Usage (convenience):
    from briefcase.integrations.frameworks import ag2_hook
    tracer = ag2_hook.instrument_agent(agent)

Usage (explicit):
    from briefcase.integrations.frameworks import AG2HookTracer
    tracer = AG2HookTracer(context_version="v2")
    tracer.instrument(agent)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from briefcase._export_mixin import ExportMixin

_INSTALL_HINT = (
    "ag2 is required for AG2HookTracer. "
    "Install with: pip install ag2  or  pip install briefcase-ai[ag2]"
)

# Optional dependency guard: ag2 publishes the autogen namespace.
try:
    from autogen import ConversableAgent  # noqa: F401
    _AG2_AVAILABLE = True
except ImportError:
    _AG2_AVAILABLE = False
    ConversableAgent = None  # type: ignore[assignment,misc]


def instrument_agent(
    agent: Any,
    context_version: Optional[str] = None,
    async_capture: bool = True,
    exporter: Any = None,
) -> "AG2HookTracer":
    """Create an AG2HookTracer and instrument the given agent.

    Args:
        agent: A ConversableAgent (or subclass) instance.
        context_version: Optional version tag added to all decision records.
        async_capture: If True (default), export is fire-and-forget.
        exporter: Briefcase exporter instance; falls back to the global config.

    Returns:
        The AG2HookTracer instance registered on the agent.

    Raises:
        ImportError: If ag2 is not installed.
    """
    tracer = AG2HookTracer(
        context_version=context_version,
        async_capture=async_capture,
        exporter=exporter,
    )
    tracer.instrument(agent)
    return tracer


class AG2HookTracer(ExportMixin):
    """Briefcase hook-based tracer for AG2.

    Captures:
    - Message sends (process_message_before_send)
    - Message context (process_all_messages_before_reply)
    - Agent state updates (update_agent_state)
    - LLM input/output safeguard events (safeguard_llm_inputs/outputs)
    - Tool input/output safeguard events (safeguard_tool_inputs/outputs)

    All hook functions return their argument unmodified (read-only
    observation) and never raise into agent execution.
    """

    # Hook names that the AG2 ConversableAgent supports
    _HOOK_NAMES = [
        "process_message_before_send",
        "process_all_messages_before_reply",
        "update_agent_state",
        "safeguard_llm_inputs",
        "safeguard_llm_outputs",
        "safeguard_tool_inputs",
        "safeguard_tool_outputs",
    ]

    def __init__(
        self,
        context_version: Optional[str] = None,
        async_capture: bool = True,
        capture_messages: bool = True,
        capture_llm: bool = True,
        capture_tools: bool = True,
        capture_state: bool = True,
        max_input_chars: int = 10000,
        max_output_chars: int = 10000,
        exporter: Any = None,
    ):
        if not _AG2_AVAILABLE:
            raise ImportError(_INSTALL_HINT)

        self.context_version = context_version
        self.async_capture = async_capture
        self.capture_messages = capture_messages
        self.capture_llm = capture_llm
        self.capture_tools = capture_tools
        self.capture_state = capture_state
        self.max_input_chars = max_input_chars
        self.max_output_chars = max_output_chars
        self._exporter = exporter

        self._records: List[Dict[str, Any]] = []

    # Public API

    def get_records(self) -> List[Dict[str, Any]]:
        """Return all captured decision records."""
        return list(self._records)

    def clear(self) -> None:
        """Clear all captured records."""
        self._records.clear()

    @property
    def decision_count(self) -> int:
        """Number of captured decision records."""
        return len(self._records)

    def instrument(self, agent: Any) -> None:
        """Register all hooks on the given agent.

        Args:
            agent: A ConversableAgent instance.

        Raises:
            ImportError: If ag2 is not installed.
        """
        require_ag2()
        self._register_hooks(agent)

    def instrument_many(self, agents: List[Any]) -> None:
        """Register hooks on multiple agents.

        Args:
            agents: A list of ConversableAgent instances.
        """
        for agent in agents:
            self.instrument(agent)

    # Hook registration

    def _register_hooks(self, agent: Any) -> None:
        """Register all applicable hooks on the agent."""
        if self.capture_messages:
            agent.register_hook(
                "process_message_before_send",
                self._make_message_send_hook(agent),
            )
            agent.register_hook(
                "process_all_messages_before_reply",
                self._make_message_context_hook(agent),
            )

        if self.capture_state:
            agent.register_hook(
                "update_agent_state",
                self._make_state_update_hook(agent),
            )

        if self.capture_llm:
            agent.register_hook(
                "safeguard_llm_inputs",
                self._make_llm_input_hook(agent),
            )
            agent.register_hook(
                "safeguard_llm_outputs",
                self._make_llm_output_hook(agent),
            )

        if self.capture_tools:
            agent.register_hook(
                "safeguard_tool_inputs",
                self._make_tool_input_hook(agent),
            )
            agent.register_hook(
                "safeguard_tool_outputs",
                self._make_tool_output_hook(agent),
            )

    def _make_message_send_hook(self, agent: Any):
        """Return the hook for process_message_before_send."""
        tracer = self

        def _hook(message, recipient, silent):
            try:
                record = tracer._build_record(
                    decision_type="message_send",
                    function_name=_agent_name(agent),
                    inputs={
                        "content": tracer._safe_extract_message(message),
                        "recipient": _agent_name(recipient),
                        "silent": silent,
                    },
                )
                tracer._append_and_export(record)
            except Exception:
                pass
            return message

        return _hook

    def _make_message_context_hook(self, agent: Any):
        """Return the hook for process_all_messages_before_reply."""
        tracer = self

        def _hook(messages):
            try:
                safe_msgs = []
                if isinstance(messages, list):
                    for m in messages:
                        safe_msgs.append(tracer._safe_extract_message(m))
                record = tracer._build_record(
                    decision_type="message_context",
                    function_name=_agent_name(agent),
                    inputs={
                        "agent": _agent_name(agent),
                        "message_count": len(messages) if isinstance(messages, list) else 0,
                        "messages": safe_msgs,
                    },
                )
                tracer._append_and_export(record)
            except Exception:
                pass
            return messages

        return _hook

    def _make_state_update_hook(self, agent: Any):
        """Return the hook for update_agent_state."""
        tracer = self

        def _hook(agent_state):
            try:
                record = tracer._build_record(
                    decision_type="state_update",
                    function_name=_agent_name(agent),
                    inputs={
                        "agent": _agent_name(agent),
                        "state": _safe_serialize_small(agent_state, tracer.max_input_chars),
                    },
                )
                tracer._append_and_export(record)
            except Exception:
                pass
            return agent_state

        return _hook

    def _make_llm_input_hook(self, agent: Any):
        """Return the hook for safeguard_llm_inputs."""
        tracer = self

        def _hook(messages):
            try:
                safe_msgs = []
                if isinstance(messages, list):
                    for m in messages:
                        safe_msgs.append(tracer._safe_extract_message(m))
                record = tracer._build_record(
                    decision_type="llm_input",
                    function_name=_agent_name(agent),
                    inputs={
                        "agent": _agent_name(agent),
                        "messages": safe_msgs,
                    },
                )
                tracer._append_and_export(record)
            except Exception:
                pass
            return messages

        return _hook

    def _make_llm_output_hook(self, agent: Any):
        """Return the hook for safeguard_llm_outputs."""
        tracer = self

        def _hook(response):
            try:
                record = tracer._build_record(
                    decision_type="llm_output",
                    function_name=_agent_name(agent),
                    outputs={
                        "agent": _agent_name(agent),
                        "response": _safe_serialize_small(response, tracer.max_output_chars),
                    },
                )
                tracer._append_and_export(record)
            except Exception:
                pass
            return response

        return _hook

    def _make_tool_input_hook(self, agent: Any):
        """Return the hook for safeguard_tool_inputs."""
        tracer = self

        def _hook(tool_call):
            try:
                record = tracer._build_record(
                    decision_type="tool_input",
                    function_name=_agent_name(agent),
                    inputs={
                        "agent": _agent_name(agent),
                        "tool_call": _safe_serialize_small(tool_call, tracer.max_input_chars),
                    },
                )
                tracer._append_and_export(record)
            except Exception:
                pass
            return tool_call

        return _hook

    def _make_tool_output_hook(self, agent: Any):
        """Return the hook for safeguard_tool_outputs."""
        tracer = self

        def _hook(tool_result):
            try:
                record = tracer._build_record(
                    decision_type="tool_output",
                    function_name=_agent_name(agent),
                    outputs={
                        "agent": _agent_name(agent),
                        "result": _safe_serialize_small(tool_result, tracer.max_output_chars),
                    },
                )
                tracer._append_and_export(record)
            except Exception:
                pass
            return tool_result

        return _hook

    # Internal helpers

    def _build_record(
        self,
        decision_type: str,
        function_name: Optional[str] = None,
        inputs: Optional[Dict[str, Any]] = None,
        outputs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a serializable decision record dict.

        Hook events are instantaneous observations: the record carries
        started_at (the capture instant) with no ended_at or duration.
        """
        record: Dict[str, Any] = {
            "decision_id": str(uuid.uuid4()),
            "decision_type": decision_type,
            "function_name": function_name or "ag2",
            "inputs": inputs or {},
            "outputs": outputs or {},
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.context_version is not None:
            record["context_version"] = self.context_version
        return record

    def _append_and_export(self, record: Dict[str, Any]) -> None:
        """Append the record and trigger export."""
        self._records.append(record)
        self._trigger_export(record)

    def _safe_extract_message(self, message: Any) -> Any:
        return _safe_extract_message(message, self.max_input_chars)


# Module-level helpers

def require_ag2() -> None:
    """Raise ImportError with an install hint when ag2 is absent."""
    if not _AG2_AVAILABLE:
        raise ImportError(_INSTALL_HINT)


def _agent_name(agent: Any) -> Optional[str]:
    """Extract the name from an agent object; never raises."""
    try:
        return getattr(agent, "name", None) or str(agent)
    except Exception:
        return None


def _safe_extract_message(message: Any, max_chars: int = 10000) -> Any:
    """Serialize a message to a loggable form; never raises."""
    try:
        if message is None:
            return None
        if isinstance(message, str):
            return message[:max_chars]
        if isinstance(message, dict):
            content = message.get("content", "")
            role = message.get("role", "unknown")
            return {
                "role": role,
                "content": str(content)[:max_chars],
            }
        return str(message)[:max_chars]
    except Exception:
        return "<unserializable>"


def _safe_serialize_small(obj: Any, max_chars: int = 10000) -> Any:
    """Serialize a small object to a JSON-compatible form; never raises."""
    try:
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return str(obj)[:max_chars] if isinstance(obj, str) else obj
        if isinstance(obj, dict):
            return {str(k): str(v)[:max_chars] for k, v in list(obj.items())[:50]}
        if isinstance(obj, (list, tuple)):
            return [str(item)[:max_chars] for item in list(obj)[:50]]
        return str(obj)[:max_chars]
    except Exception:
        return "<unserializable>"
