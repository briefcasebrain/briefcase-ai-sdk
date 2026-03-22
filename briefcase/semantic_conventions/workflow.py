"""
Semantic conventions for multi-agent workflow correlation.
"""

# Workflow-level attributes
WORKFLOW_ID = "workflow.id"
WORKFLOW_NAME = "workflow.name"
WORKFLOW_STARTED_AT = "workflow.started_at"
WORKFLOW_COMPLETED_AT = "workflow.completed_at"
WORKFLOW_STATUS = "workflow.status"  # success, failed, partial
WORKFLOW_AGENT_COUNT = "workflow.agent_count"
WORKFLOW_AGENT_CHAIN = "workflow.agent_chain"  # "intake  clinical  decision"

# Agent-level attributes (within workflow)
AGENT_WORKFLOW_ID = "agent.workflow_id"
AGENT_PARENT_ID = "agent.parent_id"
AGENT_POSITION_IN_CHAIN = "agent.position"  # 1, 2, 3, etc.

# Correlation attributes
TRACE_ID = "trace.id"
PARENT_SPAN_ID = "parent.span_id"
