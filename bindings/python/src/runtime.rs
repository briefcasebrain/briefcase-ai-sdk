//! Global async runtime management for Python bindings
//!
//! This module provides a singleton tokio runtime that is shared across all Python
//! bindings to avoid the catastrophic performance and resource issues of creating
//! a new runtime for every async call.

use once_cell::sync::OnceCell;
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

/// Execute a future using the global runtime with error conversion
pub fn block_on_result<F, R, E>(future: F) -> PyResult<R>
where
    F: Future<Output = Result<R, E>> + Send,
    R: Send,
    E: std::fmt::Display + Send,
{
    let runtime = get_runtime()?;
    runtime
        .block_on(future)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
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
    E: std::fmt::Display + Send,
{
    fn block_on_python(self) -> PyResult<T> {
        block_on_result(self)
    }
}

/// Extension trait for vector results from batch operations
pub trait PythonAsyncVecExt<T> {
    /// Block on the future and convert vector result to PyResult
    fn block_on_python(self) -> PyResult<Vec<T>>;
}

impl<F, T, E> PythonAsyncVecExt<T> for F
where
    F: Future<Output = Vec<Result<T, E>>> + Send,
    T: Send,
    E: std::fmt::Display + Send,
{
    fn block_on_python(self) -> PyResult<Vec<T>> {
        let runtime = get_runtime()?;
        let results = runtime.block_on(self);

        let mut successes = Vec::new();
        for result in results {
            match result {
                Ok(item) => successes.push(item),
                Err(_) => {
                    // For batch operations, we skip failed items
                    // Could log errors here if needed
                    continue;
                }
            }
        }
        Ok(successes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_runtime_initialization() {
        let config = RuntimeConfig::default();
        assert!(init_runtime(config).is_ok());
        assert!(is_initialized());

        // Test that we can get the runtime
        assert!(get_runtime().is_ok());

        // Test that we can execute a simple future
        let result = block_on_result(async { Ok::<i32, &str>(42) });
        assert_eq!(result.unwrap(), 42);
    }

    #[test]
    fn test_block_on_result() {
        let config = RuntimeConfig::default();
        init_runtime(config).unwrap();

        // Test successful result
        let result = block_on_result(async { Ok::<i32, &str>(42) });
        assert_eq!(result.unwrap(), 42);

        // Test error result
        let result = block_on_result(async { Err::<i32, &str>("test error") });
        assert!(result.is_err());
    }

    #[test]
    fn test_async_trait() {
        let config = RuntimeConfig::default();
        init_runtime(config).unwrap();

        let future = async { Ok::<i32, &str>(42) };
        let result = future.block_on_python();
        assert_eq!(result.unwrap(), 42);
    }
}
