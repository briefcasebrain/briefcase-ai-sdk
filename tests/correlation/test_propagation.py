"""
Tests for trace context propagation utilities.

Covers:
  - TraceContextCarrier.inject() with and without OTel
  - TraceContextCarrier.extract() with and without OTel
  - Workflow state embedding in tracestate
  - Convenience functions (inject_trace_context, extract_trace_context)
  - Error handling and fallback behavior
"""

import sys
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from briefcase.correlation.propagation import (
    TraceContextCarrier,
    inject_trace_context,
    extract_trace_context,
)


# 
# Setup: Mock opentelemetry if not available
# 

# Create mock opentelemetry modules to prevent ImportError
if 'opentelemetry' not in sys.modules:
    sys.modules['opentelemetry'] = MagicMock()
    sys.modules['opentelemetry.trace'] = MagicMock()
    sys.modules['opentelemetry.context'] = MagicMock()
    sys.modules['opentelemetry.trace.propagation'] = MagicMock()
    sys.modules['opentelemetry.trace.propagation.tracecontext'] = MagicMock()


# 
# Fixtures and Helpers
# 

def inject_mock_propagator(mock_class):
    """Helper to inject a mock propagator into the propagation module."""
    import briefcase.correlation.propagation as propagation_module
    original = getattr(propagation_module, 'TraceContextTextMapPropagator', None)
    propagation_module.TraceContextTextMapPropagator = mock_class
    return original


def restore_propagator(original):
    """Helper to restore the original propagator class."""
    import briefcase.correlation.propagation as propagation_module
    if original is not None:
        propagation_module.TraceContextTextMapPropagator = original
    elif hasattr(propagation_module, 'TraceContextTextMapPropagator'):
        delattr(propagation_module, 'TraceContextTextMapPropagator')


@pytest.fixture
def reset_propagator():
    """Reset the cached propagator before and after each test."""
    original_propagator = TraceContextCarrier._propagator
    TraceContextCarrier._propagator = None
    yield
    TraceContextCarrier._propagator = original_propagator


# 
# Tests: inject() - returns dict
# 

def test_inject_returns_dict(reset_propagator):
    """Test that inject() returns a dictionary."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        result = TraceContextCarrier.inject()
        assert isinstance(result, dict)


def test_inject_with_none_carrier_creates_dict(reset_propagator):
    """Test that inject(None) creates a new dict."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        result = TraceContextCarrier.inject(None)
        assert isinstance(result, dict)


def test_inject_with_existing_carrier_returns_same_dict(reset_propagator):
    """Test that inject() with an existing carrier returns that dict."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        carrier = {"existing_key": "existing_value"}
        result = TraceContextCarrier.inject(carrier)
        assert result is carrier


def test_inject_without_otel_returns_empty_carrier(reset_propagator):
    """Test that inject() returns empty carrier when OTel is not available."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        result = TraceContextCarrier.inject()
        # Without OTel, no headers should be added
        assert len(result) == 0


# 
# Tests: extract() - without OTel
# 

def test_extract_without_otel_returns_none(reset_propagator):
    """Test that extract() returns None when OTel is not available."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        result = TraceContextCarrier.extract({})
        assert result is None


def test_extract_without_otel_with_headers_still_returns_none(reset_propagator):
    """Test that extract() returns None even with trace headers when OTel unavailable."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        headers = {"traceparent": "00-trace-id-span-id-01"}
        result = TraceContextCarrier.extract(headers)
        assert result is None


# 
# Tests: inject_trace_context() convenience function
# 

def test_inject_trace_context_returns_dict(reset_propagator):
    """Test that inject_trace_context() returns a dictionary."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        result = inject_trace_context()
        assert isinstance(result, dict)


def test_inject_trace_context_with_headers(reset_propagator):
    """Test that inject_trace_context() accepts headers."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        headers = {"existing": "value"}
        result = inject_trace_context(headers)
        assert isinstance(result, dict)


# 
# Tests: extract_trace_context() convenience function
# 

def test_extract_trace_context_accepts_dict(reset_propagator):
    """Test that extract_trace_context() accepts a dictionary."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        result = extract_trace_context({})
        # Without OTel, should return None
        assert result is None


def test_extract_trace_context_with_traceparent(reset_propagator):
    """Test that extract_trace_context() handles traceparent header."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        headers = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
        result = extract_trace_context(headers)
        assert result is None  # No OTel available


# 
# Tests: inject with OTel mocking
# 

@patch('briefcase.correlation.propagation.HAS_OTEL', True)
def test_inject_with_otel_calls_propagator(reset_propagator):
    """Test that inject() calls the OTel propagator when available."""
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)

    original = inject_mock_propagator(mock_propagator_class)
    try:
        result = TraceContextCarrier.inject()

        # Propagator should have been called
        mock_propagator.inject.assert_called_once()
        assert isinstance(result, dict)
    finally:
        restore_propagator(original)


@patch('briefcase.correlation.propagation.HAS_OTEL', True)
@patch('briefcase.correlation.propagation.get_current_workflow')
def test_inject_with_otel_and_workflow(mock_get_workflow, reset_propagator):
    """Test that inject() embeds workflow info when available."""
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)

    # Mock a workflow
    mock_workflow = MagicMock()
    mock_workflow.workflow_id = "test-workflow-id"
    mock_get_workflow.return_value = mock_workflow

    original = inject_mock_propagator(mock_propagator_class)
    try:
        carrier = {}
        result = TraceContextCarrier.inject(carrier)

        # Should have added workflow to tracestate
        assert "tracestate" in result or "traceparent" in result
        if "tracestate" in result:
            assert "workflow_id" in result["tracestate"]
    finally:
        restore_propagator(original)


@patch('briefcase.correlation.propagation.HAS_OTEL', True)
@patch('briefcase.correlation.propagation.get_current_workflow')
def test_inject_workflow_state_format(mock_get_workflow, reset_propagator):
    """Test that workflow state is properly formatted."""
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)

    mock_workflow = MagicMock()
    mock_workflow.workflow_id = "my-workflow-123"
    mock_get_workflow.return_value = mock_workflow

    original = inject_mock_propagator(mock_propagator_class)
    try:
        carrier = {}
        TraceContextCarrier.inject(carrier)

        # Check that tracestate contains properly formatted workflow info
        if "tracestate" in carrier:
            assert "briefcase=workflow_id:my-workflow-123" in carrier["tracestate"]
    finally:
        restore_propagator(original)


@patch('briefcase.correlation.propagation.HAS_OTEL', True)
def test_inject_without_workflow_no_tracestate_workflow(reset_propagator):
    """Test that inject() doesn't add workflow state if no workflow exists."""
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)

    original = inject_mock_propagator(mock_propagator_class)
    try:
        with patch('briefcase.correlation.propagation.get_current_workflow') as mock_get_workflow:
            mock_get_workflow.return_value = None

            carrier = {}
            TraceContextCarrier.inject(carrier)

            # If no workflow, tracestate shouldn't have briefcase info added
            # (though propagator might add its own)
            if "tracestate" in carrier:
                # If there is a tracestate, it shouldn't contain briefcase workflow
                assert "briefcase" not in carrier["tracestate"]
    finally:
        restore_propagator(original)


# 
# Tests: extract with OTel mocking
# 

@patch('briefcase.correlation.propagation.HAS_OTEL', True)
def test_extract_with_otel_calls_propagator(reset_propagator):
    """Test that extract() calls the OTel propagator when available."""
    import briefcase.correlation.propagation as propagation_module
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)
    mock_propagator.extract.return_value = MagicMock()
    mock_context = MagicMock()
    mock_context.attach.return_value = "token-123"

    original_prop = inject_mock_propagator(mock_propagator_class)
    original_ctx = getattr(propagation_module, 'context', None)
    propagation_module.context = mock_context

    try:
        headers = {"traceparent": "00-trace-id-span-id-01"}
        result = TraceContextCarrier.extract(headers)

        mock_propagator.extract.assert_called_once_with(headers)
        mock_context.attach.assert_called_once()
        assert result == "token-123"
    finally:
        restore_propagator(original_prop)
        if original_ctx is not None:
            propagation_module.context = original_ctx
        elif hasattr(propagation_module, 'context'):
            delattr(propagation_module, 'context')


@patch('briefcase.correlation.propagation.HAS_OTEL', True)
def test_extract_returns_context_token(reset_propagator):
    """Test that extract() returns a context token for restoration."""
    import briefcase.correlation.propagation as propagation_module
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)
    mock_ctx_obj = MagicMock()
    mock_propagator.extract.return_value = mock_ctx_obj
    mock_token = MagicMock()
    mock_context = MagicMock()
    mock_context.attach.return_value = mock_token

    original_prop = inject_mock_propagator(mock_propagator_class)
    original_ctx = getattr(propagation_module, 'context', None)
    propagation_module.context = mock_context

    try:
        result = TraceContextCarrier.extract({})

        assert result == mock_token
    finally:
        restore_propagator(original_prop)
        if original_ctx is not None:
            propagation_module.context = original_ctx
        elif hasattr(propagation_module, 'context'):
            delattr(propagation_module, 'context')


# 
# Tests: Error handling
# 

@patch('briefcase.correlation.propagation.HAS_OTEL', True)
def test_inject_handles_propagator_exception(reset_propagator):
    """Test that inject() handles exceptions gracefully."""
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)
    mock_propagator.inject.side_effect = Exception("Propagator error")

    original = inject_mock_propagator(mock_propagator_class)
    try:
        result = TraceContextCarrier.inject()

        # Should return the carrier even if propagator fails
        assert isinstance(result, dict)
    finally:
        restore_propagator(original)


@patch('briefcase.correlation.propagation.HAS_OTEL', True)
def test_extract_handles_propagator_exception(reset_propagator):
    """Test that extract() handles exceptions gracefully."""
    import briefcase.correlation.propagation as propagation_module
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)
    mock_propagator.extract.side_effect = Exception("Propagator error")
    mock_context = MagicMock()

    original_prop = inject_mock_propagator(mock_propagator_class)
    original_ctx = getattr(propagation_module, 'context', None)
    propagation_module.context = mock_context

    try:
        result = TraceContextCarrier.extract({})

        # Should return None if extraction fails
        assert result is None
    finally:
        restore_propagator(original_prop)
        if original_ctx is not None:
            propagation_module.context = original_ctx
        elif hasattr(propagation_module, 'context'):
            delattr(propagation_module, 'context')


@patch('briefcase.correlation.propagation.HAS_OTEL', True)
def test_inject_handles_workflow_error(reset_propagator):
    """Test that inject() handles workflow retrieval errors gracefully."""
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)

    original = inject_mock_propagator(mock_propagator_class)
    try:
        with patch('briefcase.correlation.propagation.get_current_workflow') as mock_get_workflow:
            mock_get_workflow.side_effect = Exception("Workflow error")

            # Should not raise, but handle gracefully
            result = TraceContextCarrier.inject()
            assert isinstance(result, dict)
    finally:
        restore_propagator(original)


# 
# Tests: Propagator caching
# 

@patch('briefcase.correlation.propagation.HAS_OTEL', True)
def test_propagator_is_cached(reset_propagator):
    """Test that propagator is cached after first use."""
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)

    original = inject_mock_propagator(mock_propagator_class)
    try:
        # First call
        TraceContextCarrier.inject()
        assert mock_propagator_class.call_count == 1

        # Second call should use cached propagator
        TraceContextCarrier.inject()
        assert mock_propagator_class.call_count == 1  # Still 1, not 2
    finally:
        restore_propagator(original)


@patch('briefcase.correlation.propagation.HAS_OTEL', True)
def test_cached_propagator_used_for_extract(reset_propagator):
    """Test that extract() uses the same cached propagator."""
    import briefcase.correlation.propagation as propagation_module
    mock_propagator = MagicMock()
    mock_propagator_class = MagicMock(return_value=mock_propagator)
    mock_context = MagicMock()

    original_prop = inject_mock_propagator(mock_propagator_class)
    original_ctx = getattr(propagation_module, 'context', None)
    propagation_module.context = mock_context

    try:
        # First call (inject)
        TraceContextCarrier.inject()
        call_count_after_inject = mock_propagator_class.call_count

        # Second call (extract)
        TraceContextCarrier.extract({})
        call_count_after_extract = mock_propagator_class.call_count

        # No additional constructor calls
        assert call_count_after_extract == call_count_after_inject
    finally:
        restore_propagator(original_prop)
        if original_ctx is not None:
            propagation_module.context = original_ctx
        elif hasattr(propagation_module, 'context'):
            delattr(propagation_module, 'context')


# 
# Tests: Multiple invocations
# 

def test_multiple_inject_calls_without_otel(reset_propagator):
    """Test that multiple inject() calls work correctly without OTel."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        result1 = TraceContextCarrier.inject()
        result2 = TraceContextCarrier.inject()

        assert isinstance(result1, dict)
        assert isinstance(result2, dict)


def test_multiple_extract_calls_without_otel(reset_propagator):
    """Test that multiple extract() calls work correctly without OTel."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        result1 = TraceContextCarrier.extract({})
        result2 = TraceContextCarrier.extract({})

        assert result1 is None
        assert result2 is None


def test_inject_and_extract_sequence(reset_propagator):
    """Test a realistic inject -> extract sequence."""
    with patch('briefcase.correlation.propagation.HAS_OTEL', False):
        # Service A: inject context
        headers = inject_trace_context()
        assert isinstance(headers, dict)

        # Service B: extract context
        token = extract_trace_context(headers)
        # Without OTel, token should be None
        assert token is None
