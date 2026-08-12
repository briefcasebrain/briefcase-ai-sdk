//! Global async runtime management for Python bindings
//!
//! This module provides a singleton tokio runtime that is shared across all Python
//! bindings to avoid the catastrophic performance and resource issues of creating
//! a new runtime for every async call.

use briefcase_core::client::ClientError;
use briefcase_core::replay::ReplayError;
use briefcase_core::storage::StorageError;
use once_cell::sync::OnceCell;
use pyo3::exceptions::{
    PyConnectionError, PyKeyError, PyOSError, PyPermissionError, PyRuntimeError, PyValueError,
};
use pyo3::prelude::*;
use std::future::Future;
use tokio::runtime::Runtime;

/// Global tokio runtime instance shared across all Python bindings
static GLOBAL_RUNTIME: OnceCell<Runtime> = OnceCell::new();

/// Runtime configuration for the Python bindings
#[derive(Debug, Clone)]
pub struct RuntimeConfig {
    /// Number of worker threads for the runtime
    pub worker_threads: usize,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            worker_threads: 2, // Conservative default for Python bindings
        }
    }
}

/// Initialize the global runtime with the given configuration
pub fn init_runtime(config: RuntimeConfig) -> PyResult<()> {
    GLOBAL_RUNTIME
        .set({
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(config.worker_threads)
                .enable_io()
                .enable_time()
                .thread_name("briefcase-python")
                .build()
                .map_err(|e| {
                    PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(format!(
                        "Failed to initialize async runtime: {}",
                        e
                    ))
                })?
        })
        .map_err(|_| {
            PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("Global runtime already initialized")
        })?;

    Ok(())
}

/// Get the global runtime handle
pub fn get_runtime() -> PyResult<&'static Runtime> {
    GLOBAL_RUNTIME.get().ok_or_else(|| {
        PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(
            "Global runtime not initialized. Call briefcase.init() first",
        )
    })
}

/// Conversion from a bridged core error to a typed Python exception.
pub trait IntoPyErr {
    fn into_py_err(self) -> PyErr;
}

impl IntoPyErr for ClientError {
    fn into_py_err(self) -> PyErr {
        let msg = self.to_string();
        match self {
            ClientError::AuthFailed(_)
            | ClientError::PermissionDenied(_)
            | ClientError::Expired => PyPermissionError::new_err(msg),
            ClientError::ServerUnreachable(_) => PyConnectionError::new_err(msg),
            ClientError::InvalidArgument(_) => PyValueError::new_err(msg),
            ClientError::NoStorage => PyRuntimeError::new_err(msg),
            ClientError::Storage(e) => e.into_py_err(),
        }
    }
}

impl IntoPyErr for StorageError {
    fn into_py_err(self) -> PyErr {
        let msg = self.to_string();
        match self {
            StorageError::NotFound(_) => PyKeyError::new_err(msg),
            StorageError::ConnectionError(_) => PyConnectionError::new_err(msg),
            StorageError::PermissionDenied(_) => PyPermissionError::new_err(msg),
            StorageError::SerializationError(_) | StorageError::InvalidQuery(_) => {
                PyValueError::new_err(msg)
            }
            StorageError::IoError(_) => PyOSError::new_err(msg),
            StorageError::QuotaExceeded => PyRuntimeError::new_err(msg),
        }
    }
}

impl IntoPyErr for ReplayError {
    fn into_py_err(self) -> PyErr {
        let msg = self.to_string();
        match self {
            ReplayError::SnapshotNotFound(_) => PyKeyError::new_err(msg),
            ReplayError::StorageError(_)
            | ReplayError::ExecutionError(_)
            | ReplayError::PolicyViolation(_) => PyRuntimeError::new_err(msg),
        }
    }
}

impl IntoPyErr for String {
    fn into_py_err(self) -> PyErr {
        PyRuntimeError::new_err(self)
    }
}

impl IntoPyErr for &str {
    fn into_py_err(self) -> PyErr {
        PyRuntimeError::new_err(self.to_string())
    }
}

/// Execute a future on the global runtime with the GIL released, converting
/// errors to typed Python exceptions.
pub fn block_on_result<F, R, E>(future: F) -> PyResult<R>
where
    F: Future<Output = Result<R, E>> + Send,
    R: Send,
    E: IntoPyErr + Send,
{
    let runtime = get_runtime()?;
    Python::with_gil(|py| py.allow_threads(|| runtime.block_on(future)))
        .map_err(IntoPyErr::into_py_err)
}

/// Check if the global runtime is initialized
pub fn is_initialized() -> bool {
    GLOBAL_RUNTIME.get().is_some()
}

/// Shutdown the global runtime (for cleanup)
/// Note: Since we use OnceCell, we cannot actually shut down the runtime
/// It will be cleaned up when the process exits
pub fn shutdown_runtime() -> PyResult<()> {
    // Runtime will be cleaned up automatically when process exits
    // Cannot shutdown a OnceCell runtime without consuming it
    Ok(())
}

/// Helper trait for converting async operations to sync for Python
pub trait PythonAsyncExt<T> {
    fn block_on_python(self) -> PyResult<T>;
}

impl<F, T, E> PythonAsyncExt<T> for F
where
    F: Future<Output = Result<T, E>> + Send,
    T: Send,
    E: IntoPyErr + Send,
{
    fn block_on_python(self) -> PyResult<T> {
        block_on_result(self)
    }
}

/// What a batch produced: the items that succeeded, which indices failed, and
/// the typed exception to raise. Kept separate from the raise so a caller that
/// can convert `T` to Python attaches the successes to the exception instead of
/// discarding work the runtime already did.
pub struct BatchOutcome<T> {
    pub successes: Vec<T>,
    pub failed_indices: Vec<usize>,
    pub total: usize,
    pub error: Option<PyErr>,
}

impl<T> BatchOutcome<T> {
    /// Consume the outcome: on success return the items, otherwise raise the
    /// typed error after `enrich` has annotated it. `enrich` receives the
    /// exception and the successful items so it can attach them.
    pub fn into_result<F>(self, enrich: F) -> PyResult<Vec<T>>
    where
        F: FnOnce(&Bound<'_, pyo3::exceptions::PyBaseException>, &[T]) -> PyResult<()>,
    {
        let Some(err) = self.error else {
            return Ok(self.successes);
        };
        Python::with_gil(|py| {
            let value = err.value(py);
            let _ = value.setattr("failed_indices", self.failed_indices);
            let _ = value.setattr("succeeded", self.successes.len());
            let _ = value.setattr("total", self.total);
            let _ = enrich(value, &self.successes);
        });
        Err(err)
    }
}

/// Extension trait for vector results from batch operations
pub trait PythonAsyncVecExt<T> {
    /// Block on the future with the GIL released and return the successes and
    /// failures separately, without raising. Call `BatchOutcome::into_result`
    /// to raise the first failure's typed exception (the same type the
    /// single-item call raises), carrying every failed item in its message.
    fn block_on_python_partitioned(self) -> PyResult<BatchOutcome<T>>;
}

impl<F, T, E> PythonAsyncVecExt<T> for F
where
    F: Future<Output = Vec<Result<T, E>>> + Send,
    T: Send,
    E: IntoPyErr + std::fmt::Display + Send,
{
    fn block_on_python_partitioned(self) -> PyResult<BatchOutcome<T>> {
        let runtime = get_runtime()?;
        let results = Python::with_gil(|py| py.allow_threads(|| runtime.block_on(self)));

        let total = results.len();
        let mut successes = Vec::with_capacity(total);
        let mut first_error = None;
        let mut failures = Vec::new();
        let mut failed_indices = Vec::new();
        for (index, result) in results.into_iter().enumerate() {
            match result {
                Ok(item) => successes.push(item),
                Err(e) => {
                    failures.push(format!("item {}: {}", index, e));
                    failed_indices.push(index);
                    if first_error.is_none() {
                        first_error = Some(e);
                    }
                }
            }
        }

        let error = first_error.map(|e| {
            let typed = e.into_py_err();
            let summary = format!(
                "{} of {} batch items failed: {}",
                failures.len(),
                total,
                failures.join("; ")
            );
            Python::with_gil(|py| PyErr::from_type(typed.get_type(py), summary))
        });

        Ok(BatchOutcome {
            successes,
            failed_indices,
            total,
            error,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The runtime is a process global and `cargo test` runs these in parallel
    /// threads of one process, so only the first caller may initialize it.
    fn ensure_runtime() {
        pyo3::prepare_freethreaded_python();
        if !is_initialized() {
            let _ = init_runtime(RuntimeConfig::default());
        }
        assert!(is_initialized());
    }

    #[test]
    fn test_runtime_initialization() {
        ensure_runtime();

        // Test that we can get the runtime
        assert!(get_runtime().is_ok());

        // Test that we can execute a simple future
        let result = block_on_result(async { Ok::<i32, &str>(42) });
        assert_eq!(result.unwrap(), 42);
    }

    #[test]
    fn test_init_runtime_is_rejected_once_initialized() {
        ensure_runtime();
        assert!(init_runtime(RuntimeConfig::default()).is_err());
    }

    #[test]
    fn test_block_on_result() {
        ensure_runtime();

        // Test successful result
        let result = block_on_result(async { Ok::<i32, &str>(42) });
        assert_eq!(result.unwrap(), 42);

        // Test error result
        let result = block_on_result(async { Err::<i32, &str>("test error") });
        assert!(result.is_err());
    }

    #[test]
    fn test_async_trait() {
        ensure_runtime();

        let future = async { Ok::<i32, &str>(42) };
        let result = future.block_on_python();
        assert_eq!(result.unwrap(), 42);
    }

    #[test]
    fn test_batch_outcome_returns_successes_when_nothing_failed() {
        ensure_runtime();

        let future = async { vec![Ok::<i32, &str>(1), Ok(2), Ok(3)] };
        let outcome = future.block_on_python_partitioned().unwrap();
        assert!(outcome.error.is_none());
        assert_eq!(outcome.into_result(|_, _| Ok(())).unwrap(), vec![1, 2, 3]);
    }

    #[test]
    fn test_batch_outcome_keeps_successes_alongside_the_error() {
        ensure_runtime();

        let future = async { vec![Ok::<i32, &str>(1), Err("boom"), Ok(3)] };
        let outcome = future.block_on_python_partitioned().unwrap();

        assert_eq!(outcome.successes, vec![1, 3]);
        assert_eq!(outcome.failed_indices, vec![1]);
        assert_eq!(outcome.total, 3);

        // into_result hands those successes to the caller before raising, which
        // is how replay_batch attaches them to the exception.
        let mut seen = Vec::new();
        let err = outcome
            .into_result(|_, successes| {
                seen.extend_from_slice(successes);
                Ok(())
            })
            .unwrap_err();
        assert_eq!(seen, vec![1, 3]);
        Python::with_gil(|py| {
            let value = err.value(py);
            assert_eq!(
                value
                    .getattr("failed_indices")
                    .unwrap()
                    .extract::<Vec<usize>>()
                    .unwrap(),
                vec![1]
            );
            assert_eq!(
                value
                    .getattr("succeeded")
                    .unwrap()
                    .extract::<usize>()
                    .unwrap(),
                2
            );
            assert_eq!(
                value.getattr("total").unwrap().extract::<usize>().unwrap(),
                3
            );
        });
    }
}
