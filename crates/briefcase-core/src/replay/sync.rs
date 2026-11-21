//! Synchronous replay engine implementation
//!
//! This module provides a synchronous variant of the replay engine
//! for use cases that don't require async functionality.

use super::{
    check_policy, check_result_policy, ChangeType, ExecutionConfig, FieldChange, PolicyViolation,
    ReplayError, ReplayMode, ReplayPolicy, ReplayResult, ReplayStats, ReplayStatus, SnapshotDiff,
    SyncModelExecutor,
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
                // Nothing re-executes, so nothing was compared: Pending with
                // no match claimed, as in the async engine.
                Ok(ReplayResult {
                    status: ReplayStatus::Pending,
                    original_snapshot,
                    replay_output: None,
                    outputs_match: false,
                    diff: None,
                    policy_violations: Vec::new(),
                    execution_time_ms: start_time.elapsed().as_millis() as f64,
                })
            }
            ReplayMode::Strict | ReplayMode::Tolerant => {
                self.execute_replay(&original_snapshot, replay_mode, start_time)
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

        // Rules are checked against what the replay produced, so a policy can
        // only pass when something actually re-executed.
        result.policy_violations = check_result_policy(&result, policy);

        if !result.policy_violations.is_empty() {
            result.status = ReplayStatus::Failed;
        }

        Ok(result)
    }

    /// Validate a snapshot against a policy (without re-executing).
    ///
    /// No replay runs, so there is nothing to compare the recorded values
    /// against: every rule reports as unverified rather than passing, exactly
    /// as `ReplayEngine::validate` does.
    pub fn validate(
        &self,
        snapshot_id: &str,
        policy: &ReplayPolicy,
    ) -> Result<Vec<PolicyViolation>, ReplayError> {
        let snapshot = self
            .storage
            .load_decision(snapshot_id)
            .map_err(|e| ReplayError::StorageError(e.to_string()))?;

        Ok(check_policy(&snapshot, None, policy))
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
            self.unverified_replay(original, start_time)
        }
    }

    /// Get replay statistics for a list of snapshots. Counts follow
    /// `ReplayEngine::get_replay_stats`: a replay that re-executed is
    /// "successful" whether it matched or not, one that re-executed nothing
    /// is "unverified", and one that returned an error is "failed".
    pub fn get_replay_stats(&self, snapshot_ids: &[String]) -> Result<ReplayStats, ReplayError> {
        let mut stats = ReplayStats {
            total_replays: snapshot_ids.len(),
            ..Default::default()
        };

        for snapshot_id in snapshot_ids {
            match self.replay(snapshot_id, None, None) {
                Ok(result) => {
                    stats.total_execution_time_ms += result.execution_time_ms;

                    if matches!(result.status, ReplayStatus::Pending | ReplayStatus::Running) {
                        stats.unverified_replays += 1;
                    } else {
                        stats.successful_replays += 1;
                        if result.outputs_match {
                            stats.exact_matches += 1;
                        } else {
                            stats.mismatches += 1;
                        }
                    }
                }
                Err(_) => stats.failed_replays += 1,
            }
        }

        let completed = stats.successful_replays + stats.unverified_replays;
        stats.average_execution_time_ms = if completed > 0 {
            stats.total_execution_time_ms / completed as f64
        } else {
            0.0
        };

        Ok(stats)
    }

    /// No executor is set, so nothing re-executes and there is no second
    /// answer to compare against. Reporting the recorded output as a match
    /// would turn "not checked" into "verified"; the result is Pending with
    /// no match claimed and no replay output, as in the async engine.
    fn unverified_replay(
        &self,
        original: &DecisionSnapshot,
        start_time: Instant,
    ) -> Result<ReplayResult, ReplayError> {
        Ok(ReplayResult {
            status: ReplayStatus::Pending,
            original_snapshot: original.clone(),
            replay_output: None,
            outputs_match: false,
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::*;
    use crate::replay::{Comparator, ValidationRule};
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

        // Nothing was compared, so no match may be claimed.
        assert_eq!(result.status, ReplayStatus::Pending);
        assert!(!result.outputs_match);
        assert!(result.replay_output.is_none());
    }

    /// Returns a fixed output; `execute` sleeps so the replay has a
    /// measurable duration.
    struct FixedExecutor(serde_json::Value);

    impl SyncModelExecutor for FixedExecutor {
        fn execute(
            &self,
            _inputs: &[Input],
            _model_params: Option<&ModelParameters>,
            _context: &ExecutionContext,
            _config: &ExecutionConfig,
        ) -> Result<super::super::ExecutionResult, ReplayError> {
            std::thread::sleep(std::time::Duration::from_millis(5));
            Ok(super::super::ExecutionResult {
                outputs: vec![Output::new("test_output", self.0.clone(), "string")],
                execution_time_ms: 5.0,
                metadata: Default::default(),
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

    fn engine_with(output: serde_json::Value) -> SyncReplayEngine<MemoryStorageBackend> {
        SyncReplayEngine::new(MemoryStorageBackend::new())
            .with_executor(Arc::new(FixedExecutor(output)))
    }

    #[test]
    fn test_sync_replay_without_executor_is_unverified() {
        for mode in [ReplayMode::Tolerant, ReplayMode::Strict] {
            let engine = SyncReplayEngine::new(MemoryStorageBackend::new());
            let decision_id = engine
                .storage
                .save_decision(&create_test_decision())
                .unwrap();

            let result = engine.replay(&decision_id, Some(mode), None).unwrap();

            // Nothing re-executed: the recorded output must not be echoed
            // back as a matching replay.
            assert_eq!(result.status, ReplayStatus::Pending);
            assert!(!result.outputs_match);
            assert!(result.replay_output.is_none());
        }
    }

    #[test]
    fn test_sync_replay_with_executor_gives_a_verdict() {
        let engine = engine_with(json!("result"));
        let id = engine
            .storage
            .save_decision(&create_test_decision())
            .unwrap();
        let result = engine.replay(&id, Some(ReplayMode::Strict), None).unwrap();
        assert_eq!(result.status, ReplayStatus::Success);
        assert!(result.outputs_match);

        let engine = engine_with(json!("something else entirely"));
        let id = engine
            .storage
            .save_decision(&create_test_decision())
            .unwrap();
        let result = engine.replay(&id, Some(ReplayMode::Strict), None).unwrap();
        assert_eq!(result.status, ReplayStatus::Failed);
        assert!(!result.outputs_match);
    }

    #[test]
    fn test_sync_replay_stats() {
        // No executor: both replays are unverified, neither a success nor a
        // failure nor a match.
        let engine = SyncReplayEngine::new(MemoryStorageBackend::new());
        let id1 = engine
            .storage
            .save_decision(&create_test_decision())
            .unwrap();
        let id2 = engine
            .storage
            .save_decision(&create_test_decision())
            .unwrap();

        let stats = engine
            .get_replay_stats(&[id1, id2, "missing".to_string()])
            .unwrap();

        assert_eq!(stats.total_replays, 3);
        assert_eq!(stats.unverified_replays, 2);
        assert_eq!(stats.successful_replays, 0);
        assert_eq!(stats.exact_matches, 0);
        assert_eq!(stats.mismatches, 0);
        assert_eq!(stats.failed_replays, 1);

        // With an executor the replays carry verdicts.
        let engine = engine_with(json!("result"));
        let id = engine
            .storage
            .save_decision(&create_test_decision())
            .unwrap();
        let stats = engine.get_replay_stats(&[id]).unwrap();
        assert_eq!(stats.successful_replays, 1);
        assert_eq!(stats.exact_matches, 1);
        assert_eq!(stats.unverified_replays, 0);
    }

    #[test]
    fn test_sync_validate_never_passes_an_unchecked_rule() {
        let engine = SyncReplayEngine::new(MemoryStorageBackend::new());
        let policy = ReplayPolicy::new("test_policy").with_exact_match("function_name");
        let id = engine
            .storage
            .save_decision(&create_test_decision())
            .unwrap();

        let violations = engine.validate(&id, &policy).unwrap();
        assert_eq!(violations.len(), 1);
        assert_eq!(violations[0].actual, "not replayed");
        assert_eq!(violations[0].expected, "test_function");
    }

    #[test]
    fn test_sync_policy_without_executor_fails() {
        let engine = SyncReplayEngine::new(MemoryStorageBackend::new());
        let policy = ReplayPolicy::new("p").with_exact_match("test_output");
        let id = engine
            .storage
            .save_decision(&create_test_decision())
            .unwrap();

        let result = engine.replay_with_policy(&id, &policy, None).unwrap();
        assert_eq!(result.status, ReplayStatus::Failed);
        assert_eq!(result.policy_violations[0].actual, "not replayed");
    }

    #[test]
    fn test_sync_policy_resolves_snapshot_level_fields() {
        let engine = engine_with(json!("result"));
        let decision = create_test_decision().with_execution_time(1_000.0);
        let id = engine.storage.save_decision(&decision).unwrap();

        // function_name and a generous execution-time bound both resolve on
        // the replayed side and pass, alongside an output rule.
        let policy = ReplayPolicy::new("p")
            .with_exact_match("function_name")
            .with_exact_match("test_output")
            .add_rule(ValidationRule {
                field: "execution_time_ms".to_string(),
                comparator: Comparator::MaxIncreasePercent,
                threshold: 10.0,
            });
        let result = engine.replay_with_policy(&id, &policy, None).unwrap();
        assert!(
            result.policy_violations.is_empty(),
            "{:?}",
            result.policy_violations
        );
        assert_eq!(result.status, ReplayStatus::Success);

        // The replay took ~5 ms against a recorded 1000 ms: a rule that
        // forbids any decrease is a real, numeric violation, not "absent".
        let policy = ReplayPolicy::new("p").add_rule(ValidationRule {
            field: "execution_time_ms".to_string(),
            comparator: Comparator::MaxDecreasePercent,
            threshold: 1.0,
        });
        let result = engine.replay_with_policy(&id, &policy, None).unwrap();
        assert_eq!(result.policy_violations.len(), 1);
        assert_ne!(result.policy_violations[0].actual, "absent");
        assert!(result.policy_violations[0].message.contains("moved from"));
    }
}
