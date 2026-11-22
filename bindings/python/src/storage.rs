//! Python bindings for storage backends

use crate::integrity::{json_to_py, load_signing_key, py_to_json_text};
use crate::models::{PyDecisionSnapshot, PySnapshot, PySnapshotQuery};
use crate::runtime::{get_runtime, IntoPyErr, PythonAsyncExt};
use briefcase_core::storage::chain::{ChainConfig, Checkpoint, VerifyOptions};
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

    /// Buffer a decision and return its id. The write lands when the batch
    /// fills or `flush()` is called, whichever comes first.
    fn save_decision(&self, decision: PyRef<PyDecisionSnapshot>) -> PyResult<String> {
        let backend = self.inner.clone();
        let decision_inner = decision.inner.clone();
        backend.save_decision(&decision_inner).block_on_python()
    }

    /// Write every buffered decision to the wrapped backend now.
    /// Returns how many decisions were written.
    fn flush(&self) -> PyResult<usize> {
        let backend = self.inner.clone();
        backend
            .flush()
            .block_on_python()
            .map(|r| r.snapshots_written)
    }

    /// Decisions accepted but not yet written.
    fn pending(&self) -> usize {
        self.inner.pending()
    }

    fn load(&self, snapshot_id: String) -> PyResult<PySnapshot> {
        self.inner
            .load(&snapshot_id)
            .block_on_python()
            .map(|s| PySnapshot { inner: s })
    }

    /// Load a decision, including one still sitting in the buffer.
    fn load_decision(&self, decision_id: String) -> PyResult<PyDecisionSnapshot> {
        self.inner
            .load_decision(&decision_id)
            .block_on_python()
            .map(|d| PyDecisionSnapshot { inner: d })
    }

    /// Query the wrapped backend. Buffered decisions are not visible until
    /// they are flushed.
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

    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// Flush on the way out, so `with BufferedBackend(...)` cannot lose a
    /// partial batch.
    fn __exit__(
        &self,
        _exc_type: PyObject,
        _exc_value: PyObject,
        _traceback: PyObject,
    ) -> PyResult<bool> {
        self.flush()?;
        Ok(false)
    }

    fn __repr__(&self) -> String {
        format!("BufferedBackend(pending={})", self.inner.pending())
    }
}

/// Python wrapper for SqliteBackend
#[pyclass(name = "SqliteBackend")]
pub struct PySqliteBackend {
    pub inner: SqliteBackend,
}

#[pymethods]
impl PySqliteBackend {
    /// Open (or create) the database at `path`, or an in-memory one when
    /// `path` is None.
    ///
    /// `append_only` is off by default: saving an id that is already stored
    /// replaces the row. With `append_only=True` such a save raises
    /// `FileExistsError` and leaves the stored row untouched, and `delete`
    /// raises `PermissionError`.
    ///
    /// `chained=True` turns on the tamper-evident mode (implies append-only):
    /// every save also appends a hash-chained entry, and every
    /// `checkpoint_every` entries (0 = only on `checkpoint()`) a checkpoint
    /// with a Merkle root is written, signed when `signing_key` is given (a
    /// key file path, or the file's bytes: private JWK, hex seed or 32-byte
    /// seed; the same keys `briefcase.integrity` uses).
    #[new]
    #[pyo3(signature = (path=None, append_only=false, chained=false, signing_key=None, checkpoint_every=100))]
    fn new(
        path: Option<String>,
        append_only: bool,
        chained: bool,
        signing_key: Option<&Bound<'_, PyAny>>,
        checkpoint_every: u64,
    ) -> PyResult<Self> {
        let backend = if let Some(path) = path {
            SqliteBackend::new(path)
        } else {
            SqliteBackend::in_memory()
        };

        let backend = match backend {
            Ok(backend) => backend.with_append_only(append_only),
            Err(e) => {
                return Err(PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(format!(
                    "Failed to create SQLite backend: {}",
                    e
                )))
            }
        };
        if !chained {
            if signing_key.is_some() {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "signing_key needs chained=True",
                ));
            }
            return Ok(Self { inner: backend });
        }
        let mut config = ChainConfig::new().with_checkpoint_every(checkpoint_every);
        if let Some(key) = signing_key {
            config = config.with_signing_key(load_signing_key(key)?);
        }
        let inner = backend.with_chain(config).map_err(|e| e.into_py_err())?;
        Ok(Self { inner })
    }

    /// Whether saves go through the hash chain.
    #[getter]
    fn chained(&self) -> bool {
        self.inner.is_chained()
    }

    /// Append one JSON record (a dict, or JSON text) under `record_id`.
    /// Returns the chain entry as a dict. Raises `FileExistsError` when the
    /// id is already in the chain and `ValueError` for values RFC 8785
    /// cannot represent exactly (NaN, integers beyond 2**53 a double cannot
    /// hold).
    fn append_record(
        &self,
        py: Python<'_>,
        record_id: String,
        record: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let value: serde_json::Value = serde_json::from_str(&py_to_json_text(record)?)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        let inner = self.inner.clone();
        let entry = py
            .allow_threads(move || inner.append_record(&record_id, &value))
            .map_err(|e| e.into_py_err())?;
        json_to_py(py, &entry)
    }

    /// Write a checkpoint over every entry since the previous one; returns
    /// the latest checkpoint as a dict (None for an empty chain).
    fn checkpoint(&self, py: Python<'_>) -> PyResult<PyObject> {
        let inner = self.inner.clone();
        let cp = py
            .allow_threads(move || inner.checkpoint())
            .map_err(|e| e.into_py_err())?;
        json_to_py(py, &cp)
    }

    /// The newest chain entry as a dict, or None.
    fn chain_head(&self, py: Python<'_>) -> PyResult<PyObject> {
        let head = self.inner.chain_head().map_err(|e| e.into_py_err())?;
        json_to_py(py, &head)
    }

    /// Every checkpoint, oldest first, as dicts.
    fn checkpoints(&self, py: Python<'_>) -> PyResult<PyObject> {
        let cps = self.inner.checkpoints().map_err(|e| e.into_py_err())?;
        json_to_py(py, &cps)
    }

    /// Merkle inclusion proof of entry `seq` (None until a checkpoint covers it).
    fn inclusion_proof(&self, py: Python<'_>, seq: u64) -> PyResult<PyObject> {
        let proof = self
            .inner
            .inclusion_proof(seq)
            .map_err(|e| e.into_py_err())?;
        json_to_py(py, &proof)
    }

    /// Walk the chain and checkpoints; returns the report as a dict with
    /// `intact`, counts, and `problems`. `against_checkpoint` (a checkpoint
    /// dict or JSON kept outside the database) is the only way to detect
    /// removal of the newest entries; `expected_key_id` pins the signer.
    #[pyo3(signature = (against_checkpoint=None, expected_key_id=None))]
    fn verify(
        &self,
        py: Python<'_>,
        against_checkpoint: Option<&Bound<'_, PyAny>>,
        expected_key_id: Option<String>,
    ) -> PyResult<PyObject> {
        let anchor = match against_checkpoint {
            None => None,
            Some(obj) => Some(
                serde_json::from_str::<Checkpoint>(&py_to_json_text(obj)?).map_err(|e| {
                    pyo3::exceptions::PyValueError::new_err(format!(
                        "against_checkpoint is not a checkpoint: {}",
                        e
                    ))
                })?,
            ),
        };
        let options = VerifyOptions {
            against_checkpoint: anchor,
            expected_key_id,
        };
        let inner = self.inner.clone();
        let report = py
            .allow_threads(move || inner.verify_chain(&options))
            .map_err(|e| e.into_py_err())?;
        json_to_py(py, &report)
    }

    #[classmethod]
    #[pyo3(signature = (append_only=false))]
    fn in_memory(_cls: &Bound<'_, pyo3::types::PyType>, append_only: bool) -> PyResult<Self> {
        SqliteBackend::in_memory()
            .map(|inner| Self {
                inner: inner.with_append_only(append_only),
            })
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    /// Whether this backend refuses to overwrite or delete stored snapshots.
    #[getter]
    fn append_only(&self) -> bool {
        self.inner.is_append_only()
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
        if self.inner.is_chained() {
            "SqliteBackend(chained=True)".into()
        } else if self.inner.is_append_only() {
            "SqliteBackend(append_only=True)".into()
        } else {
            "SqliteBackend()".into()
        }
    }
}
