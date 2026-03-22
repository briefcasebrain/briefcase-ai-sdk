"""
Correlation utilities for multi-agent workflows.
"""

try:
    from briefcase.correlation.workflow import (
        briefcase_workflow,
        BriefcaseWorkflowContext,
        get_current_workflow,
    )
    from briefcase.correlation.propagation import (
        TraceContextCarrier,
        inject_trace_context,
        extract_trace_context,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.correlation requires the 'correlation' extra.\n"
        "Install it with: pip install briefcase-ai[correlation]"
    ) from exc

__all__ = [
    "briefcase_workflow",
    "BriefcaseWorkflowContext",
    "get_current_workflow",
    "TraceContextCarrier",
    "inject_trace_context",
    "extract_trace_context",
]
