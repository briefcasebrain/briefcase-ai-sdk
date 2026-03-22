//! Synchronous replay engine implementation
//!
//! This module provides a synchronous variant of the replay engine
//! for use cases that don't require async functionality.

use super::{
    ChangeType, Comparator, ExecutionConfig, FieldChange, PolicyViolation, ReplayError, ReplayMode,
    ReplayPolicy, ReplayResult, ReplayStats, ReplayStatus, SnapshotDiff, SyncModelExecutor,
};
use crate::models::DecisionSnapshot;
use crate::storage::sync::SyncStorageBackend;
use std::sync::Arc;
use std::time::Instant;

/// Synchronous replay engine
pub struct SyncReplayEngine<S: SyncStorageBackend> {
    storage: S,
    default_mode: ReplayMode,
    executor: Option<Arc<dyn SyncModelExecutor>>,
}

impl<S: SyncStorageBackend> SyncReplayEngine<S> {
    /// Create a new synchronous replay engine
    pub fn new(storage: S) -> Self {
        Self {
            storage,
            default_mode: ReplayMode::Tolerant,
            executor: None,
        }
    }

    /// Create a replay engine with a specific default mode
    pub fn with_mode(storage: S, mode: ReplayMode) -> Self {
        Self {
            storage,
            default_mode: mode,
            executor: None,
        }
    }

    /// Set a model executor for the replay engine
    pub fn with_executor(mut self, executor: Arc<dyn SyncModelExecutor>) -> Self {
        self.executor = Some(executor);
        self
    }

    /// Get a reference to the current executor, if any
    pub fn executor(&self) -> Option<&dyn SyncModelExecutor> {
        self.executor.as_ref().map(|arc| arc.as_ref())
    }

    /// Get the default replay mode
    pub fn default_mode(&self) -> ReplayMode {
        self.default_mode.clone()
    }

    /// Replay a snapshot by ID
    pub fn replay(
        &self,
        snapshot_id: &str,
        mode: Option<ReplayMode>,
        _context_overrides: Option<serde_json::Value>,
    ) -> Result<ReplayResult, ReplayError> {
        let start_time = Instant::now();
        let replay_mode = mode.unwrap_or_else(|| self.default_mode());

        let original_snapshot = self
            .storage
            .load_decision(snapshot_id)
            .map_err(|e| ReplayError::StorageError(e.to_string()))?;

        match replay_mode {
            ReplayMode::ValidationOnly => {
                // Just validate without re-executing
                Ok(ReplayResult {
                    status: ReplayStatus::Success,
                    original_snapshot,
                    replay_output: None,
                    outputs_match: true, // Assume match for validation-only
                    diff: None,
                    policy_violations: Vec::new(),
                    execution_time_ms: start_time.elapsed().as_millis() as f64,
                })
            }
            ReplayMode::Strict | ReplayMode::Tolerant => {
                // Use the executor if available, otherwise fall back to simulation
                if self.executor.is_some() {
                    self.execute_replay(&original_snapshot, replay_mode, start_time)
                } else {
                    // For sync implementation, we'll simulate replay
                    let simulated_output = simulate_execution(&original_snapshot);
                    let outputs_match =
                        compare_outputs(&original_snapshot, &simulated_output, &replay_mode);

                    Ok(ReplayResult {
                        status: if outputs_match {
                            ReplayStatus::Success
                        } else {
                            ReplayStatus::Failed
                        },
                        original_snapshot,
                        replay_output: Some(simulated_output),
                        outputs_match,
                        diff: None, // TODO: Implement diff calculation
                        policy_violations: Vec::new(),
                        execution_time_ms: start_time.elapsed().as_millis() as f64,
                    })
                }
            }
        }
    }

    /// Replay with policy validation
    pub fn replay_with_policy(
        &self,
        snapshot_id: &str,
        policy: &ReplayPolicy,
        mode: Option<ReplayMode>,
    ) -> Result<ReplayResult, ReplayError> {
        let mut result = self.replay(snapshot_id, mode, None)?;

        // Validate against policy
        let violations = self.validate(snapshot_id, policy)?;
        result.policy_violations = violations;

        if !result.policy_violations.is_empty() {
            result.status = ReplayStatus::Failed;
        }

        Ok(result)
    }

    /// Validate a snapshot against a policy
    pub fn validate(
        &self,
        snapshot_id: &str,
        policy: &ReplayPolicy,
    ) -> Result<Vec<PolicyViolation>, ReplayError> {
        let snapshot = self
            .storage
            .load_decision(snapshot_id)
            .map_err(|e| ReplayError::StorageError(e.to_string()))?;

        let mut violations = Vec::new();

        // Apply policy rules
        for rule in &policy.rules {
            match rule.comparator {
                Comparator::ExactMatch => {
                    // For exact match, we'd need to compare with expected values
                    // This is a simplified implementation
                    if rule.field == "function_name" {
                        // Example validation logic
                        if snapshot.function_name.is_empty() {
                            violations.push(PolicyViolation {
                                rule_name: format!("exact_match_{}", rule.field),
                                field: rule.field.clone(),
                                expected: "non-empty function name".to_string(),
                                actual: "empty".to_string(),
                                message: "Function name cannot be empty".to_string(),
                            });
                        }
                    }
                }
                Comparator::SemanticSimilarity => {
                    // For similarity threshold, we'd need reference values
                    // This is a simplified implementation
                    if rule.field == "output" && snapshot.outputs.is_empty() {
                        violations.push(PolicyViolation {
                            rule_name: format!("similarity_{}", rule.field),
                            field: rule.field.clone(),
                            expected: "at least one output".to_string(),
                            actual: "no outputs".to_string(),
                            message: "At least one output is required".to_string(),
                        });
                    }
                }
                Comparator::MaxIncreasePercent => {
                    // Placeholder for cost/performance checks
                    // Would need historical data for comparison
                }
                Comparator::MaxDecreasePercent => {
                    // Placeholder for cost/performance checks
                    // Would need historical data for comparison
                }
                Comparator::WithinRange => {
                    // Placeholder for range checks
                    // Would need expected ranges for comparison
                }
            }
        }

        Ok(violations)
    }

    /// Execute replay with a model executor, if available
    fn execute_replay(
        &self,
        original: &DecisionSnapshot,
        mode: ReplayMode,
        start_time: Instant,
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

            // Execute with the configured default config
            let config = ExecutionConfig::default();
            let exec_result = executor.execute(
                &original.inputs,
                original.model_parameters.as_ref(),
                &original.context,
                &config,
            )?;

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
            self.simulate_replay(original, mode, start_time)
        }
    }

    /// Get replay statistics for a list of snapshots
    pub fn get_replay_stats(&self, snapshot_ids: &[String]) -> Result<ReplayStats, ReplayError> {
        let mut total_replays = 0;
        let mut successful_replays = 0;
        let mut failed_replays = 0;
        let mut exact_matches = 0;
        let mut mismatches = 0;
        let mut total_execution_time_ms = 0.0;

        for snapshot_id in snapshot_ids {
            match self.replay(snapshot_id, None, None) {
                Ok(result) => {
                    total_replays += 1;
                    total_execution_time_ms += result.execution_time_ms;

                    match result.status {
                        ReplayStatus::Success => {
                            successful_replays += 1;
                            if result.outputs_match {
                                exact_matches += 1;
                            } else {
                                mismatches += 1;
                            }
                        }
                        _ => {
                            failed_replays += 1;
                            mismatches += 1;
                        }
                    }
                }
                Err(_) => {
                    total_replays += 1;
                    failed_replays += 1;
                    mismatches += 1;
                }
            }
        }

        let average_execution_time_ms = if total_replays > 0 {
            total_execution_time_ms / total_replays as f64
        } else {
            0.0
        };

        Ok(ReplayStats {
            total_replays,
            successful_replays,
            failed_replays,
            exact_matches,
            mismatches,
            average_execution_time_ms,
            total_execution_time_ms,
        })
    }

    fn simulate_replay(
        &self,
        original: &DecisionSnapshot,
        mode: ReplayMode,
        start_time: Instant,
    ) -> Result<ReplayResult, ReplayError> {
        let simulated_output = simulate_execution(original);
        let outputs_match = compare_outputs(original, &simulated_output, &mode);

        Ok(ReplayResult {
            status: if outputs_match {
                ReplayStatus::Success
            } else {
                ReplayStatus::Failed
            },
            original_snapshot: original.clone(),
            replay_output: Some(simulated_output),
            outputs_match,
            diff: None,
            policy_violations: Vec::new(),
            execution_time_ms: start_time.elapsed().as_millis() as f64,
        })
    }
}

impl<S: SyncStorageBackend> Clone for SyncReplayEngine<S>
where
    S: Clone,
{
    fn clone(&self) -> Self {
        Self {
            storage: self.storage.clone(),
            default_mode: self.default_mode.clone(),
            executor: self.executor.clone(),
        }
    }
}

/// Simulate execution for demo purposes
/// In a real implementation, this would re-execute the decision
fn simulate_execution(decision: &DecisionSnapshot) -> serde_json::Value {
    // For now, just return the first output if available
    if let Some(output) = decision.outputs.first() {
        output.value.clone()
    } else {
        serde_json::Value::Null
    }
}

/// Compare outputs based on replay mode
fn compare_outputs(
    decision: &DecisionSnapshot,
    simulated_output: &serde_json::Value,
    mode: &ReplayMode,
) -> bool {
    if let Some(original_output) = decision.outputs.first() {
        match mode {
            ReplayMode::Strict => {
                // Exact match required
                original_output.value == *simulated_output
            }
            ReplayMode::Tolerant => {
                // Allow some differences (simplified implementation)
                if original_output.value == *simulated_output {
                    true
                } else {
                    // Could implement fuzzy matching here
                    false
                }
            }
            ReplayMode::ValidationOnly => {
                // Always true for validation-only mode
                true
            }
        }
    } else {
        simulated_output.is_null()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::*;
    use crate::storage::sync::MemoryStorageBackend;
    use serde_json::json;

    fn create_test_decision() -> DecisionSnapshot {
        let input = Input::new("test_input", json!("value"), "string");
        let output = Output::new("test_output", json!("result"), "string");
        let model_params = ModelParameters::new("gpt-4");

        DecisionSnapshot::new("test_function")
            .with_module("test_module")
            .add_input(input)
            .add_output(output)
            .with_model_parameters(model_params)
            .add_tag("env", "test")
    }

    #[test]
    fn test_sync_replay_validation_only() {
        let storage = MemoryStorageBackend::new();
        let engine = SyncReplayEngine::new(storage);

        let decision = create_test_decision();
        let decision_id = engine.storage.save_decision(&decision).unwrap();

        let result = engine
            .replay(&decision_id, Some(ReplayMode::ValidationOnly), None)
            .unwrap();

        assert_eq!(result.status, ReplayStatus::Success);
        assert!(result.outputs_match);
        assert!(result.replay_output.is_none());
    }

    #[test]
    fn test_sync_replay_tolerant_mode() {
        let storage = MemoryStorageBackend::new();
        let engine = SyncReplayEngine::new(storage);

        let decision = create_test_decision();
        let decision_id = engine.storage.save_decision(&decision).unwrap();

        let result = engine
            .replay(&decision_id, Some(ReplayMode::Tolerant), None)
            .unwrap();

        assert_eq!(result.status, ReplayStatus::Success);
        assert!(result.replay_output.is_some());
    }

    #[test]
    fn test_sync_replay_stats() {
        let storage = MemoryStorageBackend::new();
        let engine = SyncReplayEngine::new(storage);

        let decision1 = create_test_decision();
        let decision2 = create_test_decision();

        let id1 = engine.storage.save_decision(&decision1).unwrap();
        let id2 = engine.storage.save_decision(&decision2).unwrap();

        let stats = engine.get_replay_stats(&[id1, id2]).unwrap();

        assert_eq!(stats.total_replays, 2);
        assert!(stats.total_execution_time_ms >= 0.0);
        assert!(stats.average_execution_time_ms >= 0.0);
    }

    #[test]
    fn test_sync_replay_policy_validation() {
        let storage = MemoryStorageBackend::new();
        let engine = SyncReplayEngine::new(storage);

        let policy = ReplayPolicy::new("test_policy".to_string())
            .with_exact_match("function_name".to_string());

        let decision = create_test_decision();
        let decision_id = engine.storage.save_decision(&decision).unwrap();

        let violations = engine.validate(&decision_id, &policy).unwrap();

        // Should pass validation since function name is not empty
        assert!(violations.is_empty());
    }
}
