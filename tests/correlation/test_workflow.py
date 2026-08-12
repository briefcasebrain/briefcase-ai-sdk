"""
Tests for workflow correlation.
"""

import pytest
from unittest.mock import Mock
from briefcase.correlation.workflow import briefcase_workflow, get_current_workflow

try:
    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False

# Check for real OTel SDK (not just a mock injected by other tests)
try:
    from opentelemetry.sdk.trace import TracerProvider as _TP  # noqa: F401
    HAS_OTEL_SDK = True
except (ImportError, ModuleNotFoundError):
    HAS_OTEL_SDK = False


def test_workflow_context_creation():
    """Test workflow context creates and cleans up properly."""
    briefcase_client = Mock()

    with briefcase_workflow("test_workflow", briefcase_client) as workflow:
        assert workflow.workflow_name == "test_workflow"
        assert workflow.workflow_id.startswith("wf_")
        assert get_current_workflow() == workflow

    # Verify workflow is cleared after exit
    assert get_current_workflow() is None


def test_workflow_context_with_custom_id():
    """Test workflow context with custom workflow_id."""
    briefcase_client = Mock()
    custom_id = "custom_workflow_123"

    with briefcase_workflow("test", briefcase_client, workflow_id=custom_id) as workflow:
        assert workflow.workflow_id == custom_id


def test_agent_registration():
    """Test that agents can register with workflow."""
    briefcase_client = Mock()

    with briefcase_workflow("test", briefcase_client) as workflow:
        workflow.register_agent("101", "intake")
        workflow.register_agent("102", "decision")

        assert workflow._agent_count == 2
        assert workflow._agent_chain == ["intake(101)", "decision(102)"]


def test_nested_workflow_restores_outer_context():
    """Exiting a nested workflow restores the enclosing workflow context."""
    briefcase_client = Mock()

    with briefcase_workflow("outer", briefcase_client) as outer:
        with briefcase_workflow("inner", briefcase_client) as inner:
            assert get_current_workflow() is inner
        assert get_current_workflow() is outer
    assert get_current_workflow() is None


def test_workflow_exception_handling():
    """Test workflow handles exceptions properly."""
    briefcase_client = Mock()

    with pytest.raises(ValueError):
        with briefcase_workflow("test", briefcase_client):
            raise ValueError("Test error")

    # Workflow should still have cleaned up
    assert get_current_workflow() is None


@pytest.mark.skipif(not HAS_OTEL_SDK, reason="OpenTelemetry SDK not installed")
def test_workflow_with_otel():
    """Test workflow context with OpenTelemetry available."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Store old state
    old_provider = trace._TRACER_PROVIDER
    old_done = trace._TRACER_PROVIDER_SET_ONCE._done

    # Setup tracing
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Reset the Once guard and set new provider
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace.set_tracer_provider(provider)

    try:
        briefcase_client = Mock()

        with briefcase_workflow("test_workflow", briefcase_client) as workflow:
            assert workflow.workflow_name == "test_workflow"
            workflow.register_agent("1", "agent1")

        # Verify span attributes
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]

        assert span.name == "workflow.test_workflow"
        assert span.attributes["workflow.id"].startswith("wf_")
        assert span.attributes["workflow.name"] == "test_workflow"
        assert span.attributes["workflow.status"] == "success"
        assert span.attributes["workflow.agent_count"] == 1
        assert "agent1(1)" in span.attributes["workflow.agent_chain"]
    finally:
        # Restore old state
        trace._TRACER_PROVIDER = old_provider
        trace._TRACER_PROVIDER_SET_ONCE._done = old_done
        exporter.clear()
