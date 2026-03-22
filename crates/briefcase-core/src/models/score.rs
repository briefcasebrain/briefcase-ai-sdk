use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct Score {
    pub success: bool,
    pub primary_value: Option<f64>,
    #[serde(default)]
    pub metrics: HashMap<String, f64>,
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
}

impl Score {
    pub fn new(success: bool) -> Self {
        Self {
            success,
            primary_value: None,
            metrics: HashMap::new(),
            metadata: HashMap::new(),
        }
    }

    pub fn with_primary_value(mut self, value: f64) -> Self {
        self.primary_value = Some(value);
        self
    }

    pub fn add_metric(mut self, name: impl Into<String>, value: f64) -> Self {
        self.metrics.insert(name.into(), value);
        self
    }

    pub fn add_metadata(mut self, key: impl Into<String>, value: serde_json::Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }
}
