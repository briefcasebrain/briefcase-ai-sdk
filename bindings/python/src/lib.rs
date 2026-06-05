//! # Briefcase AI Python Bindings
//!
//! Python FFI bindings for the Briefcase AI core library, providing high-performance
//! AI observability, replay, and decision tracking for Python applications.
//!
//! ## Features
//!
//! - **AI Decision Tracking**: Capture inputs, outputs, and context for every AI decision
//! - **Deterministic Replay**: Reproduce AI decisions exactly with full context preservation
//! - **Cost Management**: Track and optimize AI model usage costs
//! - **Drift Detection**: Monitor model performance and behavior changes
//! - **Data Sanitization**: Built-in privacy controls for sensitive data
//! - **Multiple Storage Backends**: SQLite, cloud storage, and custom backends
//!
//! ## Installation
//!
//! ```bash
//! pip install briefcase-ai
//! ```
//!
//! ## Usage
//!
//! ```python
//! import briefcase
//!
//! briefcase.init()
//! decision = briefcase.DecisionSnapshot("ai_function")
//! decision.add_input(briefcase.Input("query", "Hello world", "string"))
//! decision.add_output(
//!     briefcase.Output("response", "Hello back!", "string").with_confidence(0.95)
//! )
//! storage = briefcase.storage.SqliteBackend.in_memory()
//! decision_id = storage.save_decision(decision)
//! ```

use pyo3::prelude::*;

// Re-export core types with Python wrappers
mod client;
mod cost;
mod drift;
mod models;
mod replay;
mod runtime;
mod sanitization;
mod storage;
mod tokens;

use cost::*;
use drift::*;
use models::*;
use replay::*;
use runtime::{init_runtime, RuntimeConfig};
use sanitization::*;
use storage::*;

/// Initialize the Briefcase AI library with default configuration
#[pyfunction]
fn init(py: Python) -> PyResult<()> {
    let config = RuntimeConfig::default();
    init_runtime(config)?;
    let atexit = py.import("atexit")?;
    let core_module = py.import("briefcase._native")?;
    let shutdown = core_module.getattr("_shutdown_runtime")?;
    atexit.call_method1("register", (shutdown,))?;
    Ok(())
}

/// Initialize with custom configuration
#[pyfunction]
#[pyo3(signature = (worker_threads = 2))]
fn init_with_config(worker_threads: usize) -> PyResult<()> {
    let config = RuntimeConfig { worker_threads };
    init_runtime(config)
}

/// Check if the runtime is initialized
#[pyfunction]
fn is_initialized() -> bool {
    runtime::is_initialized()
}

/// Internal function to shutdown the runtime (called by atexit)
#[pyfunction]
fn _shutdown_runtime() -> PyResult<()> {
    runtime::shutdown_runtime()
}

/// Briefcase AI Python module
#[pymodule]
fn _native(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Core data models
    m.add_class::<PyScorecard>()?;
    m.add_class::<PyExperimentMetadata>()?;
    m.add_class::<PyAgentMetadata>()?;
    m.add_class::<PyToolCallMetadata>()?;
    m.add_class::<PyHardwareMetadata>()?;
    m.add_class::<PyDataRef>()?;
    m.add_class::<PyInput>()?;
    m.add_class::<PyOutput>()?;
    m.add_class::<PyModelParameters>()?;
    m.add_class::<PyExecutionContext>()?;
    m.add_class::<PyDecisionSnapshot>()?;
    m.add_class::<PySnapshot>()?;

    // Storage backends
    m.add_class::<PySqliteBackend>()?;
    m.add_class::<PyBufferedBackend>()?;

    // Core functionality
    m.add_class::<PyDriftCalculator>()?;
    m.add_class::<PyDriftMetrics>()?;
    m.add_class::<PyCostCalculator>()?;
    m.add_class::<PyCostEstimate>()?;
    m.add_class::<PyBudgetStatus>()?;
    m.add_class::<PySanitizer>()?;
    m.add_class::<PySanitizationResult>()?;
    m.add_class::<PyJsonSanitizationResult>()?;
    m.add_class::<PyRedaction>()?;
    m.add_class::<PyReplayEngine>()?;
    m.add_class::<PyReplayPolicy>()?;
    m.add_class::<PyReplayResult>()?;
    m.add_class::<PyReplayStats>()?;

    // Query types
    m.add_class::<PySnapshotQuery>()?;

    // Authenticated client
    m.add_class::<client::PyBriefcaseClient>()?;
    m.add_class::<client::PyValidatedClient>()?;

    // Runtime initialization functions
    m.add_function(wrap_pyfunction!(init, m)?)?;
    m.add_function(wrap_pyfunction!(init_with_config, m)?)?;
    m.add_function(wrap_pyfunction!(is_initialized, m)?)?;
    m.add_function(wrap_pyfunction!(_shutdown_runtime, m)?)?;

    // Token estimation
    m.add_function(wrap_pyfunction!(tokens::count_tokens, m)?)?;
    m.add_function(wrap_pyfunction!(tokens::count_tokens_batch, m)?)?;

    // Add module metadata
    m.add("__version__", "3.3.1")?;
    m.add("__author__", "Briefcase AI")?;

    Ok(())
}
