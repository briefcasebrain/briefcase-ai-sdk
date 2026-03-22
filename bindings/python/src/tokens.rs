use briefcase_core::tokens;
use pyo3::prelude::*;

#[pyfunction]
pub fn count_tokens(text: String, model: Option<String>) -> usize {
    let model_name = model.unwrap_or_else(|| "gpt-3.5-turbo".to_string());
    tokens::count_tokens(&text, &model_name)
}

#[pyfunction]
pub fn count_tokens_batch(texts: Vec<String>, model: Option<String>) -> Vec<usize> {
    let model_name = model.unwrap_or_else(|| "gpt-3.5-turbo".to_string());
    tokens::count_tokens_batch(texts, &model_name)
}
