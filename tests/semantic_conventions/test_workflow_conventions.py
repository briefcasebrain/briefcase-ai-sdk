"""
Tests for multi-agent workflow correlation semantic conventions.

Covers:
  - All constants are defined as strings
  - Constant values follow naming convention (workflow.*, agent.*, trace.*, parent.*)
  - Workflow-level attribute constants
  - Agent-level attribute constants
  - Correlation attribute constants
"""

import pytest
from briefcase.semantic_conventions import workflow


# 
# Helper function
# 

def get_module_constants(module):
    """Get all uppercase constants from a module."""
    return {
        name: getattr(module, name)
        for name in dir(module)
        if name.isupper() and not name.startswith('_')
    }


# 
# Tests: All constants defined
# 

def test_workflow_id_defined():
    """Test that WORKFLOW_ID is defined."""
    assert hasattr(workflow, 'WORKFLOW_ID')


def test_workflow_name_defined():
    """Test that WORKFLOW_NAME is defined."""
    assert hasattr(workflow, 'WORKFLOW_NAME')


def test_workflow_started_at_defined():
    """Test that WORKFLOW_STARTED_AT is defined."""
    assert hasattr(workflow, 'WORKFLOW_STARTED_AT')


def test_workflow_completed_at_defined():
    """Test that WORKFLOW_COMPLETED_AT is defined."""
    assert hasattr(workflow, 'WORKFLOW_COMPLETED_AT')


def test_workflow_status_defined():
    """Test that WORKFLOW_STATUS is defined."""
    assert hasattr(workflow, 'WORKFLOW_STATUS')


def test_workflow_agent_count_defined():
    """Test that WORKFLOW_AGENT_COUNT is defined."""
    assert hasattr(workflow, 'WORKFLOW_AGENT_COUNT')


def test_workflow_agent_chain_defined():
    """Test that WORKFLOW_AGENT_CHAIN is defined."""
    assert hasattr(workflow, 'WORKFLOW_AGENT_CHAIN')


def test_agent_workflow_id_defined():
    """Test that AGENT_WORKFLOW_ID is defined."""
    assert hasattr(workflow, 'AGENT_WORKFLOW_ID')


def test_agent_parent_id_defined():
    """Test that AGENT_PARENT_ID is defined."""
    assert hasattr(workflow, 'AGENT_PARENT_ID')


def test_agent_position_in_chain_defined():
    """Test that AGENT_POSITION_IN_CHAIN is defined."""
    assert hasattr(workflow, 'AGENT_POSITION_IN_CHAIN')


def test_trace_id_defined():
    """Test that TRACE_ID is defined."""
    assert hasattr(workflow, 'TRACE_ID')


def test_parent_span_id_defined():
    """Test that PARENT_SPAN_ID is defined."""
    assert hasattr(workflow, 'PARENT_SPAN_ID')


# 
# Tests: All constants are strings
# 

def test_all_constants_are_strings():
    """Test that all constants are strings."""
    constants = get_module_constants(workflow)
    for name, value in constants.items():
        assert isinstance(value, str), f"{name} is not a string, got {type(value)}"


# 
# Tests: Naming convention (workflow.*, agent.*, trace.*, parent.*)
# 

def test_all_constants_follow_naming_convention():
    """Test that all constants follow expected naming conventions."""
    constants = get_module_constants(workflow)
    valid_prefixes = ("workflow.", "agent.", "trace.", "parent.")
    for name, value in constants.items():
        assert value.startswith(valid_prefixes), \
            f"{name}='{value}' does not start with any of {valid_prefixes}"


def test_workflow_constants_have_correct_prefix():
    """Test that workflow constants have workflow.* prefix."""
    assert workflow.WORKFLOW_ID.startswith("workflow.")
    assert workflow.WORKFLOW_NAME.startswith("workflow.")
    assert workflow.WORKFLOW_STARTED_AT.startswith("workflow.")
    assert workflow.WORKFLOW_COMPLETED_AT.startswith("workflow.")
    assert workflow.WORKFLOW_STATUS.startswith("workflow.")
    assert workflow.WORKFLOW_AGENT_COUNT.startswith("workflow.")
    assert workflow.WORKFLOW_AGENT_CHAIN.startswith("workflow.")


def test_agent_constants_have_correct_prefix():
    """Test that agent constants have agent.* prefix."""
    assert workflow.AGENT_WORKFLOW_ID.startswith("agent.")
    assert workflow.AGENT_PARENT_ID.startswith("agent.")
    assert workflow.AGENT_POSITION_IN_CHAIN.startswith("agent.")


def test_trace_constant_has_correct_prefix():
    """Test that trace constant has trace.* prefix."""
    assert workflow.TRACE_ID.startswith("trace.")


def test_parent_constant_has_correct_prefix():
    """Test that parent constant has parent.* prefix."""
    assert workflow.PARENT_SPAN_ID.startswith("parent.")


# 
# Tests: Specific constant values
# 

def test_workflow_id_value():
    """Test WORKFLOW_ID value."""
    assert workflow.WORKFLOW_ID == "workflow.id"


def test_workflow_name_value():
    """Test WORKFLOW_NAME value."""
    assert workflow.WORKFLOW_NAME == "workflow.name"


def test_workflow_started_at_value():
    """Test WORKFLOW_STARTED_AT value."""
    assert workflow.WORKFLOW_STARTED_AT == "workflow.started_at"


def test_workflow_completed_at_value():
    """Test WORKFLOW_COMPLETED_AT value."""
    assert workflow.WORKFLOW_COMPLETED_AT == "workflow.completed_at"


def test_workflow_status_value():
    """Test WORKFLOW_STATUS value."""
    assert workflow.WORKFLOW_STATUS == "workflow.status"


def test_workflow_agent_count_value():
    """Test WORKFLOW_AGENT_COUNT value."""
    assert workflow.WORKFLOW_AGENT_COUNT == "workflow.agent_count"


def test_workflow_agent_chain_value():
    """Test WORKFLOW_AGENT_CHAIN value."""
    assert workflow.WORKFLOW_AGENT_CHAIN == "workflow.agent_chain"


def test_agent_workflow_id_value():
    """Test AGENT_WORKFLOW_ID value."""
    assert workflow.AGENT_WORKFLOW_ID == "agent.workflow_id"


def test_agent_parent_id_value():
    """Test AGENT_PARENT_ID value."""
    assert workflow.AGENT_PARENT_ID == "agent.parent_id"


def test_agent_position_in_chain_value():
    """Test AGENT_POSITION_IN_CHAIN value."""
    assert workflow.AGENT_POSITION_IN_CHAIN == "agent.position"


def test_trace_id_value():
    """Test TRACE_ID value."""
    assert workflow.TRACE_ID == "trace.id"


def test_parent_span_id_value():
    """Test PARENT_SPAN_ID value."""
    assert workflow.PARENT_SPAN_ID == "parent.span_id"


# 
# Tests: Constant count and coverage
# 

def test_expected_number_of_constants():
    """Test that the module has the expected number of constants."""
    constants = get_module_constants(workflow)
    # 7 Workflow + 3 Agent + 1 Trace + 1 Parent = 12
    assert len(constants) == 12, f"Expected 12 constants, got {len(constants)}"


def test_no_duplicate_values():
    """Test that no two constants have the same value."""
    constants = get_module_constants(workflow)
    values = list(constants.values())
    assert len(values) == len(set(values)), "Duplicate constant values found"


# 
# Tests: Semantic grouping
# 

def test_workflow_group_completeness():
    """Test that all workflow-level constants are present."""
    workflow_constants = [
        workflow.WORKFLOW_ID,
        workflow.WORKFLOW_NAME,
        workflow.WORKFLOW_STARTED_AT,
        workflow.WORKFLOW_COMPLETED_AT,
        workflow.WORKFLOW_STATUS,
        workflow.WORKFLOW_AGENT_COUNT,
        workflow.WORKFLOW_AGENT_CHAIN,
    ]
    assert len(workflow_constants) == 7
    assert all(isinstance(c, str) for c in workflow_constants)


def test_agent_group_completeness():
    """Test that all agent-level constants are present."""
    agent_constants = [
        workflow.AGENT_WORKFLOW_ID,
        workflow.AGENT_PARENT_ID,
        workflow.AGENT_POSITION_IN_CHAIN,
    ]
    assert len(agent_constants) == 3
    assert all(isinstance(c, str) for c in agent_constants)


def test_correlation_group_completeness():
    """Test that all correlation constants are present."""
    correlation_constants = [
        workflow.TRACE_ID,
        workflow.PARENT_SPAN_ID,
    ]
    assert len(correlation_constants) == 2
    assert all(isinstance(c, str) for c in correlation_constants)


# 
# Tests: Logical relationships and usage patterns
# 

def test_workflow_and_agent_constants_relate():
    """Test that workflow and agent constants form a coherent model."""
    # Agent should reference a workflow
    assert hasattr(workflow, 'WORKFLOW_ID')
    assert hasattr(workflow, 'AGENT_WORKFLOW_ID')
    # Both should be strings
    assert isinstance(workflow.WORKFLOW_ID, str)
    assert isinstance(workflow.AGENT_WORKFLOW_ID, str)


def test_trace_and_parent_for_correlation():
    """Test that trace and parent constants support correlation."""
    # For linking spans
    assert hasattr(workflow, 'TRACE_ID')
    assert hasattr(workflow, 'PARENT_SPAN_ID')
    assert isinstance(workflow.TRACE_ID, str)
    assert isinstance(workflow.PARENT_SPAN_ID, str)


def test_workflow_timing_constants():
    """Test that workflow has timing constants for lifecycle tracking."""
    timing_constants = [
        workflow.WORKFLOW_STARTED_AT,
        workflow.WORKFLOW_COMPLETED_AT,
    ]
    assert all(isinstance(c, str) for c in timing_constants)
    # Both should use .at suffix for timestamps
    assert all("at" in c for c in timing_constants)


def test_workflow_execution_constants():
    """Test that workflow has constants for execution metadata."""
    execution_constants = [
        workflow.WORKFLOW_STATUS,
        workflow.WORKFLOW_AGENT_COUNT,
        workflow.WORKFLOW_AGENT_CHAIN,
    ]
    assert all(isinstance(c, str) for c in execution_constants)


def test_agent_position_implies_ordering():
    """Test that agent position constant implies ordering capability."""
    # The position constant should support defining agent order
    assert workflow.AGENT_POSITION_IN_CHAIN == "agent.position"
    # This allows agents to express their position in the chain
