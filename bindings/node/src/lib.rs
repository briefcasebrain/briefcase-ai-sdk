//! Node binding for the briefcase-core cost engine.
//!
//! Deliberately minimal: model pricing and id normalization, the surfaces a
//! JavaScript consumer prices usage with. Storage, replay, sanitization, and
//! token counting stay Python/Rust-only until a Node consumer needs them.

#![deny(clippy::all)]

use briefcase_core::CostCalculator as CoreCalculator;
use napi::bindgen_prelude::*;
use napi_derive::napi;

/// Cost estimate for one model invocation, in USD.
#[napi(object)]
pub struct CostEstimate {
    pub model_name: String,
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub input_cost: f64,
    pub output_cost: f64,
    pub total_cost: f64,
}

/// Canonicalizes a platform-qualified model id (region and vendor prefixes,
/// date stamps, version suffixes) to a pricing-table key. Cost estimation
/// applies this automatically when the exact id is not registered.
#[napi]
pub fn normalize_model_id(model_id: String) -> String {
    CoreCalculator::normalize_model_id(&model_id)
}

/// Prices model invocations from the built-in table of first-party and
/// Bedrock list rates.
#[napi]
pub struct CostCalculator {
    inner: CoreCalculator,
}

#[napi]
impl CostCalculator {
    #[napi(constructor)]
    #[allow(clippy::new_without_default)]
    pub fn new() -> Self {
        Self {
            inner: CoreCalculator::new(),
        }
    }

    /// Estimates the cost of one invocation. Unknown models reject with an
    /// error naming the id; platform-qualified ids normalize automatically.
    #[napi]
    pub fn estimate_cost(
        &self,
        model_name: String,
        input_tokens: u32,
        output_tokens: u32,
    ) -> Result<CostEstimate> {
        let est = self
            .inner
            .estimate_cost(&model_name, input_tokens as usize, output_tokens as usize)
            .map_err(|e| Error::from_reason(e.to_string()))?;
        Ok(CostEstimate {
            model_name: est.model_name,
            input_tokens,
            output_tokens,
            input_cost: est.input_cost,
            output_cost: est.output_cost,
            total_cost: est.total_cost,
        })
    }

    /// Lists every registered pricing-table model id.
    #[napi]
    pub fn model_ids(&self) -> Vec<String> {
        let mut ids: Vec<String> = self
            .inner
            .get_all_models()
            .into_iter()
            .map(|p| p.model_name.clone())
            .collect();
        ids.sort();
        ids
    }
}
