use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct ToolCallMetadata {
    pub tool_id: String,
    pub tool_name: String,
    pub arguments: String,
    pub result: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct AgentMetadata {
    pub agent_id: String,
    pub role: String,
    pub handoff_from: Option<String>,
    #[serde(default)]
    pub tool_calls: Vec<ToolCallMetadata>,
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
}

impl AgentMetadata {
    pub fn new(agent_id: impl Into<String>, role: impl Into<String>) -> Self {
        Self {
            agent_id: agent_id.into(),
            role: role.into(),
            handoff_from: None,
            tool_calls: Vec::new(),
            metadata: HashMap::new(),
        }
    }

    pub fn with_handoff(mut self, from: String) -> Self {
        self.handoff_from = Some(from);
        self
    }

    pub fn add_tool_call(&mut self, tool_call: ToolCallMetadata) {
        self.tool_calls.push(tool_call);
    }
}
