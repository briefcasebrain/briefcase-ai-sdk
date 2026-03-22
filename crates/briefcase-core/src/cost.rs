use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use thiserror::Error;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelPricing {
    pub model_name: String,
    pub provider: String,
    pub input_cost_per_1k_tokens: f64,  // USD
    pub output_cost_per_1k_tokens: f64, // USD
    pub context_window: usize,
    pub max_output_tokens: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CostEstimate {
    pub model_name: String,
    pub input_tokens: usize,
    pub output_tokens: usize,
    pub input_cost: f64,
    pub output_cost: f64,
    pub total_cost: f64,
    pub currency: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BudgetStatus {
    pub budget_usd: f64,
    pub spent_usd: f64,
    pub remaining_usd: f64,
    pub percent_used: f64,
    pub status: BudgetAlert,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum BudgetAlert {
    Ok,       // < 80% used
    Warning,  // 80-95% used
    Critical, // 95-100% used
    Exceeded, // > 100% used
}

pub struct CostCalculator {
    pricing_table: HashMap<String, ModelPricing>,
}

impl CostCalculator {
    /// Create with default pricing table (OpenAI, Anthropic, etc.)
    pub fn new() -> Self {
        let mut pricing_table = HashMap::new();

        // OpenAI models
        pricing_table.insert(
            "gpt-4".to_string(),
            ModelPricing {
                model_name: "gpt-4".to_string(),
                provider: "openai".to_string(),
                input_cost_per_1k_tokens: 0.03,
                output_cost_per_1k_tokens: 0.06,
                context_window: 8192,
                max_output_tokens: Some(4096),
            },
        );

        pricing_table.insert(
            "gpt-4-turbo".to_string(),
            ModelPricing {
                model_name: "gpt-4-turbo".to_string(),
                provider: "openai".to_string(),
                input_cost_per_1k_tokens: 0.01,
                output_cost_per_1k_tokens: 0.03,
                context_window: 128000,
                max_output_tokens: Some(4096),
            },
        );

        pricing_table.insert(
            "gpt-3.5-turbo".to_string(),
            ModelPricing {
                model_name: "gpt-3.5-turbo".to_string(),
                provider: "openai".to_string(),
                input_cost_per_1k_tokens: 0.0005,
                output_cost_per_1k_tokens: 0.0015,
                context_window: 16385,
                max_output_tokens: Some(4096),
            },
        );

        pricing_table.insert(
            "gpt-4o".to_string(),
            ModelPricing {
                model_name: "gpt-4o".to_string(),
                provider: "openai".to_string(),
                input_cost_per_1k_tokens: 0.005,
                output_cost_per_1k_tokens: 0.015,
                context_window: 128000,
                max_output_tokens: Some(4096),
            },
        );

        pricing_table.insert(
            "gpt-4o-mini".to_string(),
            ModelPricing {
                model_name: "gpt-4o-mini".to_string(),
                provider: "openai".to_string(),
                input_cost_per_1k_tokens: 0.00015,
                output_cost_per_1k_tokens: 0.0006,
                context_window: 128000,
                max_output_tokens: Some(16384),
            },
        );

        // Anthropic models
        pricing_table.insert(
            "claude-3-opus".to_string(),
            ModelPricing {
                model_name: "claude-3-opus".to_string(),
                provider: "anthropic".to_string(),
                input_cost_per_1k_tokens: 0.015,
                output_cost_per_1k_tokens: 0.075,
                context_window: 200000,
                max_output_tokens: Some(4096),
            },
        );

        pricing_table.insert(
            "claude-3-sonnet".to_string(),
            ModelPricing {
                model_name: "claude-3-sonnet".to_string(),
                provider: "anthropic".to_string(),
                input_cost_per_1k_tokens: 0.003,
                output_cost_per_1k_tokens: 0.015,
                context_window: 200000,
                max_output_tokens: Some(4096),
            },
        );

        pricing_table.insert(
            "claude-3-haiku".to_string(),
            ModelPricing {
                model_name: "claude-3-haiku".to_string(),
                provider: "anthropic".to_string(),
                input_cost_per_1k_tokens: 0.00025,
                output_cost_per_1k_tokens: 0.00125,
                context_window: 200000,
                max_output_tokens: Some(4096),
            },
        );

        pricing_table.insert(
            "claude-3-5-sonnet".to_string(),
            ModelPricing {
                model_name: "claude-3-5-sonnet".to_string(),
                provider: "anthropic".to_string(),
                input_cost_per_1k_tokens: 0.003,
                output_cost_per_1k_tokens: 0.015,
                context_window: 200000,
                max_output_tokens: Some(8192),
            },
        );

        // Google models
        pricing_table.insert(
            "gemini-pro".to_string(),
            ModelPricing {
                model_name: "gemini-pro".to_string(),
                provider: "google".to_string(),
                input_cost_per_1k_tokens: 0.0005,
                output_cost_per_1k_tokens: 0.0015,
                context_window: 30720,
                max_output_tokens: Some(2048),
            },
        );

        pricing_table.insert(
            "gemini-ultra".to_string(),
            ModelPricing {
                model_name: "gemini-ultra".to_string(),
                provider: "google".to_string(),
                input_cost_per_1k_tokens: 0.0125,
                output_cost_per_1k_tokens: 0.0375,
                context_window: 30720,
                max_output_tokens: Some(2048),
            },
        );

        Self { pricing_table }
    }

    /// Estimate cost for a given model and token counts
    pub fn estimate_cost(
        &self,
        model_name: &str,
        input_tokens: usize,
        output_tokens: usize,
    ) -> Result<CostEstimate, CostError> {
        let pricing = self
            .pricing_table
            .get(model_name)
            .ok_or_else(|| CostError::UnknownModel(model_name.to_string()))?;

        if input_tokens == 0 && output_tokens == 0 {
            return Err(CostError::InvalidTokenCount);
        }

        // Check if tokens exceed context window
        if input_tokens + output_tokens > pricing.context_window {
            return Err(CostError::InvalidTokenCount);
        }

        // Check if output tokens exceed max output
        if let Some(max_output) = pricing.max_output_tokens {
            if output_tokens > max_output {
                return Err(CostError::InvalidTokenCount);
            }
        }

        let input_cost = (input_tokens as f64 / 1000.0) * pricing.input_cost_per_1k_tokens;
        let output_cost = (output_tokens as f64 / 1000.0) * pricing.output_cost_per_1k_tokens;
        let total_cost = input_cost + output_cost;

        Ok(CostEstimate {
            model_name: model_name.to_string(),
            input_tokens,
            output_tokens,
            input_cost,
            output_cost,
            total_cost,
            currency: "USD".to_string(),
        })
    }

    /// Estimate cost from text (estimates tokens)
    pub fn estimate_cost_from_text(
        &self,
        model_name: &str,
        input_text: &str,
        estimated_output_tokens: usize,
    ) -> Result<CostEstimate, CostError> {
        let input_tokens = self.estimate_tokens(input_text);
        self.estimate_cost(model_name, input_tokens, estimated_output_tokens)
    }

    /// Check budget status
    pub fn check_budget(&self, spent: f64, budget: f64) -> BudgetStatus {
        if budget <= 0.0 {
            return BudgetStatus {
                budget_usd: budget,
                spent_usd: spent,
                remaining_usd: budget - spent,
                percent_used: 100.0,
                status: BudgetAlert::Exceeded,
            };
        }

        let percent_used = (spent / budget) * 100.0;
        let remaining = budget - spent;

        let status = match percent_used {
            p if p >= 100.0 => BudgetAlert::Exceeded,
            p if p >= 95.0 => BudgetAlert::Critical,
            p if p >= 80.0 => BudgetAlert::Warning,
            _ => BudgetAlert::Ok,
        };

        BudgetStatus {
            budget_usd: budget,
            spent_usd: spent,
            remaining_usd: remaining,
            percent_used: percent_used.min(100.0),
            status,
        }
    }

    /// Get cheapest model for a given context size
    pub fn get_cheapest_model(&self, min_context_window: usize) -> Option<&ModelPricing> {
        self.pricing_table
            .values()
            .filter(|pricing| pricing.context_window >= min_context_window)
            .min_by(|a, b| {
                let avg_cost_a = (a.input_cost_per_1k_tokens + a.output_cost_per_1k_tokens) / 2.0;
                let avg_cost_b = (b.input_cost_per_1k_tokens + b.output_cost_per_1k_tokens) / 2.0;
                avg_cost_a
                    .partial_cmp(&avg_cost_b)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
    }

    /// Get all models under a cost threshold (per 1k tokens average)
    pub fn get_models_under_cost(&self, max_cost_per_1k: f64) -> Vec<&ModelPricing> {
        self.pricing_table
            .values()
            .filter(|pricing| {
                let avg_cost =
                    (pricing.input_cost_per_1k_tokens + pricing.output_cost_per_1k_tokens) / 2.0;
                avg_cost <= max_cost_per_1k
            })
            .collect()
    }

    /// Get models by provider
    pub fn get_models_by_provider(&self, provider: &str) -> Vec<&ModelPricing> {
        self.pricing_table
            .values()
            .filter(|pricing| pricing.provider.eq_ignore_ascii_case(provider))
            .collect()
    }

    /// Compare cost between two models for given usage
    pub fn compare_models(
        &self,
        model_a: &str,
        model_b: &str,
        input_tokens: usize,
        output_tokens: usize,
    ) -> Result<ModelComparison, CostError> {
        let cost_a = self.estimate_cost(model_a, input_tokens, output_tokens)?;
        let cost_b = self.estimate_cost(model_b, input_tokens, output_tokens)?;

        let savings = cost_a.total_cost - cost_b.total_cost;
        let percent_difference = if cost_a.total_cost > 0.0 {
            (savings / cost_a.total_cost) * 100.0
        } else {
            0.0
        };

        Ok(ModelComparison {
            model_a: cost_a,
            model_b: cost_b,
            cheaper_model: if savings > 0.0 { model_b } else { model_a }.to_string(),
            savings: savings.abs(),
            percent_difference: percent_difference.abs(),
        })
    }

    /// Add custom model pricing
    pub fn add_model(&mut self, pricing: ModelPricing) {
        self.pricing_table
            .insert(pricing.model_name.clone(), pricing);
    }

    /// Remove a model from pricing table
    pub fn remove_model(&mut self, model_name: &str) -> Option<ModelPricing> {
        self.pricing_table.remove(model_name)
    }

    /// Get all available models
    pub fn get_all_models(&self) -> Vec<&ModelPricing> {
        self.pricing_table.values().collect()
    }

    /// Estimate tokens from text (rough approximation: chars / 4)
    fn estimate_tokens(&self, text: &str) -> usize {
        // Basic tokenization approximation
        // Real-world implementations should use proper tokenizers like tiktoken
        let char_count = text.len();

        // Account for different languages and complexity
        let token_estimate = if text.is_ascii() {
            // English text: roughly 4 chars per token
            (char_count as f64 / 4.0).ceil() as usize
        } else {
            // Non-ASCII text: typically more tokens
            (char_count as f64 / 3.0).ceil() as usize
        };

        // Add some tokens for special tokens, formatting, etc.
        token_estimate + (token_estimate / 20) // Add 5% overhead
    }

    /// Calculate monthly cost projection
    pub fn project_monthly_cost(
        &self,
        model_name: &str,
        daily_input_tokens: usize,
        daily_output_tokens: usize,
        days_per_month: f64,
    ) -> Result<CostProjection, CostError> {
        let daily_cost = self.estimate_cost(model_name, daily_input_tokens, daily_output_tokens)?;
        let monthly_cost = daily_cost.total_cost * days_per_month;

        Ok(CostProjection {
            model_name: model_name.to_string(),
            daily_cost: daily_cost.total_cost,
            monthly_cost,
            annual_cost: monthly_cost * 12.0,
            currency: "USD".to_string(),
        })
    }
}

impl Default for CostCalculator {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, Clone)]
pub struct ModelComparison {
    pub model_a: CostEstimate,
    pub model_b: CostEstimate,
    pub cheaper_model: String,
    pub savings: f64,
    pub percent_difference: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CostProjection {
    pub model_name: String,
    pub daily_cost: f64,
    pub monthly_cost: f64,
    pub annual_cost: f64,
    pub currency: String,
}

#[derive(Error, Debug, Clone, PartialEq)]
pub enum CostError {
    #[error("Unknown model: {0}")]
    UnknownModel(String),
    #[error("Invalid token count")]
    InvalidTokenCount,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cost_estimation() {
        let calculator = CostCalculator::new();

        let estimate = calculator.estimate_cost("gpt-4", 1000, 500).unwrap();

        assert_eq!(estimate.model_name, "gpt-4");
        assert_eq!(estimate.input_tokens, 1000);
        assert_eq!(estimate.output_tokens, 500);
        assert_eq!(estimate.input_cost, 0.03); // 1000 tokens = 1k tokens * $0.03
        assert_eq!(estimate.output_cost, 0.03); // 500 tokens = 0.5k tokens * $0.06
        assert_eq!(estimate.total_cost, 0.06);
        assert_eq!(estimate.currency, "USD");
    }

    #[test]
    fn test_unknown_model() {
        let calculator = CostCalculator::new();
        let result = calculator.estimate_cost("unknown-model", 1000, 500);

        assert!(matches!(result, Err(CostError::UnknownModel(_))));
    }

    #[test]
    fn test_invalid_token_count() {
        let calculator = CostCalculator::new();

        // Zero tokens
        let result = calculator.estimate_cost("gpt-4", 0, 0);
        assert!(matches!(result, Err(CostError::InvalidTokenCount)));

        // Exceeding context window (gpt-4 has 8192)
        let result = calculator.estimate_cost("gpt-4", 10000, 0);
        assert!(matches!(result, Err(CostError::InvalidTokenCount)));

        // Exceeding max output tokens (gpt-4 has 4096)
        let result = calculator.estimate_cost("gpt-4", 1000, 5000);
        assert!(matches!(result, Err(CostError::InvalidTokenCount)));
    }

    #[test]
    fn test_budget_status() {
        let calculator = CostCalculator::new();

        // OK status
        let status = calculator.check_budget(50.0, 100.0);
        assert_eq!(status.status, BudgetAlert::Ok);
        assert_eq!(status.percent_used, 50.0);
        assert_eq!(status.remaining_usd, 50.0);

        // Warning status
        let status = calculator.check_budget(85.0, 100.0);
        assert_eq!(status.status, BudgetAlert::Warning);

        // Critical status
        let status = calculator.check_budget(96.0, 100.0);
        assert_eq!(status.status, BudgetAlert::Critical);

        // Exceeded status
        let status = calculator.check_budget(110.0, 100.0);
        assert_eq!(status.status, BudgetAlert::Exceeded);
        assert_eq!(status.remaining_usd, -10.0);
    }

    #[test]
    fn test_cheapest_model() {
        let calculator = CostCalculator::new();

        let cheapest = calculator.get_cheapest_model(8000);
        assert!(cheapest.is_some());
        let model = cheapest.unwrap();

        // Should be one of the cheaper models with sufficient context
        assert!(model.context_window >= 8000);
    }

    #[test]
    fn test_models_under_cost() {
        let calculator = CostCalculator::new();

        let cheap_models = calculator.get_models_under_cost(0.01);
        assert!(!cheap_models.is_empty());

        // All returned models should have average cost <= 0.01
        for model in &cheap_models {
            let avg_cost = (model.input_cost_per_1k_tokens + model.output_cost_per_1k_tokens) / 2.0;
            assert!(avg_cost <= 0.01);
        }
    }

    #[test]
    fn test_models_by_provider() {
        let calculator = CostCalculator::new();

        let openai_models = calculator.get_models_by_provider("openai");
        assert!(!openai_models.is_empty());
        for model in &openai_models {
            assert_eq!(model.provider, "openai");
        }

        let anthropic_models = calculator.get_models_by_provider("anthropic");
        assert!(!anthropic_models.is_empty());
        for model in &anthropic_models {
            assert_eq!(model.provider, "anthropic");
        }
    }

    #[test]
    fn test_model_comparison() {
        let calculator = CostCalculator::new();

        let comparison = calculator
            .compare_models("gpt-4", "gpt-3.5-turbo", 1000, 500)
            .unwrap();

        // GPT-3.5-turbo should be cheaper than GPT-4
        assert_eq!(comparison.cheaper_model, "gpt-3.5-turbo");
        assert!(comparison.savings > 0.0);
        assert!(comparison.percent_difference > 0.0);
    }

    #[test]
    fn test_cost_from_text() {
        let calculator = CostCalculator::new();

        let text = "Hello, world!";
        let estimate = calculator
            .estimate_cost_from_text("gpt-3.5-turbo", text, 100)
            .unwrap();

        assert!(estimate.input_tokens > 0);
        assert_eq!(estimate.output_tokens, 100);
        assert!(estimate.total_cost > 0.0);
    }

    #[test]
    fn test_token_estimation() {
        let calculator = CostCalculator::new();

        // English text
        let english_text = "Hello, world! This is a test.";
        let tokens = calculator.estimate_tokens(english_text);

        // Should be roughly chars/4 with some overhead
        let expected = ((english_text.len() as f64 / 4.0).ceil() as usize * 105) / 100; // 5% overhead
        assert!(tokens >= expected - 2 && tokens <= expected + 2);

        // Empty text
        assert_eq!(calculator.estimate_tokens(""), 0);
    }

    #[test]
    fn test_custom_model() {
        let mut calculator = CostCalculator::new();

        let custom_model = ModelPricing {
            model_name: "custom-model".to_string(),
            provider: "custom".to_string(),
            input_cost_per_1k_tokens: 0.001,
            output_cost_per_1k_tokens: 0.002,
            context_window: 4096,
            max_output_tokens: Some(2048),
        };

        calculator.add_model(custom_model.clone());

        let estimate = calculator.estimate_cost("custom-model", 1000, 500).unwrap();
        assert_eq!(estimate.input_cost, 0.001);
        assert_eq!(estimate.output_cost, 0.001);
        assert_eq!(estimate.total_cost, 0.002);

        // Test removal
        let removed = calculator.remove_model("custom-model");
        assert!(removed.is_some());
        assert_eq!(removed.unwrap().model_name, "custom-model");

        // Should no longer be available
        let result = calculator.estimate_cost("custom-model", 1000, 500);
        assert!(matches!(result, Err(CostError::UnknownModel(_))));
    }

    #[test]
    fn test_cost_projection() {
        let calculator = CostCalculator::new();

        let projection = calculator
            .project_monthly_cost("gpt-4", 4000, 2000, 30.0)
            .unwrap();

        assert_eq!(projection.model_name, "gpt-4");
        assert!(projection.daily_cost > 0.0);
        assert_eq!(projection.monthly_cost, projection.daily_cost * 30.0);
        assert_eq!(projection.annual_cost, projection.monthly_cost * 12.0);
    }

    #[test]
    fn test_all_default_models_available() {
        let calculator = CostCalculator::new();

        // Test all default models can be used for cost estimation
        let test_models = [
            "gpt-4",
            "gpt-4-turbo",
            "gpt-3.5-turbo",
            "gpt-4o",
            "gpt-4o-mini",
            "claude-3-opus",
            "claude-3-sonnet",
            "claude-3-haiku",
            "claude-3-5-sonnet",
            "gemini-pro",
            "gemini-ultra",
        ];

        for model in &test_models {
            let result = calculator.estimate_cost(model, 1000, 500);
            assert!(result.is_ok(), "Model {} should be available", model);
        }
    }
}
