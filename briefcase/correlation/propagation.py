"""
Utilities for propagating trace context across processes and services.
"""

from typing import Dict, Optional
from briefcase._logging import get_logger

from briefcase._otel import context, TraceContextTextMapPropagator, HAS_OTEL
from briefcase.correlation.workflow import get_current_workflow

logger = get_logger(__name__)


class TraceContextCarrier:
    """
    Carrier for extracting and injecting trace context into HTTP headers,
    message queues, or other transport mechanisms.

    Usage:
        # Service A: Inject context into HTTP headers
        headers = TraceContextCarrier.inject()
        requests.post(url, headers=headers)

        # Service B: Extract context from headers
        with TraceContextCarrier.extract(request.headers):
            # Agents automatically join the workflow
            result = agent.process(data)
    """

    _propagator = None

    @classmethod
    def _get_propagator(cls):
        """Lazy initialization of propagator."""
        if cls._propagator is None and HAS_OTEL:
            cls._propagator = TraceContextTextMapPropagator()
        return cls._propagator

    @classmethod
    def inject(cls, carrier: Optional[Dict] = None) -> Dict:
        """
        Inject current trace context into carrier (dict/headers).

        Returns a dict with W3C Trace Context headers:
        - traceparent: trace-id, span-id, trace-flags
        - tracestate: vendor-specific state (e.g., workflow_id)
        """
        if carrier is None:
            carrier = {}

        if not HAS_OTEL:
            logger.debug("OpenTelemetry not available, cannot inject trace context")
            return carrier

        propagator = cls._get_propagator()
        if propagator:
            try:
                propagator.inject(carrier)

                # Add Briefcase-specific state
                workflow = get_current_workflow()
                if workflow:
                    # Add workflow info to tracestate
                    existing_state = carrier.get("tracestate", "")
                    workflow_state = f"briefcase=workflow_id:{workflow.workflow_id}"
                    carrier["tracestate"] = f"{workflow_state},{existing_state}" if existing_state else workflow_state
            except Exception as e:
                logger.error(f"Failed to inject trace context: {e}")

        return carrier

    @classmethod
    def extract(cls, carrier: Dict):
        """
        Extract trace context from carrier and set as current context.

        Returns a context token that can be used to restore the context.

        Usage:
            token = TraceContextCarrier.extract(request.headers)
            # Code here runs in the propagated trace context
            # Optionally: context.detach(token) when done
        """
        if not HAS_OTEL:
            logger.debug("OpenTelemetry not available, cannot extract trace context")
            return None

        propagator = cls._get_propagator()
        if propagator:
            try:
                ctx = propagator.extract(carrier)
                return context.attach(ctx)
            except Exception as e:
                logger.error(f"Failed to extract trace context: {e}")
                return None

        return None


def inject_trace_context(headers: Optional[Dict] = None) -> Dict:
    """Convenience function to inject trace context into HTTP headers."""
    return TraceContextCarrier.inject(headers)


def extract_trace_context(headers: Dict):
    """Convenience function to extract trace context from HTTP headers."""
    return TraceContextCarrier.extract(headers)
