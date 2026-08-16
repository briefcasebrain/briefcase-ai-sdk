"""Microsoft AutoGen v0.4+ logging-based integration for Briefcase.

Attaches a logging.Handler to AutoGen's EVENT_LOGGER_NAME and
TRACE_LOGGER_NAME to capture agent messages, tool calls, and control events
as Briefcase decision records.

Usage (convenience):
    from briefcase.integrations.frameworks import autogen_hook
    handler = autogen_hook.install()

Usage (explicit):
    from briefcase.integrations.frameworks import AutoGenEventHandler
    handler = AutoGenEventHandler(context_version="v2")
    handler.attach()
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from briefcase._export_mixin import ExportMixin

_INSTALL_HINT = (
    "autogen-agentchat is required for AutoGenEventHandler. "
    "Install with: pip install autogen-agentchat  or  pip install briefcase-ai[autogen]"
)

# Optional dependency guard. The fallback constants keep the module importable
# without autogen-agentchat; the constructor raises when it is absent.
try:
    from autogen_agentchat import EVENT_LOGGER_NAME, TRACE_LOGGER_NAME
    _AUTOGEN_AVAILABLE = True
except ImportError:
    _AUTOGEN_AVAILABLE = False
    EVENT_LOGGER_NAME = "autogen_agentchat.event"
    TRACE_LOGGER_NAME = "autogen_agentchat.trace"


# Global install tracking for idempotency
_INSTALLED_HANDLER: Optional["AutoGenEventHandler"] = None


def install(
    context_version: Optional[str] = None,
    async_capture: bool = True,
    exporter: Any = None,
) -> "AutoGenEventHandler":
    """Install AutoGenEventHandler globally into AutoGen's logging layer.

    Idempotent: calling install() again returns the same handler without
    double-attachment.

    Args:
        context_version: Optional version tag added to all decision records.
        async_capture: If True (default), export is fire-and-forget.
        exporter: Briefcase exporter instance; falls back to the global config.

    Returns:
        The installed AutoGenEventHandler instance.

    Raises:
        ImportError: If autogen-agentchat is not installed.
    """
    global _INSTALLED_HANDLER

    if not _AUTOGEN_AVAILABLE:
        raise ImportError(_INSTALL_HINT)

    if _INSTALLED_HANDLER is not None:
        return _INSTALLED_HANDLER

    handler = AutoGenEventHandler(
        context_version=context_version,
        async_capture=async_capture,
        exporter=exporter,
    )
    handler.attach()
    _INSTALLED_HANDLER = handler
    return handler


def uninstall() -> None:
    """Detach the globally installed handler, if any, and forget it."""
    global _INSTALLED_HANDLER
    if _INSTALLED_HANDLER is not None:
        try:
            _INSTALLED_HANDLER.detach()
        finally:
            _INSTALLED_HANDLER = None


class AutoGenEventHandler(ExportMixin, logging.Handler):
    """Briefcase logging.Handler for AutoGen v0.4+ agent event streams.

    Captures:
    - TextMessage events (source, content)
    - ToolCallRequestEvent (tool_calls)
    - ToolCallExecutionEvent (results)
    - ToolCallSummaryMessage (content)
    - StopMessage / HandoffMessage (target / content)
    - Unknown event objects (raw)
    - Trace log records (when capture_traces=True)

    Attaches to EVENT_LOGGER_NAME and TRACE_LOGGER_NAME via attach().
    All errors are swallowed; nothing propagates into agent execution.
    """

    def __init__(
        self,
        context_version: Optional[str] = None,
        async_capture: bool = True,
        capture_events: bool = True,
        capture_traces: bool = True,
        max_input_chars: int = 10000,
        max_output_chars: int = 10000,
        exporter: Any = None,
    ):
        if not _AUTOGEN_AVAILABLE:
            raise ImportError(_INSTALL_HINT)

        super().__init__()
        self.context_version = context_version
        self.async_capture = async_capture
        self.capture_events = capture_events
        self.capture_traces = capture_traces
        self.max_input_chars = max_input_chars
        self.max_output_chars = max_output_chars
        self._exporter = exporter

        self._records: List[Dict[str, Any]] = []
        self._attached_loggers: List[str] = []

    # logging.Handler interface

    def emit(self, record: logging.LogRecord) -> None:
        """Called for each log record. Classifies and captures the event."""
        try:
            logger_name = record.name
            is_event = logger_name == EVENT_LOGGER_NAME
            is_trace = logger_name == TRACE_LOGGER_NAME

            if is_event and not self.capture_events:
                return
            if is_trace and not self.capture_traces:
                return

            event_data = _extract_event_data(record.msg, self.max_input_chars)
            briefcase_record = self._build_record(event_data, logger_name)
            self._records.append(briefcase_record)
            self._trigger_export(briefcase_record)
        except Exception:
            pass

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

    def attach(self) -> None:
        """Attach this handler to AutoGen's event and trace loggers."""
        require_autogen()
        for name in (EVENT_LOGGER_NAME, TRACE_LOGGER_NAME):
            lg = logging.getLogger(name)
            lg.addHandler(self)
            lg.setLevel(logging.DEBUG)
            if name not in self._attached_loggers:
                self._attached_loggers.append(name)

    def detach(self) -> None:
        """Remove this handler from all attached loggers."""
        for name in list(self._attached_loggers):
            lg = logging.getLogger(name)
            lg.removeHandler(self)
        self._attached_loggers.clear()

    # Internal helpers

    def _build_record(
        self,
        event_data: Dict[str, Any],
        logger_name: str,
    ) -> Dict[str, Any]:
        """Build a serializable decision record.

        Log events are instantaneous observations: the record carries
        started_at (the capture instant) with no ended_at or duration.
        """
        inputs = event_data.get("inputs", {})
        record: Dict[str, Any] = {
            "decision_id": str(uuid.uuid4()),
            "decision_type": event_data.get("decision_type", "autogen_event"),
            "function_name": inputs.get("source") or "autogen",
            "inputs": inputs,
            "outputs": event_data.get("outputs", {}),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "logger": logger_name,
        }
        if self.context_version is not None:
            record["context_version"] = self.context_version
        return record


# Module-level helpers

def require_autogen() -> None:
    """Raise ImportError with an install hint when autogen-agentchat is absent."""
    if not _AUTOGEN_AVAILABLE:
        raise ImportError(_INSTALL_HINT)


def _extract_event_data(msg: Any, max_chars: int = 10000) -> Dict[str, Any]:
    """Classify and extract data from an AutoGen event object.

    Uses duck typing on the msg object to determine the event type.
    Returns a dict with 'decision_type', 'inputs', 'outputs'.
    """
    type_name = type(msg).__name__

    # TextMessage and its control-message variants
    if _has_attrs(msg, "source", "content") and not _has_attrs(msg, "tool_calls"):
        content = _safe_str(getattr(msg, "content", ""), max_chars)
        source = _safe_str(getattr(msg, "source", ""), max_chars)
        if "Stop" in type_name:
            return {
                "decision_type": "control_message",
                "inputs": {"source": source, "content": content, "subtype": "stop"},
                "outputs": {},
            }
        if "Handoff" in type_name:
            target = _safe_str(getattr(msg, "target", ""), max_chars)
            return {
                "decision_type": "control_message",
                "inputs": {"source": source, "target": target, "content": content, "subtype": "handoff"},
                "outputs": {},
            }
        if "Summary" in type_name:
            return {
                "decision_type": "tool_call_summary",
                "inputs": {"source": source, "content": content},
                "outputs": {},
            }
        return {
            "decision_type": "text_message",
            "inputs": {"source": source, "content": content},
            "outputs": {},
        }

    # ToolCallRequestEvent
    if _has_attrs(msg, "tool_calls") and not _has_attrs(msg, "results"):
        raw_calls = getattr(msg, "tool_calls", [])
        calls = _safe_serialize(raw_calls, max_chars)
        source = _safe_str(getattr(msg, "source", ""), max_chars)
        return {
            "decision_type": "tool_call_request",
            "inputs": {"source": source, "tool_calls": calls},
            "outputs": {},
        }

    # ToolCallExecutionEvent
    if _has_attrs(msg, "results"):
        raw_results = getattr(msg, "results", [])
        results = _safe_serialize(raw_results, max_chars)
        source = _safe_str(getattr(msg, "source", ""), max_chars)
        return {
            "decision_type": "tool_call_execution",
            "inputs": {"source": source},
            "outputs": {"results": results},
        }

    # Unknown / raw
    if isinstance(msg, str):
        return {
            "decision_type": "autogen_event",
            "inputs": {"raw": msg[:max_chars]},
            "outputs": {},
        }

    return {
        "decision_type": "autogen_event",
        "inputs": {"type": type_name, "raw": _safe_str(msg, max_chars)},
        "outputs": {},
    }


def _has_attrs(obj: Any, *attrs: str) -> bool:
    """Return True when obj has all the given attributes."""
    return all(hasattr(obj, a) for a in attrs)


def _safe_str(obj: Any, max_chars: int = 10000) -> str:
    """Convert an object to a truncated string; never raises."""
    try:
        return str(obj)[:max_chars]
    except Exception:
        return "<unserializable>"


def _safe_serialize(obj: Any, max_chars: int = 10000) -> Any:
    """Serialize a list or object to a JSON-compatible form; never raises."""
    try:
        if obj is None:
            return None
        if isinstance(obj, list):
            return [_safe_str(item, max_chars) for item in obj[:50]]
        return _safe_str(obj, max_chars)
    except Exception:
        return "<unserializable>"
