"""
Workflow context for automatic multi-agent correlation.
"""

from typing import Optional, List
from uuid import uuid4
from datetime import datetime
from contextlib import contextmanager
import threading
import logging

from briefcase._otel import trace, context, Status, StatusCode, HAS_OTEL
from briefcase.semantic_conventions import workflow as workflow_attrs

logger = logging.getLogger(__name__)

# Thread-local storage for workflow context
_thread_local = threading.local()


class BriefcaseWorkflowContext:
    """
    Manages correlation context for multi-agent workflows.

    All agents executed within this context are automatically correlated
    via distributed trace context propagation.

    Usage:
        with briefcase_workflow("prior_auth", client) as workflow:
            # All agent calls automatically linked
            triage_result = triage_agent.process(claim)
            decision = decision_agent.process(triage_result)
    """

    def __init__(
        self,
        workflow_name: str,
        briefcase_client,
        workflow_id: Optional[str] = None
    ):
        self.workflow_name = workflow_name
        self.briefcase_client = briefcase_client
        self.workflow_id = workflow_id or f"wf_{uuid4().hex[:12]}"

        self._tracer = None
        self._workflow_span = None
        self._token = None
        self._agent_chain: List[str] = []
        self._agent_count = 0
        self._started_at = None

        if HAS_OTEL:
            self._tracer = trace.get_tracer(__name__)

    def __enter__(self):
        """Enter workflow context - create root span."""
        self._started_at = datetime.now()

        if HAS_OTEL and self._tracer:
            try:
                # Create root workflow span
                self._workflow_span = self._tracer.start_span(
                    f"workflow.{self.workflow_name}",
                    attributes={
                        workflow_attrs.WORKFLOW_ID: self.workflow_id,
                        workflow_attrs.WORKFLOW_NAME: self.workflow_name,
                        workflow_attrs.WORKFLOW_STARTED_AT: self._started_at.isoformat()
                    }
                )

                # Set as active span in context
                self._token = context.attach(
                    trace.set_span_in_context(self._workflow_span)
                )
            except Exception as e:
                logger.error(f"Failed to create workflow span: {e}")

        # Store in thread-local for agent access
        _thread_local.workflow_context = self

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit workflow context - finalize span."""
        completed_at = datetime.now()
        duration = (completed_at - self._started_at).total_seconds()

        if HAS_OTEL and self._workflow_span:
            try:
                # Set final attributes
                self._workflow_span.set_attribute(
                    workflow_attrs.WORKFLOW_COMPLETED_AT,
                    completed_at.isoformat()
                )
                self._workflow_span.set_attribute(
                    workflow_attrs.WORKFLOW_AGENT_COUNT,
                    self._agent_count
                )
                self._workflow_span.set_attribute(
                    workflow_attrs.WORKFLOW_AGENT_CHAIN,
                    "  ".join(self._agent_chain)
                )
                self._workflow_span.set_attribute(
                    "workflow.duration_seconds",
                    duration
                )

                # Set status
                if exc_type is not None:
                    self._workflow_span.set_status(
                        Status(StatusCode.ERROR, str(exc_val))
                    )
                    self._workflow_span.set_attribute(
                        workflow_attrs.WORKFLOW_STATUS,
                        "failed"
                    )
                else:
                    self._workflow_span.set_status(Status(StatusCode.OK))
                    self._workflow_span.set_attribute(
                        workflow_attrs.WORKFLOW_STATUS,
                        "success"
                    )

                # End span and detach context
                self._workflow_span.end()
                if self._token:
                    context.detach(self._token)
            except Exception as e:
                logger.error(f"Failed to finalize workflow span: {e}")

        # Clear thread-local
        if hasattr(_thread_local, 'workflow_context'):
            delattr(_thread_local, 'workflow_context')

        return False

    def register_agent(self, agent_id: str, agent_type: str):
        """
        Register an agent execution in this workflow.

        Called automatically by AgentInstrument when agents start.
        """
        self._agent_count += 1
        agent_label = f"{agent_type}({agent_id})"
        self._agent_chain.append(agent_label)

        # Add event to workflow span
        if HAS_OTEL and self._workflow_span:
            try:
                self._workflow_span.add_event(
                    "agent_started",
                    attributes={
                        "agent.id": agent_id,
                        "agent.type": agent_type,
                        "agent.position": self._agent_count
                    }
                )
            except Exception as e:
                logger.error(f"Failed to record agent event: {e}")


@contextmanager
def briefcase_workflow(
    workflow_name: str,
    briefcase_client,
    workflow_id: Optional[str] = None
):
    """
    Convenience function for workflow context.

    Usage:
        with briefcase_workflow("prior_auth", client) as workflow:
            result = agent.process(data)
    """
    ctx = BriefcaseWorkflowContext(
        workflow_name,
        briefcase_client,
        workflow_id
    )

    with ctx:
        yield ctx


def get_current_workflow() -> Optional[BriefcaseWorkflowContext]:
    """Get the current workflow context from thread-local storage."""
    return getattr(_thread_local, 'workflow_context', None)
