"""Coverage hardening tests for briefcase.correlation.workflow."""

from __future__ import annotations

from unittest.mock import MagicMock

from briefcase.correlation.workflow import BriefcaseWorkflowContext


def test_workflow_enter_and_exit_error_branches(monkeypatch):
    # __enter__ span creation failure branch
    trace_module = MagicMock()
    tracer = MagicMock()
    tracer.start_span.side_effect = RuntimeError("span create failed")
    trace_module.get_tracer.return_value = tracer

    monkeypatch.setattr("briefcase.correlation.workflow.HAS_OTEL", True)
    monkeypatch.setattr("briefcase.correlation.workflow.trace", trace_module)

    ctx = BriefcaseWorkflowContext("wf", briefcase_client=MagicMock())
    with ctx:
        pass

    # __exit__ finalization error branch
    ctx2 = BriefcaseWorkflowContext("wf2", briefcase_client=MagicMock())
    ctx2._started_at = __import__("datetime").datetime.now()
    ctx2._workflow_span = MagicMock()
    ctx2._workflow_span.set_attribute.side_effect = RuntimeError("set attr failed")
    assert ctx2.__exit__(None, None, None) is False


def test_register_agent_event_error_branch(monkeypatch):
    monkeypatch.setattr("briefcase.correlation.workflow.HAS_OTEL", True)
    mock_trace = MagicMock()
    monkeypatch.setattr("briefcase.correlation.workflow.trace", mock_trace)

    ctx = BriefcaseWorkflowContext("wf", briefcase_client=MagicMock())
    ctx._workflow_span = MagicMock()
    ctx._workflow_span.add_event.side_effect = RuntimeError("event failed")

    # Should swallow instrumentation failure and still update counters
    ctx.register_agent("101", "planner")
    assert ctx._agent_count == 1
    assert ctx._agent_chain == ["planner(101)"]
