use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct Scorecard {
    pub metrics: HashMap<String, f64>,
    pub weights: HashMap<String, f64>,
    pub metadata: HashMap<String, serde_json::Value>,
}

impl Scorecard {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn add_score(&mut self, name: String, value: f64, weight: f64) {
        self.metrics.insert(name.clone(), value);
        self.weights.insert(name, weight);
    }

    pub fn composite_score(&self) -> f64 {
        if self.metrics.is_empty() {
            return 0.0;
        }
        let mut total_weight = 0.0;
        let mut weighted_sum = 0.0;
        for (name, val) in &self.metrics {
            let weight = self.weights.get(name).unwrap_or(&1.0);
            weighted_sum += val * weight;
            total_weight += weight;
        }
        if total_weight == 0.0 {
            0.0
        } else {
            weighted_sum / total_weight
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ExperimentMetadata {
    pub experiment_id: String,
    pub run_index: u32,
    pub total_runs: u32,
    #[serde(default)]
    pub tags: HashMap<String, String>,
}
