//! Python bindings for drift detection

use briefcase_core::{ConsensusConfidence, DriftCalculator, DriftMetrics, DriftStatus};
use pyo3::prelude::*;
use pyo3::types::PyList;

/// Python wrapper for DriftCalculator
#[pyclass(name = "DriftCalculator")]
pub struct PyDriftCalculator {
    pub inner: DriftCalculator,
}

#[pymethods]
impl PyDriftCalculator {
    /// Create new drift calculator
    #[new]
    fn new() -> Self {
        Self {
            inner: DriftCalculator::new(),
        }
    }

    /// Calculate drift from list of outputs
    fn calculate_drift(&self, outputs: Vec<String>) -> PyResult<PyDriftMetrics> {
        let metrics = self.inner.calculate_drift(&outputs);
        Ok(PyDriftMetrics { inner: metrics })
    }

    /// Set similarity threshold
    fn with_similarity_threshold(&mut self, threshold: f64) -> PyResult<()> {
        if !(0.0..=1.0).contains(&threshold) {
            return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                "Similarity threshold must be between 0.0 and 1.0",
            ));
        }
        self.inner = briefcase_core::DriftCalculator::with_threshold(threshold);
        Ok(())
    }

    /// Get similarity threshold
    #[getter]
    fn similarity_threshold(&self) -> f64 {
        self.inner.similarity_threshold()
    }

    /// String representation
    fn __repr__(&self) -> String {
        format!(
            "DriftCalculator(similarity_threshold={})",
            self.inner.similarity_threshold()
        )
    }
}

/// Python wrapper for DriftMetrics
#[pyclass(name = "DriftMetrics")]
pub struct PyDriftMetrics {
    pub inner: DriftMetrics,
}

#[pymethods]
impl PyDriftMetrics {
    /// Get consistency score
    #[getter]
    fn consistency_score(&self) -> f64 {
        self.inner.consistency_score
    }

    /// Get agreement rate
    #[getter]
    fn agreement_rate(&self) -> f64 {
        self.inner.agreement_rate
    }

    /// Get drift score
    #[getter]
    fn drift_score(&self) -> f64 {
        self.inner.drift_score
    }

    /// Get consensus output
    #[getter]
    fn consensus_output(&self) -> Option<String> {
        self.inner.consensus_output.clone()
    }

    /// Get outliers as list of indices
    #[getter]
    fn outliers(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for &index in &self.inner.outliers {
                list.append(index)?;
            }
            Ok(list.into())
        })
    }

    /// Calculate and get drift status
    fn get_status(&self, calculator: &PyDriftCalculator) -> String {
        let status = calculator.inner.get_status(&self.inner);
        match status {
            DriftStatus::Stable => "stable".to_string(),
            DriftStatus::Drifting => "drifting".to_string(),
            DriftStatus::Critical => "critical".to_string(),
        }
    }

    /// Get total samples (outliers count as proxy)
    #[getter]
    fn total_samples(&self) -> usize {
        self.inner.outliers.len()
    }

    /// Get consensus confidence
    #[getter]
    fn consensus_confidence(&self) -> String {
        match self.inner.consensus_confidence {
            ConsensusConfidence::High => "high".to_string(),
            ConsensusConfidence::Medium => "medium".to_string(),
            ConsensusConfidence::Low => "low".to_string(),
            ConsensusConfidence::None => "none".to_string(),
        }
    }

    /// Convert to dictionary
    fn to_dict(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = pyo3::types::PyDict::new(py);
            dict.set_item("consistency_score", self.inner.consistency_score)?;
            dict.set_item("agreement_rate", self.inner.agreement_rate)?;
            dict.set_item("drift_score", self.inner.drift_score)?;

            if let Some(ref consensus) = self.inner.consensus_output {
                dict.set_item("consensus_output", consensus)?;
            } else {
                dict.set_item("consensus_output", py.None())?;
            }

            let outliers_list = PyList::empty(py);
            for &index in &self.inner.outliers {
                outliers_list.append(index)?;
            }
            dict.set_item("outliers", outliers_list)?;

            dict.set_item("consensus_confidence", self.consensus_confidence())?;
            dict.set_item("total_samples", self.total_samples())?;

            Ok(dict.into())
        })
    }

    /// String representation
    fn __repr__(&self) -> String {
        format!(
            "DriftMetrics(consistency_score={:.3}, drift_score={:.3})",
            self.inner.consistency_score, self.inner.drift_score
        )
    }
}
