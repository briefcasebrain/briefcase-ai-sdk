use crate::models::{DecisionSnapshot, Output};

#[cfg(feature = "sqlite-storage")]
use crate::storage::StorageBackend;
use serde::{Deserialize, Serialize};
use thiserror::Error;
#[cfg(feature = "async")]
use tokio;

pub mod executor;
#[cfg(feature = "async")]
pub use executor::ModelExecutor;
pub use executor::{
    ComparisonResult, ExecutionConfig, ExecutionResult, FieldComparison, SyncModelExecutor,
};

#[cfg(feature = "sqlite-storage")]
pub mod sync;
#[cfg(feature = "sqlite-storage")]
pub use sync::SyncReplayEngine;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum ReplayMode {
    Strict,         // Exact byte-for-byte match required
    Tolerant,       // Allow minor differences (whitespace, formatting)
    ValidationOnly, // Validate inputs/context without re-executing
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum ReplayStatus {
    Pending,
    Running,
    Success,
    Failed,
    Partial, // Some decisions matched, some didn't
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReplayResult {
    pub status: ReplayStatus,
    pub original_snapshot: DecisionSnapshot,
    pub replay_output: Option<serde_json::Value>,
    pub outputs_match: bool,
    pub diff: Option<SnapshotDiff>,
    pub policy_violations: Vec<PolicyViolation>,
    pub execution_time_ms: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotDiff {
    pub inputs_changed: bool,
    pub outputs_changed: bool,
    pub model_params_changed: bool,
    pub execution_time_delta_ms: f64,
    pub changes: Vec<FieldChange>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FieldChange {
    pub field_path: String,
    pub old_value: serde_json::Value,
    pub new_value: serde_json::Value,
    pub change_type: ChangeType,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum ChangeType {
    Added,
    Removed,
    Modified,
}

#[derive(Debug, Clone)]
pub struct ReplayPolicy {
    pub name: String,
    pub rules: Vec<ValidationRule>,
}

impl ReplayPolicy {
    pub fn new(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            rules: Vec::new(),
        }
    }

    pub fn add_rule(mut self, rule: ValidationRule) -> Self {
        self.rules.push(rule);
        self
    }

    pub fn with_exact_match(mut self, field: impl Into<String>) -> Self {
        self.rules.push(ValidationRule {
            field: field.into(),
            comparator: Comparator::ExactMatch,
            threshold: 1.0,
        });
        self
    }

    pub fn with_similarity_threshold(mut self, field: impl Into<String>, threshold: f64) -> Self {
        self.rules.push(ValidationRule {
            field: field.into(),
            comparator: Comparator::SemanticSimilarity,
            threshold,
        });
        self
    }
}

#[derive(Debug, Clone)]
pub struct ValidationRule {
    pub field: String,
    pub comparator: Comparator,
    pub threshold: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Comparator {
    ExactMatch,
    SemanticSimilarity,
    MaxIncreasePercent,
    MaxDecreasePercent,
    WithinRange,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyViolation {
    pub rule_name: String,
    pub field: String,
    pub expected: String,
    pub actual: String,
    pub message: String,
}

/// The outputs a replay produced, or `None` when nothing re-executed.
#[cfg(feature = "sqlite-storage")]
fn replayed_outputs(result: &ReplayResult) -> Option<Vec<Output>> {
    let value = result.replay_output.as_ref()?;
    serde_json::from_value::<Vec<Output>>(value.clone()).ok()
}

/// Resolve a rule's field against a replay's outputs. `output` and an empty
/// field both mean "the first output", matching `extract_field_value`.
#[cfg(feature = "sqlite-storage")]
fn field_from_outputs(outputs: &[Output], field: &str) -> Option<serde_json::Value> {
    if field == "output" || field.is_empty() {
        return outputs.first().map(|o| o.value.clone());
    }
    outputs
        .iter()
        .find(|o| o.name == field)
        .map(|o| o.value.clone())
}

/// Render a JSON value for a violation message: strings unquoted, everything
/// else as compact JSON.
#[cfg(feature = "sqlite-storage")]
fn value_text(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

#[cfg(feature = "sqlite-storage")]
fn text_similarity(a: &str, b: &str) -> f64 {
    if a == b {
        1.0
    } else {
        strsim::normalized_levenshtein(a, b)
    }
}

#[cfg(feature = "sqlite-storage")]
#[derive(Clone)]
pub struct ReplayEngine<S: StorageBackend> {
    storage: S,
    default_mode: ReplayMode,
    #[cfg(feature = "async")]
    executor: Option<std::sync::Arc<dyn ModelExecutor>>,
}

#[cfg(feature = "sqlite-storage")]
impl<S: StorageBackend> ReplayEngine<S> {
    pub fn new(storage: S) -> Self {
        Self {
            storage,
            default_mode: ReplayMode::Tolerant,
            #[cfg(feature = "async")]
            executor: None,
        }
    }

    pub fn with_mode(storage: S, mode: ReplayMode) -> Self {
        Self {
            storage,
            default_mode: mode,
            #[cfg(feature = "async")]
            executor: None,
        }
    }

    /// Set a model executor for the replay engine
    #[cfg(feature = "async")]
    pub fn with_executor(mut self, executor: std::sync::Arc<dyn ModelExecutor>) -> Self {
        self.executor = Some(executor);
        self
    }

    /// Set the executor in place, for callers holding the engine behind a
    /// `&mut` (the Python binding, chiefly).
    #[cfg(feature = "async")]
    pub fn set_executor(&mut self, executor: std::sync::Arc<dyn ModelExecutor>) {
        self.executor = Some(executor);
    }

    /// Get a reference to the current executor, if any
    #[cfg(feature = "async")]
    pub fn executor(&self) -> Option<&dyn ModelExecutor> {
        self.executor.as_ref().map(|arc| arc.as_ref())
    }

    /// Get the default replay mode
    pub fn default_mode(&self) -> &ReplayMode {
        &self.default_mode
    }

    /// Replay a snapshot by ID
    pub async fn replay(
        &self,
        snapshot_id: &str,
        mode: Option<ReplayMode>,
        _context_overrides: Option<std::collections::HashMap<String, serde_json::Value>>,
    ) -> Result<ReplayResult, ReplayError> {
        let start_time = std::time::Instant::now();
        let replay_mode = mode.unwrap_or_else(|| self.default_mode.clone());

        // Load the original snapshot
        let original_snapshot = match self.storage.load_decision(snapshot_id).await {
            Ok(snapshot) => snapshot,
            Err(e) => {
                return Err(ReplayError::SnapshotNotFound(format!(
                    "Failed to load snapshot {}: {}",
                    snapshot_id, e
                )))
            }
        };

        match replay_mode {
            ReplayMode::ValidationOnly => {
                // Just validate the snapshot structure without re-executing
                let execution_time = start_time.elapsed().as_millis() as f64;

                Ok(ReplayResult {
                    status: ReplayStatus::Success,
                    original_snapshot,
                    replay_output: None,
                    outputs_match: true, // We're not re-executing, so assume match
                    diff: None,
                    policy_violations: Vec::new(),
                    execution_time_ms: execution_time,
                })
            }
            ReplayMode::Strict | ReplayMode::Tolerant => {
                // Use the executor if available, otherwise fall back to simulation
                #[cfg(feature = "async")]
                {
                    self.execute_replay(&original_snapshot, replay_mode, start_time)
                        .await
                }
                #[cfg(not(feature = "async"))]
                {
                    self.simulate_replay(&original_snapshot, replay_mode, start_time)
                        .await
                }
            }
        }
    }

    /// Replay with policy validation
    pub async fn replay_with_policy(
        &self,
        snapshot_id: &str,
        policy: &ReplayPolicy,
        mode: Option<ReplayMode>,
    ) -> Result<ReplayResult, ReplayError> {
        let mut result = self.replay(snapshot_id, mode, None).await?;

        // Rules are checked against what the replay produced, so a policy can
        // only pass when something actually re-executed.
        let replayed = replayed_outputs(&result);
        let violations = self.check_policy(&result.original_snapshot, replayed.as_deref(), policy);
        result.policy_violations = violations;

        if !result.policy_violations.is_empty() {
            result.status = ReplayStatus::Failed;
        }

        Ok(result)
    }

    /// Compare two snapshots
    pub async fn diff(&self, original_id: &str, new_id: &str) -> Result<SnapshotDiff, ReplayError> {
        let original = self.storage.load_decision(original_id).await.map_err(|e| {
            ReplayError::SnapshotNotFound(format!("Original snapshot not found: {}", e))
        })?;

        let new =
            self.storage.load_decision(new_id).await.map_err(|e| {
                ReplayError::SnapshotNotFound(format!("New snapshot not found: {}", e))
            })?;

        Ok(self.calculate_diff(&original, &new))
    }

    /// Validate a snapshot against a policy (without re-executing)
    pub async fn validate(
        &self,
        snapshot_id: &str,
        policy: &ReplayPolicy,
    ) -> Result<Vec<PolicyViolation>, ReplayError> {
        let snapshot = self
            .storage
            .load_decision(snapshot_id)
            .await
            .map_err(|e| ReplayError::SnapshotNotFound(e.to_string()))?;

        self.validate_against_policy(&snapshot, policy).await
    }

    /// Batch replay multiple snapshots
    pub async fn replay_batch(
        &self,
        snapshot_ids: &[String],
        mode: Option<ReplayMode>,
        concurrency: usize,
    ) -> Vec<Result<ReplayResult, ReplayError>> {
        let semaphore = tokio::sync::Semaphore::new(concurrency);
        let replay_mode = mode.unwrap_or_else(|| self.default_mode.clone());

        let tasks: Vec<_> = snapshot_ids
            .iter()
            .map(|id| {
                let id = id.clone();
                let mode = replay_mode.clone();
                let semaphore = &semaphore;
                async move {
                    let _permit = semaphore.acquire().await.unwrap();
                    self.replay(&id, Some(mode), None).await
                }
            })
            .collect();

        futures::future::join_all(tasks).await
    }

    /// Get replay statistics for a set of snapshots
    pub async fn get_replay_stats(
        &self,
        snapshot_ids: &[String],
    ) -> Result<ReplayStats, ReplayError> {
        let results = self.replay_batch(snapshot_ids, None, 4).await;

        let mut stats = ReplayStats {
            total_replays: results.len(),
            ..Default::default()
        };

        for result in results {
            match result {
                Ok(replay_result) => {
                    stats.successful_replays += 1;
                    stats.total_execution_time_ms += replay_result.execution_time_ms;

                    if replay_result.outputs_match {
                        stats.exact_matches += 1;
                    } else {
                        stats.mismatches += 1;
                    }
                }
                Err(_) => {
                    stats.failed_replays += 1;
                }
            }
        }

        stats.average_execution_time_ms = if stats.successful_replays > 0 {
            stats.total_execution_time_ms / stats.successful_replays as f64
        } else {
            0.0
        };

        Ok(stats)
    }

    // Private helper methods

    #[cfg(feature = "async")]
    async fn execute_replay(
        &self,
        original: &DecisionSnapshot,
        mode: ReplayMode,
        start_time: std::time::Instant,
    ) -> Result<ReplayResult, ReplayError> {
        if let Some(ref executor) = self.executor {
            // Check model support
            if let Some(ref params) = original.model_parameters {
                if !executor.supports_model(&params.model_name) {
                    return Err(ReplayError::ExecutionError(format!(
                        "Executor '{}' does not support model '{}'",
                        executor.executor_name(),
                        params.model_name
                    )));
                }
            }

            // Execute with the configured default config (could be customized per replay)
            let config = ExecutionConfig::default();
            let exec_result = executor
                .execute(
                    &original.inputs,
                    original.model_parameters.as_ref(),
                    &original.context,
                    &config,
                )
                .await?;

            let execution_time = start_time.elapsed().as_millis() as f64;

            // Compare outputs based on mode
            let tolerance = match mode {
                ReplayMode::Strict => 1.0,         // Exact match
                ReplayMode::Tolerant => 0.8,       // 80% similarity
                ReplayMode::ValidationOnly => 0.0, // Don't care about outputs
            };

            let comparison =
                executor.compare_outputs(&original.outputs, &exec_result.outputs, tolerance);

            let replay_output = serde_json::to_value(&exec_result.outputs).ok();

            Ok(ReplayResult {
                status: if comparison.is_match {
                    ReplayStatus::Success
                } else {
                    ReplayStatus::Failed
                },
                original_snapshot: original.clone(),
                replay_output,
                outputs_match: comparison.is_match,
                diff: Some(SnapshotDiff {
                    inputs_changed: false,
                    outputs_changed: !comparison.is_match,
                    model_params_changed: false,
                    execution_time_delta_ms: execution_time
                        - original.execution_time_ms.unwrap_or(0.0),
                    changes: comparison
                        .field_comparisons
                        .iter()
                        .filter(|c| !c.is_match)
                        .map(|c| FieldChange {
                            field_path: format!("output.{}", c.field_name),
                            old_value: c.original_value.clone(),
                            new_value: c.replayed_value.clone(),
                            change_type: ChangeType::Modified,
                        })
                        .collect(),
                }),
                policy_violations: Vec::new(),
                execution_time_ms: execution_time,
            })
        } else {
            // Fall back to simulation if no executor is set
            self.simulate_replay(original, mode, start_time).await
        }
    }

    async fn simulate_replay(
        &self,
        original: &DecisionSnapshot,
        mode: ReplayMode,
        start_time: std::time::Instant,
    ) -> Result<ReplayResult, ReplayError> {
        // In a real implementation, this would:
        // 1. Recreate the execution environment
        // 2. Call the original function with the same inputs
        // 3. Compare outputs

        // For simulation, we'll create a "replay" that matches the original
        // Use the original execution time from the snapshot, not the replay elapsed time
        let execution_time = original
            .execution_time_ms
            .unwrap_or_else(|| start_time.elapsed().as_millis() as f64);

        // Simulate some processing time
        tokio::time::sleep(tokio::time::Duration::from_millis(1)).await;

        // Nothing re-executed, so there is no second answer to compare
        // against. Reporting a match here would turn "not checked" into
        // "verified", which is the one thing a replay must never do.
        let outputs_match = false;
        let _ = mode;

        Ok(ReplayResult {
            // Pending, not Failed: the decision did not disagree, it was never
            // re-run. Set an executor with `with_executor` to get a verdict.
            status: ReplayStatus::Pending,
            original_snapshot: original.clone(),
            replay_output: None,
            outputs_match,
            diff: None,
            policy_violations: Vec::new(),
            execution_time_ms: execution_time,
        })
    }

    async fn validate_against_policy(
        &self,
        snapshot: &DecisionSnapshot,
        policy: &ReplayPolicy,
    ) -> Result<Vec<PolicyViolation>, ReplayError> {
        // No replay ran, so there is nothing to compare the recorded values
        // against. Every rule reports as unverified rather than passing.
        Ok(self.check_policy(snapshot, None, policy))
    }

    /// Check each rule by comparing the recorded value against the replayed
    /// one. `replayed` is `None` when nothing re-executed, which makes every
    /// rule a violation: an unchecked rule must never read as a pass.
    fn check_policy(
        &self,
        original: &DecisionSnapshot,
        replayed: Option<&[Output]>,
        policy: &ReplayPolicy,
    ) -> Vec<PolicyViolation> {
        policy
            .rules
            .iter()
            .filter_map(|rule| self.check_rule(original, replayed, rule))
            .collect()
    }

    fn check_rule(
        &self,
        original: &DecisionSnapshot,
        replayed: Option<&[Output]>,
        rule: &ValidationRule,
    ) -> Option<PolicyViolation> {
        let recorded = self.extract_field_value(original, &rule.field)?;

        let Some(replayed) = replayed else {
            return Some(PolicyViolation {
                rule_name: rule.field.clone(),
                field: rule.field.clone(),
                expected: value_text(&recorded),
                actual: "not replayed".to_string(),
                message: format!(
                    "Rule on '{}' could not be checked: no executor is set, so nothing \
                     re-executed. Set one with ReplayEngine::with_executor.",
                    rule.field
                ),
            });
        };

        let Some(actual) = field_from_outputs(replayed, &rule.field) else {
            return Some(PolicyViolation {
                rule_name: rule.field.clone(),
                field: rule.field.clone(),
                expected: value_text(&recorded),
                actual: "absent".to_string(),
                message: format!("Replay produced no '{}' output", rule.field),
            });
        };

        let violation = |expected: String, message: String| {
            Some(PolicyViolation {
                rule_name: rule.field.clone(),
                field: rule.field.clone(),
                expected,
                actual: value_text(&actual),
                message,
            })
        };

        match rule.comparator {
            Comparator::ExactMatch => {
                if recorded == actual {
                    None
                } else {
                    violation(
                        value_text(&recorded),
                        format!("Field '{}' changed on replay", rule.field),
                    )
                }
            }
            Comparator::SemanticSimilarity => {
                let similarity = text_similarity(&value_text(&recorded), &value_text(&actual));
                if similarity >= rule.threshold {
                    None
                } else {
                    violation(
                        format!("similarity >= {}", rule.threshold),
                        format!(
                            "Field '{}' similarity {:.3} is below the {} threshold",
                            rule.field, similarity, rule.threshold
                        ),
                    )
                }
            }
            Comparator::MaxIncreasePercent
            | Comparator::MaxDecreasePercent
            | Comparator::WithinRange => {
                let (Some(before), Some(after)) = (recorded.as_f64(), actual.as_f64()) else {
                    return violation(
                        "a number".to_string(),
                        format!(
                            "Field '{}' is not numeric, so {:?} cannot be applied",
                            rule.field, rule.comparator
                        ),
                    );
                };
                let ok = match rule.comparator {
                    Comparator::MaxIncreasePercent => {
                        before == 0.0 || (after - before) / before * 100.0 <= rule.threshold
                    }
                    Comparator::MaxDecreasePercent => {
                        before == 0.0 || (before - after) / before * 100.0 <= rule.threshold
                    }
                    _ => (after - before).abs() <= rule.threshold,
                };
                if ok {
                    None
                } else {
                    violation(
                        format!("{:?} within {}", rule.comparator, rule.threshold),
                        format!("Field '{}' moved from {} to {}", rule.field, before, after),
                    )
                }
            }
        }
    }

    fn extract_field_value(
        &self,
        snapshot: &DecisionSnapshot,
        field_path: &str,
    ) -> Option<serde_json::Value> {
        match field_path {
            "function_name" => Some(serde_json::Value::String(snapshot.function_name.clone())),
            "execution_time_ms" => snapshot
                .execution_time_ms
                .and_then(serde_json::Number::from_f64)
                .map(serde_json::Value::Number),
            // Anything else names an output. A policy is written against the
            // fields a decision produced, so `with_exact_match("category")`
            // has to resolve to the output called "category".
            other => field_from_outputs(&snapshot.outputs, other),
        }
    }

    fn calculate_diff(&self, original: &DecisionSnapshot, new: &DecisionSnapshot) -> SnapshotDiff {
        let mut changes = Vec::new();

        // Compare function names
        if original.function_name != new.function_name {
            changes.push(FieldChange {
                field_path: "function_name".to_string(),
                old_value: serde_json::Value::String(original.function_name.clone()),
                new_value: serde_json::Value::String(new.function_name.clone()),
                change_type: ChangeType::Modified,
            });
        }

        // Compare inputs
        let inputs_changed = original.inputs != new.inputs;
        if inputs_changed {
            changes.push(FieldChange {
                field_path: "inputs".to_string(),
                old_value: serde_json::to_value(&original.inputs).unwrap(),
                new_value: serde_json::to_value(&new.inputs).unwrap(),
                change_type: ChangeType::Modified,
            });
        }

        // Compare outputs
        let outputs_changed = original.outputs != new.outputs;
        if outputs_changed {
            changes.push(FieldChange {
                field_path: "outputs".to_string(),
                old_value: serde_json::to_value(&original.outputs).unwrap(),
                new_value: serde_json::to_value(&new.outputs).unwrap(),
                change_type: ChangeType::Modified,
            });
        }

        // Compare model parameters
        let model_params_changed = original.model_parameters != new.model_parameters;
        if model_params_changed {
            changes.push(FieldChange {
                field_path: "model_parameters".to_string(),
                old_value: serde_json::to_value(&original.model_parameters).unwrap(),
                new_value: serde_json::to_value(&new.model_parameters).unwrap(),
                change_type: ChangeType::Modified,
            });
        }

        let execution_time_delta_ms = match (original.execution_time_ms, new.execution_time_ms) {
            (Some(old), Some(new)) => new - old,
            _ => 0.0,
        };

        SnapshotDiff {
            inputs_changed,
            outputs_changed,
            model_params_changed,
            execution_time_delta_ms,
            changes,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct ReplayStats {
    pub total_replays: usize,
    pub successful_replays: usize,
    pub failed_replays: usize,
    pub exact_matches: usize,
    pub mismatches: usize,
    pub total_execution_time_ms: f64,
    pub average_execution_time_ms: f64,
}

#[derive(Error, Debug, Clone, PartialEq)]
pub enum ReplayError {
    #[error("Snapshot not found: {0}")]
    SnapshotNotFound(String),
    #[error("Storage error: {0}")]
    StorageError(String),
    #[error("Execution error: {0}")]
    ExecutionError(String),
    #[error("Policy violations: {0:?}")]
    PolicyViolation(Vec<PolicyViolation>),
}

#[cfg(feature = "sqlite-storage")]
impl From<crate::storage::StorageError> for ReplayError {
    fn from(err: crate::storage::StorageError) -> Self {
        ReplayError::StorageError(err.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::*;
    use crate::storage::SqliteBackend;
    use serde_json::json;

    async fn create_test_decision() -> DecisionSnapshot {
        let input = Input::new("test_input", json!("hello"), "string");
        let output = Output::new("test_output", json!("world"), "string");
        let model_params = ModelParameters::new("gpt-4");

        DecisionSnapshot::new("test_function")
            .add_input(input)
            .add_output(output)
            .with_model_parameters(model_params)
            .with_execution_time(100.0)
    }

    #[tokio::test]
    async fn test_replay_engine_creation() {
        let storage = SqliteBackend::in_memory().unwrap();
        let engine = ReplayEngine::new(storage);
        assert!(matches!(engine.default_mode, ReplayMode::Tolerant));
    }

    #[tokio::test]
    async fn test_replay_validation_only() {
        let storage = SqliteBackend::in_memory().unwrap();
        let engine = ReplayEngine::new(storage);
        let decision = create_test_decision().await;

        // Save the decision first
        let decision_id = engine.storage.save_decision(&decision).await.unwrap();

        // Replay in validation-only mode
        let result = engine
            .replay(&decision_id, Some(ReplayMode::ValidationOnly), None)
            .await
            .unwrap();

        assert_eq!(result.status, ReplayStatus::Success);
        assert!(result.outputs_match);
        assert!(result.replay_output.is_none());
    }

    #[tokio::test]
    async fn test_replay_tolerant_mode() {
        let storage = SqliteBackend::in_memory().unwrap();
        let engine = ReplayEngine::new(storage);
        let decision = create_test_decision().await;

        // Save the decision first
        let decision_id = engine.storage.save_decision(&decision).await.unwrap();

        // Replay in tolerant mode. No executor is set, so this loads the
        // snapshot and reports that nothing was verified.
        let result = engine
            .replay(&decision_id, Some(ReplayMode::Tolerant), None)
            .await
            .unwrap();

        assert_eq!(result.status, ReplayStatus::Pending);
        assert!(!result.outputs_match);
        assert!(result.replay_output.is_none());
        assert_eq!(result.original_snapshot.function_name, "test_function");
    }

    #[tokio::test]
    async fn test_replay_with_policy() {
        let storage = SqliteBackend::in_memory().unwrap();
        let engine = ReplayEngine::new(storage);
        let decision = create_test_decision().await;

        // Save the decision first
        let decision_id = engine.storage.save_decision(&decision).await.unwrap();

        // Create a policy
        let policy = ReplayPolicy::new("test_policy")
            .with_exact_match("function_name")
            .with_similarity_threshold("output", 0.9);

        // Replay with policy. Without an executor there is nothing to compare
        // against, so every rule reports as unchecked rather than passing.
        let result = engine
            .replay_with_policy(&decision_id, &policy, None)
            .await
            .unwrap();

        assert_eq!(result.status, ReplayStatus::Failed);
        assert_eq!(result.policy_violations.len(), 2);
        assert!(result
            .policy_violations
            .iter()
            .all(|v| v.actual == "not replayed"));
    }

    #[tokio::test]
    async fn test_diff_calculation() {
        let storage = SqliteBackend::in_memory().unwrap();
        let engine = ReplayEngine::new(storage);

        let decision1 = create_test_decision().await;
        let mut decision2 = create_test_decision().await;
        decision2.function_name = "different_function".to_string();

        // Save both decisions
        let id1 = engine.storage.save_decision(&decision1).await.unwrap();
        let id2 = engine.storage.save_decision(&decision2).await.unwrap();

        // Calculate diff
        let diff = engine.diff(&id1, &id2).await.unwrap();

        assert!(!diff.changes.is_empty());
        assert!(diff.changes.iter().any(|c| c.field_path == "function_name"));
    }

    #[tokio::test]
    async fn test_batch_replay() {
        let storage = SqliteBackend::in_memory().unwrap();
        let engine = ReplayEngine::new(storage);

        let mut snapshot_ids = Vec::new();

        // Save multiple decisions
        for i in 0..3 {
            let mut decision = create_test_decision().await;
            decision.function_name = format!("test_function_{}", i);
            let id = engine.storage.save_decision(&decision).await.unwrap();
            snapshot_ids.push(id);
        }

        // Batch replay
        let results = engine.replay_batch(&snapshot_ids, None, 2).await;

        assert_eq!(results.len(), 3);
        assert!(results.iter().all(|r| r.is_ok()));
    }

    #[tokio::test]
    async fn test_replay_stats() {
        let storage = SqliteBackend::in_memory().unwrap();
        let engine = ReplayEngine::new(storage);

        let mut snapshot_ids = Vec::new();

        // Save multiple decisions
        for i in 0..5 {
            let mut decision = create_test_decision().await;
            decision.function_name = format!("test_function_{}", i);
            let id = engine.storage.save_decision(&decision).await.unwrap();
            snapshot_ids.push(id);
        }

        // Get stats
        let stats = engine.get_replay_stats(&snapshot_ids).await.unwrap();

        assert_eq!(stats.total_replays, 5);
        assert_eq!(stats.successful_replays, 5);
        assert_eq!(stats.failed_replays, 0);
        assert!(stats.average_execution_time_ms > 0.0);
    }

    #[tokio::test]
    async fn test_nonexistent_snapshot() {
        let storage = SqliteBackend::in_memory().unwrap();
        let engine = ReplayEngine::new(storage);

        let result = engine.replay("nonexistent-id", None, None).await;
        assert!(matches!(result, Err(ReplayError::SnapshotNotFound(_))));
    }

    #[tokio::test]
    async fn test_policy_validation() {
        let storage = SqliteBackend::in_memory().unwrap();
        let engine = ReplayEngine::new(storage);
        let decision = create_test_decision().await;

        // Save the decision first
        let decision_id = engine.storage.save_decision(&decision).await.unwrap();

        // Create a policy that should fail
        let policy = ReplayPolicy::new("strict_policy").with_similarity_threshold("output", 0.3); // Low threshold should trigger violation

        let violations = engine.validate(&decision_id, &policy).await.unwrap();
        assert!(!violations.is_empty());
    }
}

#[cfg(test)]
mod honest_replay_tests {
    use super::executor::{ExecutionConfig, ExecutionResult, ModelExecutor};
    use super::*;
    use crate::models::*;
    use crate::storage::SqliteBackend;
    use async_trait::async_trait;
    use serde_json::json;
    use std::collections::HashMap;
    use std::sync::Arc;

    /// Returns whatever it was told to, so a test can make a replay disagree.
    struct FixedExecutor {
        value: serde_json::Value,
    }

    #[async_trait]
    impl ModelExecutor for FixedExecutor {
        async fn execute(
            &self,
            _inputs: &[Input],
            _model_params: Option<&ModelParameters>,
            _context: &ExecutionContext,
            _config: &ExecutionConfig,
        ) -> Result<ExecutionResult, ReplayError> {
            Ok(ExecutionResult {
                outputs: vec![Output::new("answer", self.value.clone(), "string")],
                execution_time_ms: 1.0,
                metadata: HashMap::new(),
                raw_response: None,
            })
        }
        fn supports_model(&self, _model_name: &str) -> bool {
            true
        }
        fn executor_name(&self) -> &str {
            "fixed"
        }
    }

    async fn stored(answer: &str) -> (SqliteBackend, String) {
        let storage = SqliteBackend::in_memory().unwrap();
        let decision = DecisionSnapshot::new("classify_ticket")
            .add_input(Input::new("text", json!("reset my password"), "string"))
            .add_output(Output::new("answer", json!(answer), "string"))
            .with_model_parameters(ModelParameters::new("gpt-4"));
        let id = storage.save_decision(&decision).await.unwrap();
        (storage, id)
    }

    #[tokio::test]
    async fn a_changed_answer_is_reported_as_a_mismatch() {
        let (storage, id) = stored("account_access").await;
        let engine = ReplayEngine::new(storage).with_executor(Arc::new(FixedExecutor {
            value: json!("billing"),
        }));

        let result = engine
            .replay(&id, Some(ReplayMode::Strict), None)
            .await
            .unwrap();

        assert!(
            !result.outputs_match,
            "the executor answered 'billing' where 'account_access' was recorded"
        );
        assert_eq!(result.status, ReplayStatus::Failed);
    }

    #[tokio::test]
    async fn an_unchanged_answer_still_matches() {
        let (storage, id) = stored("account_access").await;
        let engine = ReplayEngine::new(storage).with_executor(Arc::new(FixedExecutor {
            value: json!("account_access"),
        }));

        let result = engine
            .replay(&id, Some(ReplayMode::Strict), None)
            .await
            .unwrap();

        assert!(result.outputs_match);
        assert_eq!(result.status, ReplayStatus::Success);
    }

    #[tokio::test]
    async fn without_an_executor_a_replay_does_not_claim_to_have_verified_anything() {
        let (storage, id) = stored("account_access").await;
        let engine = ReplayEngine::new(storage);

        let result = engine
            .replay(&id, Some(ReplayMode::Strict), None)
            .await
            .unwrap();

        assert_eq!(
            result.status,
            ReplayStatus::Pending,
            "nothing re-executed, so the replay is incomplete rather than successful"
        );
        assert!(
            !result.outputs_match,
            "outputs_match must not read True when no comparison happened"
        );
    }

    #[tokio::test]
    async fn an_exact_match_policy_rule_fails_when_the_field_changed() {
        let (storage, id) = stored("account_access").await;
        let engine = ReplayEngine::new(storage).with_executor(Arc::new(FixedExecutor {
            value: json!("billing"),
        }));
        let policy = ReplayPolicy::new("output-consistency").with_exact_match("answer");

        let result = engine
            .replay_with_policy(&id, &policy, Some(ReplayMode::Strict))
            .await
            .unwrap();

        assert_eq!(
            result.policy_violations.len(),
            1,
            "answer changed, so the exact-match rule must report a violation"
        );
        assert_eq!(result.policy_violations[0].field, "answer");
    }

    #[tokio::test]
    async fn an_exact_match_policy_rule_passes_when_the_field_held() {
        let (storage, id) = stored("account_access").await;
        let engine = ReplayEngine::new(storage).with_executor(Arc::new(FixedExecutor {
            value: json!("account_access"),
        }));
        let policy = ReplayPolicy::new("output-consistency").with_exact_match("answer");

        let result = engine
            .replay_with_policy(&id, &policy, Some(ReplayMode::Strict))
            .await
            .unwrap();

        assert!(result.policy_violations.is_empty());
    }

    #[tokio::test]
    async fn a_policy_cannot_pass_when_nothing_re_executed() {
        let (storage, id) = stored("account_access").await;
        let engine = ReplayEngine::new(storage);
        let policy = ReplayPolicy::new("output-consistency").with_exact_match("answer");

        let result = engine
            .replay_with_policy(&id, &policy, Some(ReplayMode::Strict))
            .await
            .unwrap();

        assert_eq!(
            result.policy_violations.len(),
            1,
            "an unverifiable rule must report, not silently pass"
        );
    }
}
