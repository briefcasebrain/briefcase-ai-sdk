//! Core data models for AI decision tracking and observability

use super::agent::AgentMetadata;
use super::hardware::HardwareMetadata;
use super::scorecard::{ExperimentMetadata, Scorecard};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Input {
    pub name: String,
    pub value: serde_json::Value,
    pub data_type: String,
    #[serde(default = "default_schema_version")]
    pub schema_version: String,
}

impl Input {
    pub fn new(
        name: impl Into<String>,
        value: serde_json::Value,
        data_type: impl Into<String>,
    ) -> Self {
        Self {
            name: name.into(),
            value,
            data_type: data_type.into(),
            schema_version: default_schema_version(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Output {
    pub name: String,
    pub value: serde_json::Value,
    pub data_type: String,
    pub confidence: Option<f64>,
    #[serde(default = "default_schema_version")]
    pub schema_version: String,
}

impl Output {
    pub fn new(
        name: impl Into<String>,
        value: serde_json::Value,
        data_type: impl Into<String>,
    ) -> Self {
        Self {
            name: name.into(),
            value,
            data_type: data_type.into(),
            confidence: None,
            schema_version: default_schema_version(),
        }
    }
    pub fn with_confidence(mut self, confidence: f64) -> Self {
        self.confidence = Some(confidence.clamp(0.0, 1.0));
        self
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ModelParameters {
    pub model_name: String,
    pub model_version: Option<String>,
    pub provider: Option<String>,
    #[serde(default)]
    pub parameters: HashMap<String, serde_json::Value>,
    #[serde(default)]
    pub hyperparameters: HashMap<String, serde_json::Value>,
    pub weights_hash: Option<String>,
}

impl ModelParameters {
    pub fn new(model_name: impl Into<String>) -> Self {
        Self {
            model_name: model_name.into(),
            model_version: None,
            provider: None,
            parameters: HashMap::new(),
            hyperparameters: HashMap::new(),
            weights_hash: None,
        }
    }
    pub fn with_provider(mut self, provider: impl Into<String>) -> Self {
        self.provider = Some(provider.into());
        self
    }
    pub fn with_version(mut self, version: impl Into<String>) -> Self {
        self.model_version = Some(version.into());
        self
    }
    pub fn with_parameter(mut self, key: impl Into<String>, value: serde_json::Value) -> Self {
        self.parameters.insert(key.into(), value);
        self
    }
    pub fn with_hyperparameter(mut self, key: impl Into<String>, value: serde_json::Value) -> Self {
        self.hyperparameters.insert(key.into(), value);
        self
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct ExecutionContext {
    pub runtime_version: Option<String>,
    #[serde(default)]
    pub dependencies: HashMap<String, String>,
    pub random_seed: Option<i64>,
    #[serde(default)]
    pub environment_variables: HashMap<String, String>,
    #[serde(default)]
    pub hardware_info: HashMap<String, serde_json::Value>,
}

impl ExecutionContext {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn with_runtime_version(mut self, v: impl Into<String>) -> Self {
        self.runtime_version = Some(v.into());
        self
    }
    pub fn with_random_seed(mut self, s: i64) -> Self {
        self.random_seed = Some(s);
        self
    }
    pub fn with_dependency(mut self, n: impl Into<String>, v: impl Into<String>) -> Self {
        self.dependencies.insert(n.into(), v.into());
        self
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SnapshotMetadata {
    pub snapshot_id: Uuid,
    pub timestamp: DateTime<Utc>,
    pub schema_version: String,
    pub sdk_version: String,
    pub created_by: Option<String>,
    pub checksum: Option<String>,
}

impl Default for SnapshotMetadata {
    fn default() -> Self {
        Self::new()
    }
}

impl SnapshotMetadata {
    pub fn new() -> Self {
        Self {
            snapshot_id: Uuid::new_v4(),
            timestamp: Utc::now(),
            schema_version: "1.0".to_string(),
            sdk_version: "2.5.9".to_string(),
            created_by: None,
            checksum: None,
        }
    }
    pub fn compute_checksum(&mut self, data: &[u8]) {
        let mut hasher = Sha256::new();
        hasher.update(data);
        self.checksum = Some(format!("{:x}", hasher.finalize()));
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DecisionSnapshot {
    pub metadata: SnapshotMetadata,
    pub context: ExecutionContext,
    pub function_name: String,
    pub module_name: Option<String>,
    pub inputs: Vec<Input>,
    pub outputs: Vec<Output>,
    pub model_parameters: Option<ModelParameters>,
    pub execution_time_ms: Option<f64>,
    pub error: Option<String>,
    pub error_type: Option<String>,
    pub scorecard: Option<Scorecard>,
    pub experiment: Option<ExperimentMetadata>,
    pub agent: Option<AgentMetadata>,
    pub hardware: Option<HardwareMetadata>,
    #[serde(default)]
    pub tags: HashMap<String, String>,
}

impl DecisionSnapshot {
    pub fn new(function_name: impl Into<String>) -> Self {
        let mut s = Self {
            metadata: SnapshotMetadata::new(),
            context: ExecutionContext::new(),
            function_name: function_name.into(),
            module_name: None,
            inputs: Vec::new(),
            outputs: Vec::new(),
            model_parameters: None,
            execution_time_ms: None,
            error: None,
            error_type: None,
            scorecard: None,
            experiment: None,
            agent: None,
            hardware: None,
            tags: HashMap::new(),
        };
        s.update_checksum();
        s
    }
    pub fn with_module(mut self, m: impl Into<String>) -> Self {
        self.module_name = Some(m.into());
        self.update_checksum();
        self
    }
    pub fn with_context(mut self, c: ExecutionContext) -> Self {
        self.context = c;
        self.update_checksum();
        self
    }
    pub fn add_input(mut self, i: Input) -> Self {
        self.inputs.push(i);
        self.update_checksum();
        self
    }
    pub fn add_output(mut self, o: Output) -> Self {
        self.outputs.push(o);
        self.update_checksum();
        self
    }
    pub fn with_model_parameters(mut self, p: ModelParameters) -> Self {
        self.model_parameters = Some(p);
        self.update_checksum();
        self
    }
    pub fn with_execution_time(mut self, t: f64) -> Self {
        self.execution_time_ms = Some(t);
        self.update_checksum();
        self
    }
    pub fn with_error(mut self, e: impl Into<String>, et: Option<String>) -> Self {
        self.error = Some(e.into());
        self.error_type = et;
        self.update_checksum();
        self
    }
    pub fn with_scorecard(mut self, s: Scorecard) -> Self {
        self.scorecard = Some(s);
        self.update_checksum();
        self
    }
    pub fn with_agent(mut self, a: AgentMetadata) -> Self {
        self.agent = Some(a);
        self.update_checksum();
        self
    }
    pub fn with_hardware(mut self, h: HardwareMetadata) -> Self {
        self.hardware = Some(h);
        self.update_checksum();
        self
    }
    pub fn add_tag(mut self, k: impl Into<String>, v: impl Into<String>) -> Self {
        self.tags.insert(k.into(), v.into());
        self.update_checksum();
        self
    }

    fn update_checksum(&mut self) {
        if let Ok(b) = serde_json::to_vec(self) {
            self.metadata.compute_checksum(&b);
        }
    }
    /// Hash of the question: function name, input names and values, and the
    /// model name. Two decisions with the same inputs share a fingerprint
    /// whatever they answered, which is what makes it useful for grouping and
    /// lookup. To detect a changed answer, use [`Self::content_hash`].
    pub fn fingerprint(&self) -> String {
        let mut hasher = Sha256::new();
        hasher.update(self.function_name.as_bytes());
        for i in &self.inputs {
            hasher.update(i.name.as_bytes());
            hasher.update(
                serde_json::to_string(&i.value)
                    .unwrap_or_default()
                    .as_bytes(),
            );
        }
        if let Some(p) = &self.model_parameters {
            hasher.update(p.model_name.as_bytes());
        }
        format!("{:x}", hasher.finalize())
    }

    /// Hash of everything that was decided: inputs, outputs, model parameters
    /// and their values, module, tags, and any error.
    ///
    /// Volatile metadata (snapshot id, timestamps, execution time, the rolling
    /// `metadata.checksum`) is excluded, so the digest depends only on content
    /// and a verifier holding the record can recompute it. Map keys are sorted
    /// so construction order does not change the result.
    ///
    /// The hash is unkeyed. It detects corruption and casual edits; anyone who
    /// can rewrite the record can also recompute the digest, so store it
    /// somewhere the record's holder cannot reach when it has to stand up to a
    /// motivated party.
    pub fn content_hash(&self) -> String {
        let mut hasher = Sha256::new();
        let mut field = |label: &str, value: &str| {
            // Length-prefix each part so no concatenation of one field can
            // impersonate another.
            hasher.update(label.as_bytes());
            hasher.update(value.len().to_le_bytes());
            hasher.update(value.as_bytes());
        };

        field("function_name", &self.function_name);
        field("module_name", self.module_name.as_deref().unwrap_or(""));

        for i in &self.inputs {
            field("input.name", &i.name);
            field("input.type", &i.data_type);
            field("input.value", &canonical_json(&i.value));
        }
        for o in &self.outputs {
            field("output.name", &o.name);
            field("output.type", &o.data_type);
            field("output.value", &canonical_json(&o.value));
            field(
                "output.confidence",
                &o.confidence.map(|c| c.to_string()).unwrap_or_default(),
            );
        }

        if let Some(p) = &self.model_parameters {
            field("model.name", &p.model_name);
            field("model.version", p.model_version.as_deref().unwrap_or(""));
            field("model.provider", p.provider.as_deref().unwrap_or(""));
            for (k, v) in sorted_pairs(&p.parameters) {
                field("model.param", k);
                field("model.param.value", &canonical_json(v));
            }
            for (k, v) in sorted_pairs(&p.hyperparameters) {
                field("model.hyper", k);
                field("model.hyper.value", &canonical_json(v));
            }
        }

        let mut tags: Vec<_> = self.tags.iter().collect();
        tags.sort_by(|a, b| a.0.cmp(b.0));
        for (k, v) in tags {
            field("tag", k);
            field("tag.value", v);
        }

        field("error", self.error.as_deref().unwrap_or(""));
        field("error_type", self.error_type.as_deref().unwrap_or(""));

        format!("{:x}", hasher.finalize())
    }
}

/// Serialize a value with object keys sorted, so two structurally equal values
/// hash the same however they were built.
fn canonical_json(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::Object(map) => {
            let mut keys: Vec<_> = map.keys().collect();
            keys.sort();
            let parts: Vec<String> = keys
                .into_iter()
                .map(|k| format!("{}:{}", k, canonical_json(&map[k])))
                .collect();
            format!("{{{}}}", parts.join(","))
        }
        serde_json::Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(canonical_json).collect();
            format!("[{}]", parts.join(","))
        }
        other => other.to_string(),
    }
}

fn sorted_pairs(map: &HashMap<String, serde_json::Value>) -> Vec<(&String, &serde_json::Value)> {
    let mut pairs: Vec<_> = map.iter().collect();
    pairs.sort_by(|a, b| a.0.cmp(b.0));
    pairs
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Snapshot {
    pub metadata: SnapshotMetadata,
    pub decisions: Vec<DecisionSnapshot>,
    pub snapshot_type: SnapshotType,
}

impl Snapshot {
    pub fn new(t: SnapshotType) -> Self {
        let mut s = Self {
            metadata: SnapshotMetadata::new(),
            decisions: Vec::new(),
            snapshot_type: t,
        };
        s.update_checksum();
        s
    }
    pub fn add_decision(&mut self, d: DecisionSnapshot) {
        self.decisions.push(d);
        self.update_checksum();
    }
    pub fn with_created_by(mut self, c: impl Into<String>) -> Self {
        self.metadata.created_by = Some(c.into());
        self.update_checksum();
        self
    }
    fn update_checksum(&mut self) {
        if let Ok(b) = serde_json::to_vec(self) {
            self.metadata.compute_checksum(&b);
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum SnapshotType {
    Decision,
    Batch,
    Session,
}

fn default_schema_version() -> String {
    "1.0".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn decision(answer: &str) -> DecisionSnapshot {
        DecisionSnapshot::new("classify_ticket")
            .add_input(Input::new("text", json!("reset my password"), "string"))
            .add_output(Output::new("category", json!(answer), "string"))
            .with_model_parameters(
                ModelParameters::new("claude-opus-4-5").with_parameter("temperature", json!(0.0)),
            )
    }

    #[test]
    fn fingerprint_identifies_the_inputs_not_the_answer() {
        // Documented behavior: the fingerprint groups the same question asked twice.
        assert_eq!(
            decision("billing").fingerprint(),
            decision("shipping").fingerprint()
        );
    }

    #[test]
    fn content_hash_covers_the_outputs() {
        assert_ne!(
            decision("billing").content_hash(),
            decision("shipping").content_hash()
        );
    }

    #[test]
    fn content_hash_is_stable_for_identical_content() {
        assert_eq!(
            decision("billing").content_hash(),
            decision("billing").content_hash()
        );
    }

    #[test]
    fn content_hash_ignores_construction_order() {
        let a = DecisionSnapshot::new("f")
            .add_input(Input::new("x", json!(1), "int"))
            .with_module("m");
        let b = DecisionSnapshot::new("f")
            .with_module("m")
            .add_input(Input::new("x", json!(1), "int"));
        assert_eq!(a.content_hash(), b.content_hash());
    }

    #[test]
    fn content_hash_covers_model_parameter_values() {
        let cold = DecisionSnapshot::new("f").with_model_parameters(
            ModelParameters::new("m").with_parameter("temperature", json!(0.0)),
        );
        let hot = DecisionSnapshot::new("f").with_model_parameters(
            ModelParameters::new("m").with_parameter("temperature", json!(1.0)),
        );
        assert_ne!(cold.content_hash(), hot.content_hash());
        // The fingerprint hashes only the model name, so it cannot see this.
        assert_eq!(cold.fingerprint(), hot.fingerprint());
    }

    #[test]
    fn content_hash_covers_tags_and_errors() {
        let plain = decision("billing");
        let tagged = decision("billing").add_tag("environment", "production");
        let failed = decision("billing").with_error("boom", None);
        assert_ne!(plain.content_hash(), tagged.content_hash());
        assert_ne!(plain.content_hash(), failed.content_hash());
    }

    #[test]
    fn content_hash_ignores_volatile_metadata() {
        // Two records of the same decision differ in id and timestamp; the hash is
        // over what was decided, so a verifier can recompute it from the content.
        let a = decision("billing");
        let mut b = decision("billing");
        b.metadata.snapshot_id = uuid::Uuid::new_v4();
        assert_eq!(a.content_hash(), b.content_hash());
    }
}
