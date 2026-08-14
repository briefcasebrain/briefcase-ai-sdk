//! Python bindings for replay functionality

use crate::models::{json_value_to_python, python_to_json_value, PyDecisionSnapshot};
use crate::runtime::{PythonAsyncExt, PythonAsyncVecExt};
use crate::storage::PySqliteBackend;
use async_trait::async_trait;
use briefcase_core::models::{ExecutionContext, Input, ModelParameters, Output};
use briefcase_core::replay::{ExecutionConfig, ExecutionResult, ModelExecutor, ReplayError};
use briefcase_core::{
    storage::SqliteBackend, ReplayEngine, ReplayMode, ReplayPolicy, ReplayResult, ReplayStats,
    ReplayStatus,
};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::HashMap;

/// Re-executes a decision by calling back into Python.
///
/// The callable receives the recorded inputs as a dict of name to value and
/// returns the outputs the current build produces: either a dict of name to
/// value, or a bare value taken as the single output named `result`.
struct PyCallableExecutor {
    callable: PyObject,
}

#[async_trait]
impl ModelExecutor for PyCallableExecutor {
    async fn execute(
        &self,
        inputs: &[Input],
        _model_params: Option<&ModelParameters>,
        _context: &ExecutionContext,
        _config: &ExecutionConfig,
    ) -> Result<ExecutionResult, ReplayError> {
        let started = std::time::Instant::now();
        let outputs = Python::with_gil(|py| -> PyResult<Vec<Output>> {
            let recorded = PyDict::new(py);
            for input in inputs {
                recorded.set_item(&input.name, json_value_to_python(input.value.clone(), py)?)?;
            }
            let returned = self.callable.call1(py, (recorded,))?;

            // A dict names each output; anything else is one output.
            if let Ok(mapping) = returned.bind(py).downcast::<PyDict>() {
                let mut outs = Vec::with_capacity(mapping.len());
                for (key, value) in mapping.iter() {
                    outs.push(Output::new(
                        key.extract::<String>()?,
                        python_to_json_value(value.unbind(), py)?,
                        "string",
                    ));
                }
                Ok(outs)
            } else {
                Ok(vec![Output::new(
                    "result",
                    python_to_json_value(returned, py)?,
                    "string",
                )])
            }
        })
        .map_err(|e| ReplayError::ExecutionError(format!("Replay executor raised: {e}")))?;

        Ok(ExecutionResult {
            outputs,
            execution_time_ms: started.elapsed().as_secs_f64() * 1000.0,
            metadata: HashMap::new(),
            raw_response: None,
        })
    }

    fn supports_model(&self, _model_name: &str) -> bool {
        true
    }

    fn executor_name(&self) -> &str {
        "python-callable"
    }
}

/// Python wrapper for ReplayEngine
#[pyclass(name = "ReplayEngine")]
pub struct PyReplayEngine {
    pub inner: ReplayEngine<SqliteBackend>,
}

#[pymethods]
impl PyReplayEngine {
    /// Create new replay engine with storage backend
    #[new]
    fn new(storage: PyObject) -> PyResult<Self> {
        Python::with_gil(|py| {
            if let Ok(sqlite_backend) = storage.extract::<PyRef<PySqliteBackend>>(py) {
                Ok(Self {
                    inner: ReplayEngine::new(sqlite_backend.inner.clone()),
                })
            } else {
                Err(PyErr::new::<pyo3::exceptions::PyTypeError, _>(
                    "Storage must be SqliteBackend",
                ))
            }
        })
    }

    /// Re-run each replayed decision through `executor`, so `outputs_match`
    /// and any policy compare a real answer against the recorded one.
    ///
    /// `executor` is called with a dict of the recorded input names and values
    /// and returns the current outputs, either as a dict or as a single value.
    /// Without one, a replay loads the snapshot and reports status "pending".
    fn with_executor(&mut self, executor: PyObject) -> PyResult<()> {
        Python::with_gil(|py| {
            if !executor.bind(py).is_callable() {
                return Err(PyErr::new::<pyo3::exceptions::PyTypeError, _>(
                    "executor must be callable",
                ));
            }
            Ok(())
        })?;
        self.inner
            .set_executor(std::sync::Arc::new(PyCallableExecutor {
                callable: executor,
            }));
        Ok(())
    }

    /// Get default replay mode
    #[getter]
    fn default_mode(&self) -> String {
        match self.inner.default_mode() {
            ReplayMode::Strict => "strict".to_string(),
            ReplayMode::Tolerant => "tolerant".to_string(),
            ReplayMode::ValidationOnly => "validation_only".to_string(),
        }
    }

    /// Replay a snapshot
    fn replay(&self, snapshot_id: String, mode: Option<String>) -> PyResult<PyReplayResult> {
        let replay_mode = if let Some(mode_str) = mode {
            Some(match mode_str.as_str() {
                "strict" => ReplayMode::Strict,
                "tolerant" => ReplayMode::Tolerant,
                "validation_only" => ReplayMode::ValidationOnly,
                _ => {
                    return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                        "Invalid replay mode: {}",
                        mode_str
                    )))
                }
            })
        } else {
            None
        };

        let engine = self.inner.clone();

        // Use the global runtime instead of creating a new one
        engine
            .replay(&snapshot_id, replay_mode, None)
            .block_on_python()
            .map(|result| PyReplayResult { inner: result })
    }

    /// Replay with policy validation
    fn replay_with_policy(
        &self,
        snapshot_id: String,
        policy: PyRef<PyReplayPolicy>,
        mode: Option<String>,
    ) -> PyResult<PyReplayResult> {
        let replay_mode = if let Some(mode_str) = mode {
            Some(match mode_str.as_str() {
                "strict" => ReplayMode::Strict,
                "tolerant" => ReplayMode::Tolerant,
                "validation_only" => ReplayMode::ValidationOnly,
                _ => {
                    return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                        "Invalid replay mode: {}",
                        mode_str
                    )))
                }
            })
        } else {
            None
        };

        let engine = self.inner.clone();
        let policy_inner = policy.inner.clone();

        // Use the global runtime instead of creating a new one
        engine
            .replay_with_policy(&snapshot_id, &policy_inner, replay_mode)
            .block_on_python()
            .map(|result| PyReplayResult { inner: result })
    }

    /// Batch replay multiple snapshots
    #[pyo3(signature = (snapshot_ids, mode=None, max_concurrent=None))]
    fn replay_batch(
        &self,
        snapshot_ids: Vec<String>,
        mode: Option<String>,
        max_concurrent: Option<usize>,
    ) -> PyResult<PyObject> {
        let replay_mode = if let Some(mode_str) = mode {
            Some(match mode_str.as_str() {
                "strict" => ReplayMode::Strict,
                "tolerant" => ReplayMode::Tolerant,
                "validation_only" => ReplayMode::ValidationOnly,
                _ => {
                    return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                        "Invalid replay mode: {}",
                        mode_str
                    )))
                }
            })
        } else {
            None
        };

        let engine = self.inner.clone();

        // Use the global runtime and handle Python object creation outside async
        // On a partial failure the raise carries the results already computed,
        // so a caller does not lose the whole batch to one bad snapshot id.
        let results = PythonAsyncVecExt::block_on_python_partitioned(engine.replay_batch(
            &snapshot_ids,
            replay_mode,
            max_concurrent.unwrap_or(4),
        ))?
        .into_result(|err, successes| {
            Python::with_gil(|py| {
                let list = PyList::empty(py);
                for replay_result in successes {
                    list.append(Py::new(
                        py,
                        PyReplayResult {
                            inner: replay_result.clone(),
                        },
                    )?)?;
                }
                err.setattr("results", list)
            })
        })?;

        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for replay_result in results {
                let py_result = PyReplayResult {
                    inner: replay_result,
                };
                list.append(Py::new(py, py_result)?)?;
            }
            Ok(list.into())
        })
    }

    /// Validate snapshot against policy
    fn validate(&self, snapshot_id: String, policy: PyRef<PyReplayPolicy>) -> PyResult<PyObject> {
        let engine = self.inner.clone();
        let policy_inner = policy.inner.clone();

        // Use the global runtime and handle Python object creation outside async
        let violations = engine
            .validate(&snapshot_id, &policy_inner)
            .block_on_python()?;

        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for violation in violations {
                let violation_dict = PyDict::new(py);
                violation_dict.set_item("rule_name", &violation.rule_name)?;
                violation_dict.set_item("field", &violation.field)?;
                violation_dict.set_item("expected", &violation.expected)?;
                violation_dict.set_item("actual", &violation.actual)?;
                violation_dict.set_item("message", &violation.message)?;
                list.append(violation_dict)?;
            }
            Ok(list.into())
        })
    }

    /// Get replay statistics
    fn get_replay_stats(&self, snapshot_ids: Vec<String>) -> PyResult<PyReplayStats> {
        let engine = self.inner.clone();

        // Use the global runtime instead of creating a new one
        engine
            .get_replay_stats(&snapshot_ids)
            .block_on_python()
            .map(|stats| PyReplayStats { inner: stats })
    }

    /// String representation
    fn __repr__(&self) -> String {
        "ReplayEngine()".to_string()
    }
}

/// Python wrapper for ReplayPolicy
#[pyclass(name = "ReplayPolicy")]
pub struct PyReplayPolicy {
    pub inner: ReplayPolicy,
}

#[pymethods]
impl PyReplayPolicy {
    /// Create new replay policy
    #[new]
    fn new(name: String) -> Self {
        Self {
            inner: ReplayPolicy::new(name),
        }
    }

    /// Add exact match rule
    fn with_exact_match(mut slf: PyRefMut<Self>, field: String) -> PyRefMut<Self> {
        slf.inner = slf.inner.clone().with_exact_match(field);
        slf
    }

    /// Add similarity threshold rule
    fn with_similarity_threshold(
        mut slf: PyRefMut<Self>,
        field: String,
        threshold: f64,
    ) -> PyRefMut<Self> {
        slf.inner = slf
            .inner
            .clone()
            .with_similarity_threshold(field, threshold);
        slf
    }

    /// Get policy name
    #[getter]
    fn name(&self) -> String {
        self.inner.name.clone()
    }

    /// Get number of rules
    #[getter]
    fn rule_count(&self) -> usize {
        self.inner.rules.len()
    }

    /// String representation
    fn __repr__(&self) -> String {
        format!(
            "ReplayPolicy(name='{}', rules={})",
            self.inner.name,
            self.inner.rules.len()
        )
    }
}

/// Python wrapper for ReplayResult
#[pyclass(name = "ReplayResult")]
pub struct PyReplayResult {
    pub inner: ReplayResult,
}

#[pymethods]
impl PyReplayResult {
    /// Get replay status
    #[getter]
    fn status(&self) -> String {
        match self.inner.status {
            ReplayStatus::Success => "success".to_string(),
            ReplayStatus::Failed => "failed".to_string(),
            ReplayStatus::Partial => "partial".to_string(),
            ReplayStatus::Pending => "pending".to_string(),
            ReplayStatus::Running => "running".to_string(),
        }
    }

    /// Get original snapshot
    #[getter]
    fn original_snapshot(&self) -> PyDecisionSnapshot {
        PyDecisionSnapshot {
            inner: self.inner.original_snapshot.clone(),
        }
    }

    /// Check if outputs match
    #[getter]
    fn outputs_match(&self) -> bool {
        self.inner.outputs_match
    }

    /// Get execution time in milliseconds
    #[getter]
    fn execution_time_ms(&self) -> f64 {
        self.inner.execution_time_ms
    }

    /// Get policy violations
    #[getter]
    fn policy_violations(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for violation in &self.inner.policy_violations {
                let violation_dict = PyDict::new(py);
                violation_dict.set_item("rule_name", &violation.rule_name)?;
                violation_dict.set_item("field", &violation.field)?;
                violation_dict.set_item("expected", &violation.expected)?;
                violation_dict.set_item("actual", &violation.actual)?;
                violation_dict.set_item("message", &violation.message)?;
                list.append(violation_dict)?;
            }
            Ok(list.into())
        })
    }

    /// Get replay output
    #[getter]
    fn replay_output(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            if let Some(ref output) = self.inner.replay_output {
                crate::models::json_value_to_python(output.clone(), py)
            } else {
                Ok(py.None())
            }
        })
    }

    /// Convert to dictionary
    fn to_dict(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            dict.set_item("status", self.status())?;
            dict.set_item("outputs_match", self.inner.outputs_match)?;
            dict.set_item("execution_time_ms", self.inner.execution_time_ms)?;

            if let Some(ref output) = self.inner.replay_output {
                dict.set_item(
                    "replay_output",
                    crate::models::json_value_to_python(output.clone(), py)?,
                )?;
            }

            dict.set_item("policy_violations", self.policy_violations()?)?;
            Ok(dict.into())
        })
    }

    /// String representation
    fn __repr__(&self) -> String {
        format!(
            "ReplayResult(status='{}', outputs_match={})",
            self.status(),
            self.inner.outputs_match
        )
    }
}

/// Python wrapper for ReplayStats
#[pyclass(name = "ReplayStats")]
pub struct PyReplayStats {
    pub inner: ReplayStats,
}

#[pymethods]
impl PyReplayStats {
    /// Get total replays
    #[getter]
    fn total_replays(&self) -> usize {
        self.inner.total_replays
    }

    /// Get successful replays
    #[getter]
    fn successful_replays(&self) -> usize {
        self.inner.successful_replays
    }

    /// Get failed replays
    #[getter]
    fn failed_replays(&self) -> usize {
        self.inner.failed_replays
    }

    /// Get exact matches
    #[getter]
    fn exact_matches(&self) -> usize {
        self.inner.exact_matches
    }

    /// Get mismatches
    #[getter]
    fn mismatches(&self) -> usize {
        self.inner.mismatches
    }

    /// Get average execution time
    #[getter]
    fn average_execution_time_ms(&self) -> f64 {
        self.inner.average_execution_time_ms
    }

    /// Get total execution time
    #[getter]
    fn total_execution_time_ms(&self) -> f64 {
        self.inner.total_execution_time_ms
    }

    /// Get success rate
    #[getter]
    fn success_rate(&self) -> f64 {
        if self.inner.total_replays == 0 {
            0.0
        } else {
            self.inner.successful_replays as f64 / self.inner.total_replays as f64
        }
    }

    /// Convert to dictionary
    fn to_dict(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            dict.set_item("total_replays", self.inner.total_replays)?;
            dict.set_item("successful_replays", self.inner.successful_replays)?;
            dict.set_item("failed_replays", self.inner.failed_replays)?;
            dict.set_item("exact_matches", self.inner.exact_matches)?;
            dict.set_item("mismatches", self.inner.mismatches)?;
            dict.set_item(
                "average_execution_time_ms",
                self.inner.average_execution_time_ms,
            )?;
            dict.set_item(
                "total_execution_time_ms",
                self.inner.total_execution_time_ms,
            )?;
            dict.set_item("success_rate", self.success_rate())?;
            Ok(dict.into())
        })
    }

    /// String representation
    fn __repr__(&self) -> String {
        format!(
            "ReplayStats(total={}, success_rate={:.1}%)",
            self.inner.total_replays,
            self.success_rate() * 100.0
        )
    }
}
