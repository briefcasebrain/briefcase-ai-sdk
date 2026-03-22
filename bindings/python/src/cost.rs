//! Python bindings for cost calculation

use briefcase_core::{BudgetAlert, BudgetStatus, CostCalculator, CostEstimate};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

/// Python wrapper for CostCalculator
#[pyclass(name = "CostCalculator")]
pub struct PyCostCalculator {
    pub inner: CostCalculator,
}

#[pymethods]
impl PyCostCalculator {
    /// Create new cost calculator
    #[new]
    fn new() -> Self {
        Self {
            inner: CostCalculator::new(),
        }
    }

    /// Estimate cost for a model
    fn estimate_cost(
        &self,
        model_name: String,
        input_tokens: u32,
        output_tokens: u32,
    ) -> PyResult<PyCostEstimate> {
        match self
            .inner
            .estimate_cost(&model_name, input_tokens as usize, output_tokens as usize)
        {
            Ok(estimate) => Ok(PyCostEstimate { inner: estimate }),
            Err(e) => Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                e.to_string(),
            )),
        }
    }

    /// Estimate cost from text
    fn estimate_cost_from_text(
        &self,
        model_name: String,
        input_text: String,
        estimated_output_tokens: u32,
    ) -> PyResult<PyCostEstimate> {
        match self.inner.estimate_cost_from_text(
            &model_name,
            &input_text,
            estimated_output_tokens as usize,
        ) {
            Ok(estimate) => Ok(PyCostEstimate { inner: estimate }),
            Err(e) => Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                e.to_string(),
            )),
        }
    }

    /// Project monthly cost
    fn project_monthly_cost(
        &self,
        model_name: String,
        daily_input_tokens: u32,
        daily_output_tokens: u32,
        days_per_month: f64,
    ) -> PyResult<f64> {
        match self.inner.project_monthly_cost(
            &model_name,
            daily_input_tokens as usize,
            daily_output_tokens as usize,
            days_per_month,
        ) {
            Ok(projection) => Ok(projection.monthly_cost),
            Err(e) => Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                e.to_string(),
            )),
        }
    }

    /// Compare two models for cost efficiency
    fn compare_models(
        &self,
        model_a: String,
        model_b: String,
        input_tokens: u32,
        output_tokens: u32,
    ) -> PyResult<PyObject> {
        match self.inner.compare_models(
            &model_a,
            &model_b,
            input_tokens as usize,
            output_tokens as usize,
        ) {
            Ok(comparison) => Python::with_gil(|py| {
                let dict = pyo3::types::PyDict::new(py);
                dict.set_item("cheaper_model", &comparison.cheaper_model)?;
                dict.set_item("savings", comparison.savings)?;
                dict.set_item("percent_difference", comparison.percent_difference)?;
                Ok(dict.into())
            }),
            Err(e) => Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                e.to_string(),
            )),
        }
    }

    /// Get cheapest model with minimum context window
    fn get_cheapest_model(&self, min_context_window: u32) -> Option<String> {
        self.inner
            .get_cheapest_model(min_context_window as usize)
            .map(|pricing| pricing.model_name.clone())
    }

    /// Get models under cost threshold per 1k tokens
    fn get_models_under_cost(&self, max_cost_per_1k: f64) -> PyResult<PyObject> {
        let models = self.inner.get_models_under_cost(max_cost_per_1k);
        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for model in models {
                list.append(&model.model_name)?;
            }
            Ok(list.into())
        })
    }

    /// Get models by provider
    fn get_models_by_provider(&self, provider: String) -> PyResult<PyObject> {
        let models = self.inner.get_models_by_provider(&provider);
        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for model in models {
                list.append(&model.model_name)?;
            }
            Ok(list.into())
        })
    }

    /// Check budget status
    fn check_budget(&self, current_spend: f64, budget_limit: f64) -> PyBudgetStatus {
        let status = self.inner.check_budget(current_spend, budget_limit);
        PyBudgetStatus { inner: status }
    }

    /// Estimate tokens from text (rough approximation: 1 token per 4 characters)
    fn estimate_tokens(&self, text: String) -> u32 {
        (text.len() / 4).max(1) as u32
    }

    /// Get all available models
    fn get_available_models(&self) -> PyResult<PyObject> {
        let models = self.inner.get_all_models();
        Python::with_gil(|py| {
            let list = PyList::empty(py);
            for model in models {
                list.append(&model.model_name)?;
            }
            Ok(list.into())
        })
    }

    /// String representation
    fn __repr__(&self) -> String {
        "CostCalculator()".to_string()
    }
}

/// Python wrapper for CostEstimate
#[pyclass(name = "CostEstimate")]
pub struct PyCostEstimate {
    pub inner: CostEstimate,
}

#[pymethods]
impl PyCostEstimate {
    /// Get model name
    #[getter]
    fn model_name(&self) -> String {
        self.inner.model_name.clone()
    }

    /// Get currency
    #[getter]
    fn currency(&self) -> String {
        self.inner.currency.clone()
    }

    /// Get input tokens
    #[getter]
    fn input_tokens(&self) -> usize {
        self.inner.input_tokens
    }

    /// Get output tokens
    #[getter]
    fn output_tokens(&self) -> usize {
        self.inner.output_tokens
    }

    /// Get input cost
    #[getter]
    fn input_cost(&self) -> f64 {
        self.inner.input_cost
    }

    /// Get output cost
    #[getter]
    fn output_cost(&self) -> f64 {
        self.inner.output_cost
    }

    /// Get total cost
    #[getter]
    fn total_cost(&self) -> f64 {
        self.inner.total_cost
    }

    /// Get average cost per token
    #[getter]
    fn cost_per_token(&self) -> f64 {
        if self.inner.input_tokens + self.inner.output_tokens > 0 {
            self.inner.total_cost / (self.inner.input_tokens + self.inner.output_tokens) as f64
        } else {
            0.0
        }
    }

    /// Convert to dictionary
    fn to_dict(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            dict.set_item("model_name", &self.inner.model_name)?;
            dict.set_item("currency", &self.inner.currency)?;
            dict.set_item("input_tokens", self.inner.input_tokens)?;
            dict.set_item("output_tokens", self.inner.output_tokens)?;
            dict.set_item("input_cost", self.inner.input_cost)?;
            dict.set_item("output_cost", self.inner.output_cost)?;
            dict.set_item("total_cost", self.inner.total_cost)?;
            dict.set_item("cost_per_token", self.cost_per_token())?;
            Ok(dict.into())
        })
    }

    /// String representation
    fn __repr__(&self) -> String {
        format!(
            "CostEstimate(model='{}', total_cost=${:.4})",
            self.inner.model_name, self.inner.total_cost
        )
    }
}

/// Python wrapper for BudgetStatus
#[pyclass(name = "BudgetStatus")]
pub struct PyBudgetStatus {
    pub inner: BudgetStatus,
}

#[pymethods]
impl PyBudgetStatus {
    /// Get budget alert level
    #[getter]
    fn status(&self) -> String {
        match self.inner.status {
            BudgetAlert::Ok => "ok".to_string(),
            BudgetAlert::Warning => "warning".to_string(),
            BudgetAlert::Critical => "critical".to_string(),
            BudgetAlert::Exceeded => "exceeded".to_string(),
        }
    }

    /// Get current spend
    #[getter]
    fn current_spend(&self) -> f64 {
        self.inner.spent_usd
    }

    /// Get budget limit
    #[getter]
    fn budget_limit(&self) -> f64 {
        self.inner.budget_usd
    }

    /// Get remaining budget
    #[getter]
    fn remaining_budget(&self) -> f64 {
        self.inner.remaining_usd
    }

    /// Get percentage used
    #[getter]
    fn percent_used(&self) -> f64 {
        self.inner.percent_used
    }

    /// Get budget alert status as string
    #[getter]
    fn alert_message(&self) -> String {
        format!(
            "Budget is {}% used - {}",
            (self.inner.percent_used * 100.0) as i32,
            self.status()
        )
    }

    /// Convert to dictionary
    fn to_dict(&self) -> PyResult<PyObject> {
        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            dict.set_item("status", self.status())?;
            dict.set_item("current_spend", self.inner.spent_usd)?;
            dict.set_item("budget_limit", self.inner.budget_usd)?;
            dict.set_item("remaining_budget", self.inner.remaining_usd)?;
            dict.set_item("percent_used", self.inner.percent_used)?;
            dict.set_item("alert_message", self.alert_message())?;
            Ok(dict.into())
        })
    }

    /// String representation
    fn __repr__(&self) -> String {
        format!(
            "BudgetStatus(status='{}', percent_used={:.1}%)",
            self.status(),
            self.inner.percent_used
        )
    }
}
