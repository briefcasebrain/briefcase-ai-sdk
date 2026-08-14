//! Python bindings for core data models

use briefcase_core::{
    AgentMetadata, DataRef, DecisionSnapshot, ExecutionContext, ExperimentMetadata,
    HardwareMetadata, Input, ModelParameters, Output, Scorecard, Snapshot, ToolCallMetadata,
};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList};

/// Python wrapper for Scorecard
#[pyclass(name = "Scorecard")]
pub struct PyScorecard {
    pub inner: Scorecard,
}

#[pymethods]
impl PyScorecard {
    #[new]
    fn new() -> Self {
        Self {
            inner: Scorecard::new(),
        }
    }

    fn add_score<'a>(
        mut slf: PyRefMut<'a, Self>,
        name: String,
        value: f64,
        weight: f64,
    ) -> PyRefMut<'a, Self> {
        slf.inner.add_score(name, value, weight);
        slf
    }

    #[getter]
    fn composite_score(&self) -> f64 {
        self.inner.composite_score()
    }
}

/// Python wrapper for ExperimentMetadata
#[pyclass(name = "ExperimentMetadata")]
pub struct PyExperimentMetadata {
    pub inner: ExperimentMetadata,
}

#[pymethods]
impl PyExperimentMetadata {
    #[new]
    fn new(experiment_id: String, run_index: u32, total_runs: u32) -> Self {
        Self {
            inner: ExperimentMetadata {
                experiment_id,
                run_index,
                total_runs,
                tags: std::collections::HashMap::new(),
            },
        }
    }
}

/// Python wrapper for ToolCallMetadata
#[pyclass(name = "ToolCallMetadata")]
pub struct PyToolCallMetadata {
    pub inner: ToolCallMetadata,
}

#[pymethods]
impl PyToolCallMetadata {
    #[new]
    fn new(tool_id: String, tool_name: String, arguments: String) -> Self {
        Self {
            inner: ToolCallMetadata {
                tool_id,
                tool_name,
                arguments,
                result: None,
            },
        }
    }

    fn with_result<'a>(mut slf: PyRefMut<'a, Self>, result: String) -> PyRefMut<'a, Self> {
        slf.inner.result = Some(result);
        slf
    }
}

/// Python wrapper for AgentMetadata
#[pyclass(name = "AgentMetadata")]
pub struct PyAgentMetadata {
    pub inner: AgentMetadata,
}

#[pymethods]
impl PyAgentMetadata {
    #[new]
    fn new(agent_id: String, role: String) -> Self {
        Self {
            inner: AgentMetadata::new(agent_id, role),
        }
    }

    fn with_handoff<'a>(mut slf: PyRefMut<'a, Self>, from: String) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_handoff(from);
        slf
    }

    fn add_tool_call<'a>(
        mut slf: PyRefMut<'a, Self>,
        tool_call: PyRef<'a, PyToolCallMetadata>,
    ) -> PyRefMut<'a, Self> {
        slf.inner.add_tool_call(tool_call.inner.clone());
        slf
    }
}

/// Python wrapper for DataRef
#[pyclass(name = "DataRef")]
pub struct PyDataRef {
    pub inner: DataRef,
}

#[pymethods]
impl PyDataRef {
    #[new]
    fn new(uri: String, fingerprint: String) -> Self {
        Self {
            inner: DataRef::new(uri, fingerprint),
        }
    }

    fn with_version<'a>(mut slf: PyRefMut<'a, Self>, version: String) -> PyRefMut<'a, Self> {
        slf.inner.version = Some(version);
        slf
    }
}

/// Python wrapper for Input
#[pyclass(name = "Input")]
pub struct PyInput {
    pub inner: Input,
}

#[pymethods]
impl PyInput {
    #[new]
    fn new(name: String, value: PyObject, data_type: String) -> PyResult<Self> {
        Python::with_gil(|py| {
            let json_value = python_to_json_value(value, py)?;
            Ok(Self {
                inner: Input::new(name, json_value, data_type),
            })
        })
    }

    #[getter]
    fn name(&self) -> &str {
        &self.inner.name
    }

    #[getter]
    fn value(&self) -> PyObject {
        Python::with_gil(|py| {
            json_value_to_python(self.inner.value.clone(), py).unwrap_or_else(|_| py.None())
        })
    }

    #[getter]
    fn data_type(&self) -> &str {
        &self.inner.data_type
    }

    fn to_dict(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            dict.set_item("name", &self.inner.name)?;
            dict.set_item("value", json_value_to_python(self.inner.value.clone(), py)?)?;
            dict.set_item("data_type", &self.inner.data_type)?;
            Ok(dict.into())
        })
    }
}

/// Python wrapper for Output
#[pyclass(name = "Output")]
pub struct PyOutput {
    pub inner: Output,
}

#[pymethods]
impl PyOutput {
    #[new]
    fn new(name: String, value: PyObject, data_type: String) -> PyResult<Self> {
        Python::with_gil(|py| {
            let json_value = python_to_json_value(value, py)?;
            Ok(Self {
                inner: Output::new(name, json_value, data_type),
            })
        })
    }

    fn with_confidence<'a>(mut slf: PyRefMut<'a, Self>, confidence: f64) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_confidence(confidence);
        slf
    }

    #[getter]
    fn name(&self) -> &str {
        &self.inner.name
    }

    #[getter]
    fn value(&self) -> PyObject {
        Python::with_gil(|py| {
            json_value_to_python(self.inner.value.clone(), py).unwrap_or_else(|_| py.None())
        })
    }

    #[getter]
    fn data_type(&self) -> &str {
        &self.inner.data_type
    }

    #[getter]
    fn confidence(&self) -> Option<f64> {
        self.inner.confidence
    }

    fn to_dict(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            dict.set_item("name", &self.inner.name)?;
            dict.set_item("value", json_value_to_python(self.inner.value.clone(), py)?)?;
            dict.set_item("data_type", &self.inner.data_type)?;
            dict.set_item("confidence", self.inner.confidence)?;
            Ok(dict.into())
        })
    }
}

/// Python wrapper for ModelParameters
#[pyclass(name = "ModelParameters")]
pub struct PyModelParameters {
    pub inner: ModelParameters,
}

#[pymethods]
impl PyModelParameters {
    #[new]
    fn new(model_name: String) -> Self {
        Self {
            inner: ModelParameters::new(model_name),
        }
    }

    fn with_provider<'a>(mut slf: PyRefMut<'a, Self>, provider: String) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_provider(provider);
        slf
    }

    fn with_parameter<'a>(
        mut slf: PyRefMut<'a, Self>,
        key: String,
        value: PyObject,
    ) -> PyResult<PyRefMut<'a, Self>> {
        let json_val = Python::with_gil(|py| python_to_json_value(value, py))?;
        slf.inner = slf.inner.clone().with_parameter(key, json_val);
        Ok(slf)
    }

    #[getter]
    fn model_name(&self) -> &str {
        &self.inner.model_name
    }

    #[getter]
    fn provider(&self) -> Option<&str> {
        self.inner.provider.as_deref()
    }

    #[getter]
    fn parameters(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            for (k, v) in &self.inner.parameters {
                dict.set_item(k, json_value_to_python(v.clone(), py)?)?;
            }
            Ok(dict.into())
        })
    }
}

/// Python wrapper for HardwareMetadata
#[pyclass(name = "HardwareMetadata")]
pub struct PyHardwareMetadata {
    pub inner: HardwareMetadata,
}

#[pymethods]
impl PyHardwareMetadata {
    #[new]
    fn new(device_type: String, device_name: String) -> Self {
        Self {
            inner: HardwareMetadata::new(device_type, device_name),
        }
    }

    fn with_provider<'a>(mut slf: PyRefMut<'a, Self>, provider: String) -> PyRefMut<'a, Self> {
        slf.inner.provider = provider;
        slf
    }

    fn with_vram<'a>(mut slf: PyRefMut<'a, Self>, vram_gb: f32) -> PyRefMut<'a, Self> {
        slf.inner.vram_gb = vram_gb;
        slf
    }
}

/// Python wrapper for ExecutionContext
#[pyclass(name = "ExecutionContext")]
pub struct PyExecutionContext {
    pub inner: ExecutionContext,
}

#[pymethods]
impl PyExecutionContext {
    #[new]
    fn new() -> Self {
        Self {
            inner: ExecutionContext::new(),
        }
    }

    fn with_runtime_version<'a>(
        mut slf: PyRefMut<'a, Self>,
        version: String,
    ) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_runtime_version(version);
        slf
    }

    fn with_dependency<'a>(
        mut slf: PyRefMut<'a, Self>,
        name: String,
        version: String,
    ) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_dependency(name, version);
        slf
    }

    fn with_random_seed<'a>(mut slf: PyRefMut<'a, Self>, seed: i64) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_random_seed(seed);
        slf
    }

    fn with_env_var<'a>(
        mut slf: PyRefMut<'a, Self>,
        key: String,
        value: String,
    ) -> PyRefMut<'a, Self> {
        slf.inner.environment_variables.insert(key, value);
        slf
    }

    #[getter]
    fn runtime_version(&self) -> Option<&str> {
        self.inner.runtime_version.as_deref()
    }

    #[getter]
    fn dependencies(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            for (k, v) in &self.inner.dependencies {
                dict.set_item(k, v)?;
            }
            Ok(dict.into())
        })
    }

    #[getter]
    fn random_seed(&self) -> Option<i64> {
        self.inner.random_seed
    }

    #[getter]
    fn environment_variables(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            for (k, v) in &self.inner.environment_variables {
                dict.set_item(k, v)?;
            }
            Ok(dict.into())
        })
    }
}

/// Python wrapper for DecisionSnapshot
#[pyclass(name = "DecisionSnapshot")]
pub struct PyDecisionSnapshot {
    pub inner: DecisionSnapshot,
}

#[pymethods]
impl PyDecisionSnapshot {
    #[new]
    fn new(function_name: String) -> Self {
        Self {
            inner: DecisionSnapshot::new(function_name),
        }
    }

    fn add_input<'a>(mut slf: PyRefMut<'a, Self>, input: PyRef<'a, PyInput>) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().add_input(input.inner.clone());
        slf
    }

    fn add_output<'a>(
        mut slf: PyRefMut<'a, Self>,
        output: PyRef<'a, PyOutput>,
    ) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().add_output(output.inner.clone());
        slf
    }

    fn with_model_parameters<'a>(
        mut slf: PyRefMut<'a, Self>,
        params: PyRef<'a, PyModelParameters>,
    ) -> PyRefMut<'a, Self> {
        slf.inner = slf
            .inner
            .clone()
            .with_model_parameters(params.inner.clone());
        slf
    }

    fn with_scorecard<'a>(
        mut slf: PyRefMut<'a, Self>,
        scorecard: PyRef<'a, PyScorecard>,
    ) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_scorecard(scorecard.inner.clone());
        slf
    }

    fn with_agent<'a>(
        mut slf: PyRefMut<'a, Self>,
        agent: PyRef<'a, PyAgentMetadata>,
    ) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_agent(agent.inner.clone());
        slf
    }

    fn with_hardware<'a>(
        mut slf: PyRefMut<'a, Self>,
        hardware: PyRef<'a, PyHardwareMetadata>,
    ) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_hardware(hardware.inner.clone());
        slf
    }

    fn add_tag<'a>(mut slf: PyRefMut<'a, Self>, key: String, value: String) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().add_tag(key, value);
        slf
    }

    fn with_module<'a>(mut slf: PyRefMut<'a, Self>, module: String) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_module(module);
        slf
    }

    fn with_execution_time<'a>(mut slf: PyRefMut<'a, Self>, ms: f64) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_execution_time(ms);
        slf
    }

    fn with_error<'a>(
        mut slf: PyRefMut<'a, Self>,
        error: String,
        error_type: Option<String>,
    ) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_error(error, error_type);
        slf
    }

    #[getter]
    fn function_name(&self) -> &str {
        &self.inner.function_name
    }

    #[getter]
    fn module_name(&self) -> Option<&str> {
        self.inner.module_name.as_deref()
    }

    #[getter]
    fn execution_time_ms(&self) -> Option<f64> {
        self.inner.execution_time_ms
    }

    #[getter]
    fn inputs(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for input in &self.inner.inputs {
                let py_input = PyInput {
                    inner: input.clone(),
                };
                list.append(Py::new(py, py_input)?)?;
            }
            Ok(list.into())
        })
    }

    #[getter]
    fn outputs(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for output in &self.inner.outputs {
                let py_output = PyOutput {
                    inner: output.clone(),
                };
                list.append(Py::new(py, py_output)?)?;
            }
            Ok(list.into())
        })
    }

    #[getter]
    fn tags(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            for (k, v) in &self.inner.tags {
                dict.set_item(k, v)?;
            }
            Ok(dict.into())
        })
    }

    /// Hash of the inputs and model name. Identifies the same question asked
    /// again; does not change when the answer changes.
    fn fingerprint(&self) -> String {
        self.inner.fingerprint()
    }

    /// Hash of everything decided: inputs, outputs, model parameters, tags,
    /// and any error. Excludes ids and timestamps so a holder of the record
    /// can recompute it.
    fn content_hash(&self) -> String {
        self.inner.content_hash()
    }
}

/// Python wrapper for Snapshot
#[pyclass(name = "Snapshot")]
pub struct PySnapshot {
    pub inner: Snapshot,
}

#[pymethods]
impl PySnapshot {
    #[new]
    fn new(snapshot_type: Option<String>) -> PyResult<Self> {
        let t = match snapshot_type.as_deref().unwrap_or("decision") {
            "decision" => briefcase_core::models::SnapshotType::Decision,
            "batch" => briefcase_core::models::SnapshotType::Batch,
            "session" => briefcase_core::models::SnapshotType::Session,
            other => {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                    "Invalid snapshot type: '{other}'. Use 'decision', 'batch', or 'session'."
                )))
            }
        };
        Ok(Self {
            inner: Snapshot::new(t),
        })
    }

    fn add_decision<'a>(
        mut slf: PyRefMut<'a, Self>,
        decision: PyRef<'a, PyDecisionSnapshot>,
    ) -> PyRefMut<'a, Self> {
        slf.inner.add_decision(decision.inner.clone());
        slf
    }

    #[getter]
    fn snapshot_type(&self) -> &str {
        match self.inner.snapshot_type {
            briefcase_core::models::SnapshotType::Decision => "decision",
            briefcase_core::models::SnapshotType::Batch => "batch",
            briefcase_core::models::SnapshotType::Session => "session",
        }
    }

    #[getter]
    fn decisions(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for decision in &self.inner.decisions {
                let py_decision = PyDecisionSnapshot {
                    inner: decision.clone(),
                };
                list.append(Py::new(py, py_decision)?)?;
            }
            Ok(list.into())
        })
    }
}

/// Python wrapper for SnapshotQuery
#[pyclass(name = "SnapshotQuery")]
pub struct PySnapshotQuery {
    pub inner: briefcase_core::storage::SnapshotQuery,
}

#[pymethods]
impl PySnapshotQuery {
    #[new]
    fn new() -> Self {
        Self {
            inner: briefcase_core::storage::SnapshotQuery::default(),
        }
    }

    fn with_function_name<'a>(mut slf: PyRefMut<'a, Self>, name: String) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_function_name(name);
        slf
    }

    fn with_module_name<'a>(mut slf: PyRefMut<'a, Self>, name: String) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_module_name(name);
        slf
    }

    fn with_limit<'a>(mut slf: PyRefMut<'a, Self>, limit: usize) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_limit(limit);
        slf
    }

    fn with_offset<'a>(mut slf: PyRefMut<'a, Self>, offset: usize) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_offset(offset);
        slf
    }

    fn with_tag<'a>(mut slf: PyRefMut<'a, Self>, key: String, value: String) -> PyRefMut<'a, Self> {
        slf.inner = slf.inner.clone().with_tag(key, value);
        slf
    }

    fn __repr__(&self) -> String {
        format!(
            "SnapshotQuery(function_name={:?})",
            self.inner.function_name
        )
    }
}

/// Name of a Python object's type, for error messages.
fn python_type_name(any: &Bound<'_, PyAny>) -> String {
    any.get_type()
        .name()
        .map(|n| n.to_string())
        .unwrap_or_else(|_| "<unknown>".to_string())
}

/// Helper function to convert Python objects to serde_json::Value.
/// Raises TypeError for values (or dict keys) that have no JSON equivalent.
pub fn python_to_json_value(obj: PyObject, py: Python<'_>) -> PyResult<serde_json::Value> {
    if obj.is_none(py) {
        return Ok(serde_json::Value::Null);
    }
    if let Ok(b) = obj.extract::<bool>(py) {
        return Ok(serde_json::Value::Bool(b));
    }
    if let Ok(s) = obj.extract::<String>(py) {
        return Ok(serde_json::Value::String(s));
    }
    if let Ok(i) = obj.extract::<i64>(py) {
        return Ok(serde_json::Value::from(i));
    }
    if let Ok(u) = obj.extract::<u64>(py) {
        return Ok(serde_json::Value::from(u));
    }
    if let Ok(f) = obj.extract::<f64>(py) {
        // serde_json maps NaN and +/-Infinity to null, which reads back as a
        // field that was never computed. Raise instead, like every other value
        // with no JSON equivalent.
        return serde_json::Number::from_f64(f)
            .map(serde_json::Value::Number)
            .ok_or_else(|| {
                pyo3::exceptions::PyTypeError::new_err(format!(
                    "cannot convert non-finite float {} to JSON",
                    f
                ))
            });
    }
    // Handle dict
    if let Ok(dict) = obj.downcast_bound::<pyo3::types::PyDict>(py) {
        let mut map = serde_json::Map::new();
        for (k, v) in dict.iter() {
            let key = k.extract::<String>().map_err(|_| {
                pyo3::exceptions::PyTypeError::new_err(format!(
                    "dict keys must be str to convert to JSON, got {}",
                    python_type_name(&k)
                ))
            })?;
            let val = python_to_json_value(v.unbind(), py)?;
            map.insert(key, val);
        }
        return Ok(serde_json::Value::Object(map));
    }
    // Handle list
    if let Ok(list) = obj.downcast_bound::<pyo3::types::PyList>(py) {
        let mut arr = Vec::new();
        for item in list.iter() {
            arr.push(python_to_json_value(item.unbind(), py)?);
        }
        return Ok(serde_json::Value::Array(arr));
    }
    // Handle tuple
    if let Ok(tuple) = obj.downcast_bound::<pyo3::types::PyTuple>(py) {
        let mut arr = Vec::new();
        for item in tuple.iter() {
            arr.push(python_to_json_value(item.unbind(), py)?);
        }
        return Ok(serde_json::Value::Array(arr));
    }
    // Types with exactly one obvious JSON spelling are converted rather than
    // rejected: date/time objects via isoformat(), UUIDs via str(). They are
    // ordinary contents of a captured payload, so raising on them would reject
    // otherwise valid records. Anything whose JSON form would be a guess (a
    // set has no defined order, an arbitrary object no spelling at all) still
    // raises instead of being silently nulled.
    let bound = obj.bind(py);
    if let Ok(iso) = bound.call_method0("isoformat") {
        if let Ok(s) = iso.extract::<String>() {
            return Ok(serde_json::Value::String(s));
        }
    }
    if bound.get_type().name().is_ok_and(|name| name == "UUID") {
        return Ok(serde_json::Value::String(bound.str()?.extract::<String>()?));
    }

    Err(pyo3::exceptions::PyTypeError::new_err(format!(
        "cannot convert Python object of type {} to JSON",
        python_type_name(bound)
    )))
}

/// Helper function to convert serde_json::Value to Python objects
pub fn json_value_to_python(value: serde_json::Value, py: Python<'_>) -> PyResult<PyObject> {
    match value {
        serde_json::Value::Null => Ok(py.None()),
        serde_json::Value::Bool(b) => {
            let pyb = PyBool::new(py, b);
            Ok(Bound::clone(&pyb).unbind().into())
        }
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i.into_pyobject(py).unwrap().unbind().into())
            } else if let Some(u) = n.as_u64() {
                Ok(u.into_pyobject(py).unwrap().unbind().into())
            } else if let Some(f) = n.as_f64() {
                Ok(f.into_pyobject(py).unwrap().unbind().into())
            } else {
                Ok(py.None())
            }
        }
        serde_json::Value::String(s) => Ok(s.into_pyobject(py).unwrap().unbind().into()),
        serde_json::Value::Array(vec) => {
            let list = PyList::empty(py);
            for v in vec {
                list.append(json_value_to_python(v, py)?)?;
            }
            Ok(list.into())
        }
        serde_json::Value::Object(map) => {
            let dict = PyDict::new(py);
            for (k, v) in map {
                dict.set_item(k, json_value_to_python(v, py)?)?;
            }
            Ok(dict.into())
        }
    }
}
