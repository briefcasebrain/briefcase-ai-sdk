"""
Example: Synchronous multi-agent workflow with automatic correlation.

This example demonstrates how multiple agents are automatically correlated
when executed within a workflow context.
"""

from briefcase.correlation import briefcase_workflow
from unittest.mock import Mock


# Mock Briefcase client and agents for this example
class MockAgent:
    def __init__(self, agent_id: int, agent_type: str):
        self.agent_id = agent_id
        self.agent_type = agent_type

    def process(self, data: dict) -> dict:
        """Simulate agent processing."""
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "input": data,
            "output": f"Processed by {self.agent_type}"
        }


client = Mock()

# Create agents
intake_agent = MockAgent(agent_id=101, agent_type="intake")
clinical_agent = MockAgent(agent_id=102, agent_type="clinical")
decision_agent = MockAgent(agent_id=103, agent_type="decision")


def process_prior_auth(claim_data: dict) -> dict:
    """Process prior authorization through multi-agent workflow."""

    # All agents automatically correlated under this workflow
    with briefcase_workflow("prior_authorization", client) as workflow:
        print(f"Started workflow: {workflow.workflow_id}")
        print()

        # Agent 1: Intake
        print("Agent 1: Intake processing...")
        intake_result = intake_agent.process(claim_data)
        workflow.register_agent("101", "intake")
        print(f"  Result: {intake_result['output']}")
        print()

        # Agent 2: Clinical Review - automatically linked!
        print("Agent 2: Clinical Review processing...")
        clinical_result = clinical_agent.process(intake_result)
        workflow.register_agent("102", "clinical")
        print(f"  Result: {clinical_result['output']}")
        print()

        # Agent 3: Final Decision - automatically linked!
        print("Agent 3: Decision processing...")
        final_decision = decision_agent.process(clinical_result)
        workflow.register_agent("103", "decision")
        print(f"  Result: {final_decision['output']}")
        print()

        print(f"Workflow chain: {'  '.join(workflow._agent_chain)}")
        print(f"Total agents: {workflow._agent_count}")

        return final_decision


# Example usage
if __name__ == "__main__":
    claim = {
        "id": "CLM_123456",
        "patient_id": "PAT_789",
        "procedure": "72148",  # MRI lumbar spine
        "diagnosis": "M54.5"   # Low back pain
    }

    print("=" * 60)
    print("Synchronous Multi-Agent Workflow Example")
    print("=" * 60)
    print()

    result = process_prior_auth(claim)

    print()
    print("=" * 60)
    print("Workflow Complete!")
    print("=" * 60)
    print()
    print("Key Features Demonstrated:")
    print("   Automatic agent correlation")
    print("   Workflow-level tracing")
    print("   Agent chain tracking")
    print("   All agents share workflow.id and trace.id")
    print()
    print("Dashboard view: All three agents appear as:")
    print("  Intake  Clinical  Decision chain")
