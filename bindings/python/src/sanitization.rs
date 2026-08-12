//! Python bindings for PII sanitization

use briefcase_core::{PiiType, Redaction, SanitizationJsonResult, SanitizationResult, Sanitizer};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

/// Python wrapper for Sanitizer
#[pyclass(name = "Sanitizer")]
pub struct PySanitizer {
    pub inner: Sanitizer,
}

#[pymethods]
impl PySanitizer {
    /// Create new sanitizer
    #[new]
    fn new() -> Self {
        Self {
            inner: Sanitizer::new(),
        }
    }

    /// Create disabled sanitizer
    #[staticmethod]
    fn disabled() -> Self {
        Self {
            inner: Sanitizer::disabled(),
        }
    }

    /// Sanitize text
    fn sanitize(&self, text: String) -> PySanitizationResult {
        let result = self.inner.sanitize(&text);
        PySanitizationResult { inner: result }
    }

    /// Sanitize JSON object
    fn sanitize_json(&self, data: PyObject) -> PyResult<PyJsonSanitizationResult> {
        Python::with_gil(|py| {
            let json_value = crate::models::python_to_json_value(data, py)?;
            let result = self.inner.sanitize_json(&json_value);
            Ok(PyJsonSanitizationResult { inner: result })
        })
    }

    /// Check if text contains PII
    fn contains_pii(&self, text: String) -> bool {
        let pii_matches = self.inner.contains_pii(&text);
        !pii_matches.is_empty()
    }

    /// Analyze PII in text
    fn analyze_pii(&self, text: String) -> PyResult<PyObject> {
        let analysis = self.inner.analyze(&text);

        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            dict.set_item("has_pii", analysis.has_pii)?;
            dict.set_item("total_matches", analysis.total_matches)?;
            dict.set_item("unique_types", analysis.unique_types)?;

            // Convert PII types to list
            let types_list = PyList::empty(py);
            for pii_type in analysis.type_counts.keys() {
                let type_str = match pii_type {
                    PiiType::Ssn => "ssn",
                    PiiType::CreditCard => "credit_card",
                    PiiType::Email => "email",
                    PiiType::Phone => "phone",
                    PiiType::ApiKey => "api_key",
                    PiiType::IpAddress => "ip_address",
                    PiiType::Custom(name) => name,
                };
                types_list.append(type_str)?;
            }
            dict.set_item("detected_types", types_list)?;

            Ok(dict.into())
        })
    }

    /// Add custom pattern
    fn add_pattern(&mut self, name: String, pattern: String) -> PyResult<()> {
        self.inner
            .add_pattern(&name, &pattern)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))
    }

    /// Remove a pattern by name; accepts the built-in names reported by
    /// `pii_type` ("email", "ssn", ...) as well as custom pattern names.
    /// A custom pattern shadowing a built-in name is removed first, so every
    /// pattern stays removable; a second call then removes the built-in.
    fn remove_pattern(&mut self, pattern_name: String) -> bool {
        if self
            .inner
            .remove_pattern(&PiiType::Custom(pattern_name.clone()))
        {
            return true;
        }
        let pii_type = match pattern_name.as_str() {
            "ssn" => PiiType::Ssn,
            "credit_card" => PiiType::CreditCard,
            "email" => PiiType::Email,
            "phone" => PiiType::Phone,
            "api_key" => PiiType::ApiKey,
            "ip_address" => PiiType::IpAddress,
            _ => return false,
        };
        self.inner.remove_pattern(&pii_type)
    }

    /// Enable or disable sanitization
    fn set_enabled(&mut self, enabled: bool) {
        self.inner.set_enabled(enabled);
    }

    /// Check if sanitizer is enabled
    #[getter]
    fn enabled(&self) -> bool {
        // We need to check the internal state, but the field is private
        // For now, we'll assume it's enabled if we can sanitize
        !self
            .inner
            .sanitize("test@example.com")
            .sanitized
            .contains("test@example.com")
    }

    /// String representation
    fn __repr__(&self) -> String {
        "Sanitizer()".to_string()
    }
}

/// Python wrapper for SanitizationResult
#[pyclass(name = "SanitizationResult")]
pub struct PySanitizationResult {
    pub inner: SanitizationResult,
}

/// Python wrapper for SanitizationJsonResult
#[pyclass(name = "SanitizationJsonResult")]
pub struct PyJsonSanitizationResult {
    pub inner: SanitizationJsonResult,
}

#[pymethods]
impl PySanitizationResult {
    /// Get sanitized text
    #[getter]
    fn sanitized(&self) -> String {
        self.inner.sanitized.clone()
    }

    /// Get redactions as list
    #[getter]
    fn redactions(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for redaction in &self.inner.redactions {
                let py_redaction = PyRedaction {
                    inner: redaction.clone(),
                };
                list.append(Py::new(py, py_redaction)?)?;
            }
            Ok(list.into())
        })
    }

    /// Get redaction count
    #[getter]
    fn redaction_count(&self) -> usize {
        self.inner.redactions.len()
    }

    /// Check if any redactions were made
    #[getter]
    fn has_redactions(&self) -> bool {
        !self.inner.redactions.is_empty()
    }

    /// Convert to dictionary
    fn to_dict(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);

            dict.set_item("sanitized", self.sanitized())?;
            dict.set_item("redaction_count", self.redaction_count())?;
            dict.set_item("has_redactions", self.has_redactions())?;

            // Convert redactions to list of dicts
            let redactions_list = PyList::empty(py);
            for redaction in &self.inner.redactions {
                let redaction_dict = PyDict::new(py);
                redaction_dict.set_item(
                    "pii_type",
                    format!("{:?}", redaction.pii_type).to_lowercase(),
                )?;
                redaction_dict.set_item("start_position", redaction.start_position)?;
                redaction_dict.set_item("end_position", redaction.end_position)?;
                redaction_dict.set_item("original_length", redaction.original_length)?;
                redactions_list.append(redaction_dict)?;
            }
            dict.set_item("redactions", redactions_list)?;

            Ok(dict.into())
        })
    }

    /// String representation
    fn __repr__(&self) -> String {
        format!(
            "SanitizationResult(redactions={})",
            self.inner.redactions.len()
        )
    }
}

#[pymethods]
impl PyJsonSanitizationResult {
    /// Get sanitized JSON value
    #[getter]
    fn sanitized(&self) -> PyObject {
        Python::with_gil(|py| {
            crate::models::json_value_to_python(self.inner.sanitized.clone(), py)
                .unwrap_or_else(|_| py.None())
        })
    }

    /// Get number of redactions
    #[getter]
    fn redaction_count(&self) -> usize {
        self.inner.redactions.len()
    }

    /// Convert to dictionary
    fn to_dict(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            dict.set_item("sanitized", self.sanitized())?;
            dict.set_item("redaction_count", self.redaction_count())?;
            Ok(dict.into())
        })
    }

    /// String representation
    fn __repr__(&self) -> String {
        format!(
            "SanitizationJsonResult(redactions={})",
            self.inner.redactions.len()
        )
    }
}

/// Python wrapper for Redaction
#[pyclass(name = "Redaction")]
pub struct PyRedaction {
    pub inner: Redaction,
}

#[pymethods]
impl PyRedaction {
    /// Get PII type
    #[getter]
    fn pii_type(&self) -> String {
        match &self.inner.pii_type {
            PiiType::Ssn => "ssn".to_string(),
            PiiType::CreditCard => "credit_card".to_string(),
            PiiType::Email => "email".to_string(),
            PiiType::Phone => "phone".to_string(),
            PiiType::ApiKey => "api_key".to_string(),
            PiiType::IpAddress => "ip_address".to_string(),
            PiiType::Custom(name) => name.clone(),
        }
    }

    /// Get start position
    #[getter]
    fn start_position(&self) -> usize {
        self.inner.start_position
    }

    /// Get end position
    #[getter]
    fn end_position(&self) -> usize {
        self.inner.end_position
    }

    /// Get original length
    #[getter]
    fn original_length(&self) -> usize {
        self.inner.original_length
    }

    /// Convert to dictionary
    fn to_dict(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            dict.set_item("pii_type", self.pii_type())?;
            dict.set_item("start_position", self.inner.start_position)?;
            dict.set_item("end_position", self.inner.end_position)?;
            dict.set_item("original_length", self.inner.original_length)?;
            Ok(dict.into())
        })
    }

    /// String representation
    fn __repr__(&self) -> String {
        format!(
            "Redaction(type='{}', position={}:{})",
            self.pii_type(),
            self.inner.start_position,
            self.inner.end_position
        )
    }
}
