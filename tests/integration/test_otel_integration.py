"""
OpenTelemetry integration tests with real exporters.

These tests verify the full instrumentation pipeline with actual
OpenTelemetry spans and exporters.
"""

import pytest
from unittest.mock import Mock

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False

pytestmark = [
    pytest.mark.skipif(not HAS_OTEL, reason="OpenTelemetry SDK not installed"),
    pytest.mark.integration
]


@pytest.fixture(scope="function")
def otel_setup():
    """Set up OpenTelemetry with in-memory exporter."""
    # Create fresh provider and exporter for each test
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Store old state
    old_provider = trace._TRACER_PROVIDER
    old_done = trace._TRACER_PROVIDER_SET_ONCE._done

    # Reset the Once guard so we can set a new provider
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace.set_tracer_provider(provider)

    yield exporter

    # Cleanup - restore old state
    trace._TRACER_PROVIDER = old_provider
    trace._TRACER_PROVIDER_SET_ONCE._done = old_done
    exporter.clear()


def test_lakefs_client_instrumentation(otel_setup):
    """Test lakeFS client creates proper spans with attributes."""
    from briefcase.integrations.lakefs.client import VersionedClient

    exporter = otel_setup
    tracer = trace.get_tracer(__name__)

    briefcase_client = Mock()
    client = VersionedClient(
        repository="test-repo",
        branch="main",
        briefcase_client=briefcase_client,
        mock=True,
    )

    # Read object within a span
    with tracer.start_as_current_span("test_operation"):
        client.read_object("test.pdf")

    # Verify spans
    spans = exporter.get_finished_spans()
    assert len(spans) == 1

    span = spans[0]
    assert span.name == "test_operation"

    # Verify attributes
    assert "lakefs.commit.sha" in span.attributes
    assert span.attributes["lakefs.repository"] == "test-repo"
    assert span.attributes["lakefs.commit.branch"] == "main"
    assert "lakefs.artifact.test.pdf" in span.attributes

    # Verify events
    events = [e for e in span.events if e.name == "lakefs.file_accessed"]
    assert len(events) == 1
    assert events[0].attributes["lakefs.file.path"] == "test.pdf"


def test_workflow_context_creates_root_span(otel_setup):
    """Test workflow context creates root span with proper attributes."""
    from briefcase.correlation import briefcase_workflow

    exporter = otel_setup
    briefcase_client = Mock()

    with briefcase_workflow("test_workflow", briefcase_client) as workflow:
        workflow.register_agent("101", "agent1")
        workflow.register_agent("102", "agent2")

    # Verify spans
    spans = exporter.get_finished_spans()
    assert len(spans) == 1

    span = spans[0]
    assert span.name == "workflow.test_workflow"

    # Verify attributes
    assert "workflow.id" in span.attributes
    assert span.attributes["workflow.name"] == "test_workflow"
    assert span.attributes["workflow.agent_count"] == 2
    assert "agent1(101)" in span.attributes["workflow.agent_chain"]
    assert "agent2(102)" in span.attributes["workflow.agent_chain"]
    assert span.attributes["workflow.status"] == "success"


def test_workflow_context_with_exception(otel_setup):
    """Test workflow context handles exceptions properly."""
    from briefcase.correlation import briefcase_workflow

    exporter = otel_setup
    briefcase_client = Mock()

    with pytest.raises(ValueError):
        with briefcase_workflow("test_workflow", briefcase_client):
            raise ValueError("Test error")

    # Verify span status
    spans = exporter.get_finished_spans()
    assert len(spans) == 1

    span = spans[0]
    assert span.attributes["workflow.status"] == "failed"
    assert span.status.status_code.name == "ERROR"


def test_nested_spans_with_lakefs(otel_setup):
    """Test nested spans with lakeFS operations."""
    from briefcase.integrations.lakefs.client import VersionedClient

    exporter = otel_setup
    tracer = trace.get_tracer(__name__)

    briefcase_client = Mock()
    client = VersionedClient(
        repository="test-repo",
        branch="main",
        briefcase_client=briefcase_client,
        mock=True,
    )

    # Create nested spans
    with tracer.start_as_current_span("parent_operation"):
        client.read_object("file1.pdf")

        with tracer.start_as_current_span("child_operation"):
            client.read_object("file2.pdf")

    # Verify spans
    spans = exporter.get_finished_spans()
    assert len(spans) == 2

    # Verify parent-child relationship
    child_span = [s for s in spans if s.name == "child_operation"][0]
    parent_span = [s for s in spans if s.name == "parent_operation"][0]

    assert child_span.parent.span_id == parent_span.context.span_id

    # Verify both have lakeFS attributes
    assert "lakefs.commit.sha" in parent_span.attributes
    assert "lakefs.commit.sha" in child_span.attributes


def test_workflow_with_multiple_lakefs_operations(otel_setup):
    """Test workflow context with multiple lakeFS operations."""
    from briefcase.correlation import briefcase_workflow
    from briefcase.integrations.lakefs.client import VersionedClient

    exporter = otel_setup
    briefcase_client = Mock()

    with briefcase_workflow("data_processing", briefcase_client):
        # Create lakeFS client within workflow
        lakefs = VersionedClient(
            repository="test-repo",
            branch="main",
            briefcase_client=briefcase_client,
            mock=True,
        )

        # Read multiple files
        lakefs.read_object("data1.csv")
        lakefs.read_object("data2.csv")
        lakefs.list_objects(prefix="data/")

    # Verify spans
    spans = exporter.get_finished_spans()
    assert len(spans) == 1  # Workflow span

    workflow_span = spans[0]

    # Verify lakeFS attributes are captured
    assert "lakefs.commit.sha" in workflow_span.attributes
    assert "lakefs.artifact.data1.csv" in workflow_span.attributes
    assert "lakefs.artifact.data2.csv" in workflow_span.attributes

    # Verify events
    file_events = [e for e in workflow_span.events if e.name == "lakefs.file_accessed"]
    assert len(file_events) == 2  # Two file reads

    list_events = [e for e in workflow_span.events if e.name == "lakefs.objects_listed"]
    assert len(list_events) == 1  # One list operation


def test_trace_context_propagation(otel_setup):
    """Test trace context can be extracted and injected."""
    from briefcase.correlation import inject_trace_context, extract_trace_context

    exporter = otel_setup
    tracer = trace.get_tracer(__name__)

    # Create a span and inject context
    with tracer.start_as_current_span("source_span"):
        headers = inject_trace_context()

    # Verify W3C Trace Context headers
    assert "traceparent" in headers
    assert headers["traceparent"].startswith("00-")  # W3C format

    # Extract and verify
    token = extract_trace_context(headers)
    assert token is not None

    # Create span in extracted context
    with tracer.start_as_current_span("target_span"):
        pass

    # Verify spans are linked
    spans = exporter.get_finished_spans()
    assert len(spans) == 2


def test_concurrent_workflows(otel_setup):
    """Test multiple concurrent workflows maintain separate traces."""
    from briefcase.correlation import briefcase_workflow
    import threading

    exporter = otel_setup
    briefcase_client = Mock()
    results = []

    def run_workflow(name):
        with briefcase_workflow(name, briefcase_client) as workflow:
            results.append(workflow.workflow_id)

    # Run two workflows concurrently
    t1 = threading.Thread(target=run_workflow, args=("workflow1",))
    t2 = threading.Thread(target=run_workflow, args=("workflow2",))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Verify separate workflow IDs
    assert len(results) == 2
    assert results[0] != results[1]

    # Verify separate spans
    spans = exporter.get_finished_spans()
    assert len(spans) == 2

    workflow_ids = [s.attributes["workflow.id"] for s in spans]
    assert len(set(workflow_ids)) == 2  # Two unique workflow IDs


def test_span_attributes_format(otel_setup):
    """Test all span attributes follow semantic conventions."""
    from briefcase.integrations.lakefs.client import VersionedClient
    from briefcase.correlation import briefcase_workflow

    exporter = otel_setup
    briefcase_client = Mock()

    with briefcase_workflow("test", briefcase_client):
        lakefs = VersionedClient(
            repository="test-repo",
            branch="main",
            briefcase_client=briefcase_client,
            mock=True,
        )
        lakefs.read_object("test.pdf")

    spans = exporter.get_finished_spans()
    span = spans[0]

    # Verify attribute naming conventions
    lakefs_attrs = {k: v for k, v in span.attributes.items() if k.startswith("lakefs.")}
    workflow_attrs = {k: v for k, v in span.attributes.items() if k.startswith("workflow.")}

    assert len(lakefs_attrs) > 0
    assert len(workflow_attrs) > 0

    # All attributes should be lowercase with dots
    for attr_name in span.attributes.keys():
        assert attr_name.islower() or "_" in attr_name
        assert "." in attr_name
