//! Model execution traits for replay
//!
//! These traits define how to re-execute a model during replay.
//! Implementors connect the replay engine to actual model inference.

use super::ReplayError;
use crate::models::{ExecutionContext, Input, ModelParameters, Output};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[cfg(feature = "async")]
use async_trait::async_trait;

/// Result of model execution during replay
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionResult {
    pub outputs: Vec<Output>,
    pub execution_time_ms: f64,
    pub metadata: HashMap<String, serde_json::Value>,
    pub raw_response: Option<serde_json::Value>,
}

/// Configuration for how to execute during replay
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionConfig {
    /// Maximum time to wait for execution (ms)
    pub timeout_ms: u64,
    /// Whether to use cached results if available
    pub use_cache: bool,
    /// Whether to record the execution for auditing
    pub record_execution: bool,
    /// Environment overrides for execution
    pub env_overrides: HashMap<String, String>,
    /// Custom parameters passed to executor
    pub custom_params: HashMap<String, serde_json::Value>,
}

impl Default for ExecutionConfig {
    fn default() -> Self {
        Self {
            timeout_ms: 30_000,
            use_cache: false,
            record_execution: true,
            env_overrides: HashMap::new(),
            custom_params: HashMap::new(),
        }
    }
}

impl ExecutionConfig {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_timeout(mut self, timeout_ms: u64) -> Self {
        self.timeout_ms = timeout_ms;
        self
    }

    pub fn with_cache(mut self, use_cache: bool) -> Self {
        self.use_cache = use_cache;
        self
    }

    pub fn with_recording(mut self, record: bool) -> Self {
        self.record_execution = record;
        self
    }
}

/// Comparison result between original and replayed outputs
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComparisonResult {
    pub is_match: bool,
    pub similarity_score: f64, // 0.0 - 1.0
    pub field_comparisons: Vec<FieldComparison>,
    pub summary: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FieldComparison {
    pub field_name: String,
    pub original_value: serde_json::Value,
    pub replayed_value: serde_json::Value,
    pub is_match: bool,
    pub similarity: f64,
}

/// Async trait for model re-execution during replay.
///
/// Implementors provide the bridge between the replay engine and actual
/// model inference (OpenAI, Anthropic, local models, etc.).
///
/// # Example
///
/// ```rust,ignore
/// struct OpenAIExecutor { client: OpenAIClient }
///
/// #[async_trait]
/// impl ModelExecutor for OpenAIExecutor {
///     async fn execute(
///         &self, inputs: &[Input], model_params: Option<&ModelParameters>,
///         context: &ExecutionContext, config: &ExecutionConfig,
///     ) -> Result<ExecutionResult, ReplayError> {
///         let response = self.client.chat(inputs, model_params).await?;
///         Ok(ExecutionResult { outputs: vec![...], ... })
///     }
/// }
/// ```
#[cfg(feature = "async")]
#[async_trait]
pub trait ModelExecutor: Send + Sync {
    /// Execute a model with the given inputs and parameters
    async fn execute(
        &self,
        inputs: &[Input],
        model_params: Option<&ModelParameters>,
        context: &ExecutionContext,
        config: &ExecutionConfig,
    ) -> Result<ExecutionResult, ReplayError>;

    /// Check if this executor supports the given model
    fn supports_model(&self, model_name: &str) -> bool;

    /// Get executor name for logging/auditing
    fn executor_name(&self) -> &str;

    /// Compare original outputs with replayed outputs
    fn compare_outputs(
        &self,
        original: &[Output],
        replayed: &[Output],
        tolerance: f64,
    ) -> ComparisonResult {
        default_compare_outputs(original, replayed, tolerance)
    }
}

/// Sync version of ModelExecutor for non-async contexts
pub trait SyncModelExecutor: Send + Sync {
    fn execute(
        &self,
        inputs: &[Input],
        model_params: Option<&ModelParameters>,
        context: &ExecutionContext,
        config: &ExecutionConfig,
    ) -> Result<ExecutionResult, ReplayError>;

    fn supports_model(&self, model_name: &str) -> bool;

    fn executor_name(&self) -> &str;

    fn compare_outputs(
        &self,
        original: &[Output],
        replayed: &[Output],
        tolerance: f64,
    ) -> ComparisonResult {
        default_compare_outputs(original, replayed, tolerance)
    }
}

/// No-op executor that returns empty outputs.
/// Used when no real model execution is available.
pub struct NoOpExecutor;

#[cfg(feature = "async")]
#[async_trait]
impl ModelExecutor for NoOpExecutor {
    async fn execute(
        &self,
        _inputs: &[Input],
        _model_params: Option<&ModelParameters>,
        _context: &ExecutionContext,
        _config: &ExecutionConfig,
    ) -> Result<ExecutionResult, ReplayError> {
        Ok(ExecutionResult {
            outputs: vec![],
            execution_time_ms: 0.0,
            metadata: HashMap::new(),
            raw_response: None,
        })
    }

    fn supports_model(&self, _model_name: &str) -> bool {
        true
    }

    fn executor_name(&self) -> &str {
        "noop"
    }
}

impl SyncModelExecutor for NoOpExecutor {
    fn execute(
        &self,
        _inputs: &[Input],
        _model_params: Option<&ModelParameters>,
        _context: &ExecutionContext,
        _config: &ExecutionConfig,
    ) -> Result<ExecutionResult, ReplayError> {
        Ok(ExecutionResult {
            outputs: vec![],
            execution_time_ms: 0.0,
            metadata: HashMap::new(),
            raw_response: None,
        })
    }

    fn supports_model(&self, _model_name: &str) -> bool {
        true
    }

    fn executor_name(&self) -> &str {
        "noop"
    }
}

/// Echo executor that returns the original snapshot outputs.
/// Useful for testing the replay pipeline without a real model.
pub struct EchoExecutor;

#[cfg(feature = "async")]
#[async_trait]
impl ModelExecutor for EchoExecutor {
    async fn execute(
        &self,
        _inputs: &[Input],
        _model_params: Option<&ModelParameters>,
        _context: &ExecutionContext,
        _config: &ExecutionConfig,
    ) -> Result<ExecutionResult, ReplayError> {
        // Echo executor returns empty outputs - should be overridden with snapshot outputs
        Ok(ExecutionResult {
            outputs: vec![],
            execution_time_ms: 0.0,
            metadata: HashMap::new(),
            raw_response: None,
        })
    }

    fn supports_model(&self, _model_name: &str) -> bool {
        true
    }

    fn executor_name(&self) -> &str {
        "echo"
    }
}

impl SyncModelExecutor for EchoExecutor {
    fn execute(
        &self,
        _inputs: &[Input],
        _model_params: Option<&ModelParameters>,
        _context: &ExecutionContext,
        _config: &ExecutionConfig,
    ) -> Result<ExecutionResult, ReplayError> {
        Ok(ExecutionResult {
            outputs: vec![],
            execution_time_ms: 0.0,
            metadata: HashMap::new(),
            raw_response: None,
        })
    }

    fn supports_model(&self, _model_name: &str) -> bool {
        true
    }

    fn executor_name(&self) -> &str {
        "echo"
    }
}

/// Default comparison implementation using string similarity
fn default_compare_outputs(
    original: &[Output],
    replayed: &[Output],
    tolerance: f64,
) -> ComparisonResult {
    if original.len() != replayed.len() {
        return ComparisonResult {
            is_match: false,
            similarity_score: 0.0,
            field_comparisons: vec![],
            summary: format!(
                "Output count mismatch: {} vs {}",
                original.len(),
                replayed.len()
            ),
        };
    }

    let mut comparisons = Vec::new();
    let mut total_similarity = 0.0;

    for (orig, replay) in original.iter().zip(replayed.iter()) {
        let is_exact = orig.value == replay.value;
        let similarity = if is_exact {
            1.0
        } else {
            // Compute string similarity for string values
            match (&orig.value, &replay.value) {
                (serde_json::Value::String(a), serde_json::Value::String(b)) => {
                    strsim::normalized_levenshtein(a, b)
                }
                (serde_json::Value::Number(a), serde_json::Value::Number(b)) => {
                    // Numeric comparison with tolerance
                    let a_f = a.as_f64().unwrap_or(0.0);
                    let b_f = b.as_f64().unwrap_or(0.0);
                    if a_f == 0.0 && b_f == 0.0 {
                        1.0
                    } else {
                        let max = a_f.abs().max(b_f.abs());
                        if max == 0.0 {
                            1.0
                        } else {
                            1.0 - ((a_f - b_f).abs() / max).min(1.0)
                        }
                    }
                }
                _ => {
                    if is_exact {
                        1.0
                    } else {
                        0.0
                    }
                }
            }
        };

        total_similarity += similarity;
        comparisons.push(FieldComparison {
            field_name: orig.name.clone(),
            original_value: orig.value.clone(),
            replayed_value: replay.value.clone(),
            is_match: similarity >= tolerance,
            similarity,
        });
    }

    let avg_similarity = if comparisons.is_empty() {
        1.0
    } else {
        total_similarity / comparisons.len() as f64
    };
    let all_match = comparisons.iter().all(|c| c.is_match);

    ComparisonResult {
        is_match: all_match,
        similarity_score: avg_similarity,
        field_comparisons: comparisons.clone(),
        summary: if all_match {
            format!(
                "All outputs match (similarity: {:.2}%)",
                avg_similarity * 100.0
            )
        } else {
            let mismatched: Vec<_> = comparisons
                .iter()
                .filter(|c| !c.is_match)
                .map(|c| c.field_name.as_str())
                .collect();
            format!("Mismatched fields: {}", mismatched.join(", "))
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_execution_config_default() {
        let config = ExecutionConfig::default();
        assert_eq!(config.timeout_ms, 30_000);
        assert!(!config.use_cache);
        assert!(config.record_execution);
    }

    #[test]
    fn test_execution_config_builder() {
        let config = ExecutionConfig::new()
            .with_timeout(60_000)
            .with_cache(true)
            .with_recording(false);

        assert_eq!(config.timeout_ms, 60_000);
        assert!(config.use_cache);
        assert!(!config.record_execution);
    }

    #[test]
    fn test_noop_executor_sync() {
        let executor = NoOpExecutor;
        assert!(SyncModelExecutor::supports_model(&executor, "any-model"));
        assert_eq!(SyncModelExecutor::executor_name(&executor), "noop");

        let result = SyncModelExecutor::execute(
            &executor,
            &[],
            None,
            &ExecutionContext::new(),
            &ExecutionConfig::default(),
        )
        .unwrap();
        assert!(result.outputs.is_empty());
    }

    #[test]
    fn test_echo_executor_sync() {
        let executor = EchoExecutor;
        assert!(SyncModelExecutor::supports_model(&executor, "any-model"));
        assert_eq!(SyncModelExecutor::executor_name(&executor), "echo");

        let result = SyncModelExecutor::execute(
            &executor,
            &[],
            None,
            &ExecutionContext::new(),
            &ExecutionConfig::default(),
        )
        .unwrap();
        assert!(result.outputs.is_empty());
    }

    #[test]
    fn test_default_compare_outputs_exact_match() {
        let original = vec![Output::new("output", json!("hello"), "string")];
        let replayed = vec![Output::new("output", json!("hello"), "string")];

        let result = default_compare_outputs(&original, &replayed, 0.9);

        assert!(result.is_match);
        assert!(result.similarity_score >= 0.99);
        assert_eq!(result.field_comparisons.len(), 1);
    }

    #[test]
    fn test_default_compare_outputs_mismatch() {
        let original = vec![Output::new("output", json!("hello"), "string")];
        let replayed = vec![Output::new("output", json!("world"), "string")];

        let result = default_compare_outputs(&original, &replayed, 0.95);

        assert!(!result.is_match);
        assert!(result.similarity_score < 1.0);
    }

    #[test]
    fn test_default_compare_outputs_count_mismatch() {
        let original = vec![
            Output::new("output1", json!("hello"), "string"),
            Output::new("output2", json!("world"), "string"),
        ];
        let replayed = vec![Output::new("output1", json!("hello"), "string")];

        let result = default_compare_outputs(&original, &replayed, 0.9);

        assert!(!result.is_match);
        assert_eq!(result.similarity_score, 0.0);
    }

    #[test]
    fn test_default_compare_outputs_numeric() {
        let original = vec![Output::new("number", json!(100), "number")];
        let replayed = vec![Output::new("number", json!(101), "number")];

        let result = default_compare_outputs(&original, &replayed, 0.95);

        assert!(result.is_match); // Should be > 0.95 similarity
        assert!(result.similarity_score > 0.99);
    }
}
