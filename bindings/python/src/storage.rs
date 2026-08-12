//! Python bindings for storage backends

use crate::models::{PyDecisionSnapshot, PySnapshot, PySnapshotQuery};
use crate::runtime::{get_runtime, PythonAsyncExt};
use briefcase_core::storage::{buffered::BufferedBackend, SqliteBackend, StorageBackend};
use pyo3::prelude::*;
use pyo3::types::PyList;
use std::sync::Arc;

/// Python wrapper for BufferedBackend
#[pyclass(name = "BufferedBackend")]
pub struct PyBufferedBackend {
    pub inner: Arc<BufferedBackend>,
}

#[pymethods]
impl PyBufferedBackend {
    #[new]
    fn new(backend: PyObject, buffer_size: usize) -> PyResult<Self> {
        Python::with_gil(|py| {
            let inner: Arc<dyn StorageBackend> =
                if let Ok(sqlite) = backend.bind(py).extract::<PyRef<PySqliteBackend>>() {
                    Arc::new(sqlite.inner.clone())
                } else {
                    return Err(PyErr::new::<pyo3::exceptions::PyTypeError, _>(
                        "Invalid backend type. Supported: SqliteBackend",
                    ));
                };

            let runtime = get_runtime()?;
            let buffered = runtime.block_on(async { BufferedBackend::new(inner, buffer_size) });
            Ok(Self {
                inner: Arc::new(buffered),
            })
        })
    }

    /// Save a decision snapshot
    fn save_decision(&self, decision: PyRef<PyDecisionSnapshot>) -> PyResult<String> {
        let backend = self.inner.clone();
        let decision_inner = decision.inner.clone();
        backend.save_decision(&decision_inner).block_on_python()
    }
}

/// Python wrapper for SqliteBackend
#[pyclass(name = "SqliteBackend")]
pub struct PySqliteBackend {
    pub inner: SqliteBackend,
}

#[pymethods]
impl PySqliteBackend {
    #[new]
    fn new(path: Option<String>) -> PyResult<Self> {
        let backend = if let Some(path) = path {
            SqliteBackend::new(path)
        } else {
            SqliteBackend::in_memory()
        };

        match backend {
            Ok(backend) => Ok(Self { inner: backend }),
            Err(e) => Err(PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(format!(
                "Failed to create SQLite backend: {}",
                e
            ))),
        }
    }

    #[classmethod]
    fn in_memory(_cls: &Bound<'_, pyo3::types::PyType>) -> PyResult<Self> {
        SqliteBackend::in_memory()
            .map(|inner| Self { inner })
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    fn save(&self, snapshot: PyRef<PySnapshot>) -> PyResult<String> {
        self.inner.save(&snapshot.inner).block_on_python()
    }

    fn save_decision(&self, decision: PyRef<PyDecisionSnapshot>) -> PyResult<String> {
        self.inner.save_decision(&decision.inner).block_on_python()
    }

    fn load(&self, snapshot_id: String) -> PyResult<PySnapshot> {
        self.inner
            .load(&snapshot_id)
            .block_on_python()
            .map(|s| PySnapshot { inner: s })
    }

    fn load_decision(&self, decision_id: String) -> PyResult<PyDecisionSnapshot> {
        self.inner
            .load_decision(&decision_id)
            .block_on_python()
            .map(|d| PyDecisionSnapshot { inner: d })
    }

    fn query(&self, query: PyRef<PySnapshotQuery>) -> PyResult<PyObject> {
        let snapshots = self.inner.query(query.inner.clone()).block_on_python()?;
        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for s in snapshots {
                list.append(Py::new(py, PySnapshot { inner: s })?)?;
            }
            Ok(list.into())
        })
    }

    fn delete(&self, id: String) -> PyResult<bool> {
        self.inner.delete(&id).block_on_python()
    }
    fn health_check(&self) -> PyResult<bool> {
        self.inner.health_check().block_on_python()
    }
    fn __repr__(&self) -> String {
        "SqliteBackend()".into()
    }
}
