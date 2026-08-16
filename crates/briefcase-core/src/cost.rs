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

impl ModelPricing {
    /// Build flat pricing from provider per-MTok (per-1,000,000-token) prices,
    /// converting to the per-1K unit stored internally (`$5/MTok` → `0.005/1K`).
    fn from_mtok(
        model_name: &str,
        provider: &str,
        input_per_mtok: f64,
        output_per_mtok: f64,
        context_window: usize,
        max_output_tokens: Option<usize>,
    ) -> Self {
        ModelPricing {
            model_name: model_name.to_string(),
            provider: provider.to_string(),
            input_cost_per_1k_tokens: input_per_mtok / 1000.0,
            output_cost_per_1k_tokens: output_per_mtok / 1000.0,
            context_window,
            max_output_tokens,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CostEstimate {
    pub model_name: String,
    pub input_tokens: usize,
    pub output_tokens: usize,
    pub input_cost: f64,
    pub output_cost: f64,
    /// Cost of prompt-cache reads/writes (0.0 unless cache tokens were supplied).
    #[serde(default)]
    pub cache_cost: f64,
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

/// Cloud platform a model is billed through. Token rates match the first-party
/// API across platforms; the difference is captured by modifiers such as the
/// regional-endpoint premium (see [`RateCard::regional_endpoint`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub enum Platform {
    /// Provider's own API (Anthropic / OpenAI / Google).
    #[default]
    FirstParty,
    /// AWS Bedrock.
    Bedrock,
    /// Google Vertex AI.
    Vertex,
    /// Azure OpenAI.
    Azure,
}

/// Pricing tier within a platform.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub enum PricingTier {
    /// On-demand pricing.
    #[default]
    Standard,
    /// Asynchronous batch processing — 0.5x of standard input and output.
    Batch,
    /// Bill `input_tokens` at the model's cache-read rate (fully-cached input).
    Cached,
    /// Priority/premium tier — per-model multiplier (errors if the model has none).
    Priority,
    /// Flex tier (Gemini) — 0.5x of standard.
    Flex,
}

/// Data-residency selection. `Us` applies a 1.1x multiplier on eligible models.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub enum DataResidency {
    /// Global routing (default), standard pricing.
    #[default]
    Global,
    /// US-only inference (`inference_geo=us`) — 1.1x on eligible models.
    Us,
}

/// A selectable rate card: `platform × tier × modifiers`. The [`Default`]
/// (first-party, standard, global, no premium) reproduces legacy pricing.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, Default)]
pub struct RateCard {
    pub platform: Platform,
    pub tier: PricingTier,
    pub data_residency: DataResidency,
    /// Regional/multi-region endpoint — +10% on eligible models.
    pub regional_endpoint: bool,
    /// Use the model's fast-mode premium rates (if defined).
    pub fast_mode: bool,
}

impl RateCard {
    /// Parse a forgiving string such as `"batch"`, `"bedrock:batch"`,
    /// `"vertex/standard,us"`, `"azure regional"`, or `"first_party/fast"`.
    /// Tokens may be separated by any of `/ : , +` or whitespace; order is
    /// irrelevant. An empty string (or `"standard"`) yields the default card.
    pub fn parse(s: &str) -> Result<RateCard, CostError> {
        let mut card = RateCard::default();
        for raw in
            s.split(|c: char| c == '/' || c == ':' || c == ',' || c == '+' || c.is_whitespace())
        {
            let token = raw.trim().to_ascii_lowercase();
            if token.is_empty() {
                continue;
            }
            match token.as_str() {
                "first_party" | "firstparty" | "first-party" | "direct" | "anthropic"
                | "openai" | "google" => card.platform = Platform::FirstParty,
                "bedrock" | "aws" => card.platform = Platform::Bedrock,
                "vertex" | "vertexai" | "gcp" => card.platform = Platform::Vertex,
                "azure" => card.platform = Platform::Azure,
                "standard" | "ondemand" | "on-demand" => card.tier = PricingTier::Standard,
                "batch" => card.tier = PricingTier::Batch,
                "cached" | "cache" => card.tier = PricingTier::Cached,
                "priority" => card.tier = PricingTier::Priority,
                "flex" => card.tier = PricingTier::Flex,
                "regional" | "region" | "multiregion" | "multi-region" => {
                    card.regional_endpoint = true
                }
                "us" | "residency" | "data-residency" | "us-only" => {
                    card.data_residency = DataResidency::Us
                }
                "global" => card.data_residency = DataResidency::Global,
                "fast" | "fast-mode" | "fastmode" => card.fast_mode = true,
                other => return Err(CostError::UnknownRateCard(other.to_string())),
            }
        }
        Ok(card)
    }
}

/// Per-call token usage. The cache fields default to 0, so the common
/// `{ input_tokens, output_tokens }` form behaves like the legacy path.
#[derive(Debug, Clone, Copy, Default)]
pub struct TokenUsage {
    pub input_tokens: usize,
    pub output_tokens: usize,
    pub cache_read_tokens: usize,
    pub cache_write_5m_tokens: usize,
    pub cache_write_1h_tokens: usize,
}

/// Prompt-cache rates (per-1K USD, absolute). `read` is the cache-hit rate;
/// writes are the 5-minute / 1-hour cache-creation rates (Anthropic only).
#[derive(Debug, Clone, Copy)]
struct CacheRates {
    read_per_1k: f64,
    write_5m_per_1k: Option<f64>,
    write_1h_per_1k: Option<f64>,
}

impl CacheRates {
    /// Anthropic-style cache rates from per-MTok prices.
    fn from_mtok(read: f64, write_5m: f64, write_1h: f64) -> Self {
        Self {
            read_per_1k: read / 1000.0,
            write_5m_per_1k: Some(write_5m / 1000.0),
            write_1h_per_1k: Some(write_1h / 1000.0),
        }
    }
    /// Read-only cache rate (OpenAI cached-input / Gemini cache) from per-MTok.
    fn read_only_mtok(read: f64) -> Self {
        Self {
            read_per_1k: read / 1000.0,
            write_5m_per_1k: None,
            write_1h_per_1k: None,
        }
    }
}

/// An input/output rate pair (per-1K USD).
#[derive(Debug, Clone, Copy)]
struct TokenRates {
    input_per_1k: f64,
    output_per_1k: f64,
}

impl TokenRates {
    fn from_mtok(input: f64, output: f64) -> Self {
        Self {
            input_per_1k: input / 1000.0,
            output_per_1k: output / 1000.0,
        }
    }
}

/// A long-context pricing tier: when `input_tokens` exceeds `threshold_tokens`
/// the whole request is billed at these (per-1K) rates instead of the base.
#[derive(Debug, Clone, Copy)]
struct ContextTier {
    threshold_tokens: usize,
    input_per_1k: f64,
    output_per_1k: f64,
}

impl ContextTier {
    fn from_mtok(threshold_tokens: usize, input: f64, output: f64) -> Self {
        Self {
            threshold_tokens,
            input_per_1k: input / 1000.0,
            output_per_1k: output / 1000.0,
        }
    }
}

/// Eligibility flags for percentage modifiers.
#[derive(Debug, Clone, Copy, Default)]
struct ModifierFlags {
    /// Eligible for the US data-residency 1.1x multiplier.
    supports_data_residency: bool,
    /// Eligible for the regional/multi-region endpoint +10% premium.
    supports_regional_premium: bool,
}

/// Rich rate-card data attached to a model, alongside the flat [`ModelPricing`].
/// Models without an entry resolve to first-party standard pricing only.
#[derive(Debug, Clone, Default)]
struct PricingExtras {
    cache_rates: Option<CacheRates>,
    fast_mode_rates: Option<TokenRates>,
    long_context_tiers: Vec<ContextTier>,
    priority_multiplier: Option<f64>,
    /// Per-MTok-per-hour context-cache storage rate (time-based; see
    /// [`CostCalculator::estimate_cache_storage_cost`]).
    cache_storage_per_mtok_hour: Option<f64>,
    flags: ModifierFlags,
}

pub struct CostCalculator {
    pricing_table: HashMap<String, ModelPricing>,
    /// Rich rate-card data keyed by model name (parallel to `pricing_table`).
    extras: HashMap<String, PricingExtras>,
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

        let mut calculator = Self {
            pricing_table,
            extras: HashMap::new(),
        };
        calculator.install_modern_models();
        calculator
    }

    /// Canonicalizes a platform-qualified model id to a pricing-table key:
    /// strips region prefixes (us./eu./apac./global.), vendor prefixes
    /// (anthropic./amazon./cohere./mistral./meta.), a trailing -YYYYMMDD
    /// date stamp, and -vN:M / :N version suffixes. Lookups try the exact id
    /// first, so a registered platform-qualified name always wins.
    pub fn normalize_model_id(model_id: &str) -> String {
        let mut id = model_id;
        for region in ["us.", "eu.", "apac.", "global."] {
            if let Some(rest) = id.strip_prefix(region) {
                id = rest;
                break;
            }
        }
        for vendor in ["anthropic.", "amazon.", "cohere.", "mistral.", "meta."] {
            if let Some(rest) = id.strip_prefix(vendor) {
                id = rest;
                break;
            }
        }
        let mut out = id.to_string();
        // -vN:M version suffix, e.g. -v1:0
        if let Some(pos) = out.rfind("-v") {
            let tail = &out[pos + 2..];
            if !tail.is_empty()
                && tail
                    .chars()
                    .all(|c| c.is_ascii_digit() || c == ':')
            {
                out.truncate(pos);
            }
        }
        // bare :N suffix
        if let Some(pos) = out.rfind(':') {
            if out[pos + 1..].chars().all(|c| c.is_ascii_digit())
                && !out[pos + 1..].is_empty()
            {
                out.truncate(pos);
            }
        }
        // trailing -YYYYMMDD date stamp
        if out.len() > 9 {
            let (head, tail) = out.split_at(out.len() - 9);
            if tail.starts_with('-') && tail[1..].chars().all(|c| c.is_ascii_digit()) {
                out = head.to_string();
            }
        }
        out
    }

    /// Resolves a model id to the pricing-table key: the exact id when
    /// registered, otherwise its normalized form when that is registered.
    fn resolve_model_key(&self, model_name: &str) -> Option<String> {
        if self.pricing_table.contains_key(model_name) {
            return Some(model_name.to_string());
        }
        let normalized = Self::normalize_model_id(model_name);
        if self.pricing_table.contains_key(&normalized) {
            return Some(normalized);
        }
        None
    }

    /// Register a model's flat pricing together with its rate-card extras.
    fn insert_model(&mut self, pricing: ModelPricing, extras: PricingExtras) {
        let name = pricing.model_name.clone();
        self.extras.insert(name.clone(), extras);
        self.pricing_table.insert(name, pricing);
    }

    /// Add the current (2026) Claude 4.x, GPT-5.x, and Gemini 2.5/3.x models
    /// with their rate-card data. Earlier models remain registered in `new()`.
    fn install_modern_models(&mut self) {
        // ---- Anthropic (Claude API, first-party) ----
        // Opus 4.x / Sonnet 4.x share: cache read 0.1x, 5m-write 1.25x, 1h-write 2x.
        self.insert_model(
            ModelPricing::from_mtok(
                "claude-opus-4-8",
                "anthropic",
                5.0,
                25.0,
                1_000_000,
                Some(64_000),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::from_mtok(0.5, 6.25, 10.0)),
                fast_mode_rates: Some(TokenRates::from_mtok(10.0, 50.0)),
                flags: ModifierFlags {
                    supports_data_residency: true,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "claude-opus-4-7",
                "anthropic",
                5.0,
                25.0,
                1_000_000,
                Some(64_000),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::from_mtok(0.5, 6.25, 10.0)),
                fast_mode_rates: Some(TokenRates::from_mtok(30.0, 150.0)),
                flags: ModifierFlags {
                    supports_data_residency: true,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "claude-opus-4-6",
                "anthropic",
                5.0,
                25.0,
                1_000_000,
                Some(64_000),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::from_mtok(0.5, 6.25, 10.0)),
                fast_mode_rates: Some(TokenRates::from_mtok(30.0, 150.0)),
                flags: ModifierFlags {
                    supports_data_residency: true,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "claude-opus-4-5",
                "anthropic",
                5.0,
                25.0,
                200_000,
                Some(64_000),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::from_mtok(0.5, 6.25, 10.0)),
                flags: ModifierFlags {
                    supports_data_residency: false,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "claude-opus-4-1",
                "anthropic",
                15.0,
                75.0,
                200_000,
                Some(32_000),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::from_mtok(1.5, 18.75, 30.0)),
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "claude-sonnet-4-6",
                "anthropic",
                3.0,
                15.0,
                1_000_000,
                Some(64_000),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::from_mtok(0.3, 3.75, 6.0)),
                flags: ModifierFlags {
                    supports_data_residency: true,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "claude-sonnet-4-5",
                "anthropic",
                3.0,
                15.0,
                200_000,
                Some(64_000),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::from_mtok(0.3, 3.75, 6.0)),
                flags: ModifierFlags {
                    supports_data_residency: false,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "claude-haiku-4-5",
                "anthropic",
                1.0,
                5.0,
                200_000,
                Some(64_000),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::from_mtok(0.1, 1.25, 2.0)),
                flags: ModifierFlags {
                    supports_data_residency: false,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "claude-haiku-3-5",
                "anthropic",
                0.8,
                4.0,
                200_000,
                Some(8_192),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::from_mtok(0.08, 1.0, 1.6)),
                ..Default::default()
            },
        );

        // ---- OpenAI (first-party) ----
        // Cached-input ~= 0.1x; batch = 0.5x; regional +10% (models >= 2026-03-05).
        self.insert_model(
            ModelPricing::from_mtok("gpt-5.5", "openai", 5.0, 30.0, 1_050_000, Some(128_000)),
            PricingExtras {
                cache_rates: Some(CacheRates::read_only_mtok(0.5)),
                priority_multiplier: Some(2.5),
                flags: ModifierFlags {
                    supports_data_residency: false,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok("gpt-5.5-pro", "openai", 30.0, 180.0, 400_000, Some(128_000)),
            PricingExtras {
                flags: ModifierFlags {
                    supports_data_residency: false,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok("gpt-5.4", "openai", 2.5, 15.0, 400_000, Some(128_000)),
            PricingExtras {
                cache_rates: Some(CacheRates::read_only_mtok(0.25)),
                flags: ModifierFlags {
                    supports_data_residency: false,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok("gpt-5.4-mini", "openai", 0.75, 4.5, 400_000, Some(128_000)),
            PricingExtras {
                cache_rates: Some(CacheRates::read_only_mtok(0.075)),
                flags: ModifierFlags {
                    supports_data_residency: false,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok("gpt-5.4-nano", "openai", 0.2, 1.25, 400_000, Some(128_000)),
            PricingExtras {
                cache_rates: Some(CacheRates::read_only_mtok(0.02)),
                flags: ModifierFlags {
                    supports_data_residency: false,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok("gpt-5.4-pro", "openai", 30.0, 180.0, 400_000, Some(128_000)),
            PricingExtras {
                flags: ModifierFlags {
                    supports_data_residency: false,
                    supports_regional_premium: true,
                },
                ..Default::default()
            },
        );

        // ---- Google Gemini ----
        // Batch/Flex = 0.5x; Pro models price >200K prompts at a higher tier.
        self.insert_model(
            ModelPricing::from_mtok(
                "gemini-3.5-flash",
                "google",
                1.5,
                9.0,
                1_048_576,
                Some(65_536),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::read_only_mtok(0.15)),
                priority_multiplier: Some(1.8),
                cache_storage_per_mtok_hour: Some(1.0),
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "gemini-3.1-pro",
                "google",
                2.0,
                12.0,
                1_048_576,
                Some(65_536),
            ),
            PricingExtras {
                long_context_tiers: vec![ContextTier::from_mtok(200_000, 4.0, 18.0)],
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "gemini-3.1-flash-lite",
                "google",
                0.25,
                1.5,
                1_048_576,
                Some(65_536),
            ),
            PricingExtras::default(),
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "gemini-3-flash",
                "google",
                0.5,
                3.0,
                1_048_576,
                Some(65_536),
            ),
            PricingExtras::default(),
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "gemini-2.5-pro",
                "google",
                1.25,
                10.0,
                1_048_576,
                Some(65_536),
            ),
            PricingExtras {
                cache_rates: Some(CacheRates::read_only_mtok(0.125)),
                cache_storage_per_mtok_hour: Some(4.5),
                long_context_tiers: vec![ContextTier::from_mtok(200_000, 2.5, 15.0)],
                ..Default::default()
            },
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "gemini-2.5-flash",
                "google",
                0.3,
                2.5,
                1_048_576,
                Some(65_536),
            ),
            PricingExtras::default(),
        );
        self.insert_model(
            ModelPricing::from_mtok(
                "gemini-2.5-flash-lite",
                "google",
                0.1,
                0.4,
                1_048_576,
                Some(65_536),
            ),
            PricingExtras::default(),
        );

        // ---- Amazon Nova and Bedrock embedding models ----
        // Flat rates are the Bedrock us-east-1 list prices; these models are
        // served through Bedrock, so the first-party rate is the Bedrock rate.
        self.insert_model(
            ModelPricing::from_mtok("nova-premier", "amazon", 2.5, 12.5, 1_000_000, Some(10_000)),
            PricingExtras::default(),
        );
        self.insert_model(
            ModelPricing::from_mtok("nova-pro", "amazon", 0.8, 3.2, 300_000, Some(5_120)),
            PricingExtras::default(),
        );
        self.insert_model(
            ModelPricing::from_mtok("nova-lite", "amazon", 0.06, 0.24, 300_000, Some(5_120)),
            PricingExtras::default(),
        );
        self.insert_model(
            ModelPricing::from_mtok("nova-micro", "amazon", 0.035, 0.14, 128_000, Some(5_120)),
            PricingExtras::default(),
        );
        self.insert_model(
            ModelPricing::from_mtok("titan-embed-text", "amazon", 0.1, 0.0, 8_192, None),
            PricingExtras::default(),
        );
        self.insert_model(
            ModelPricing::from_mtok("cohere-embed", "cohere", 0.1, 0.0, 8_192, None),
            PricingExtras::default(),
        );
    }

    /// Estimate cost for a given model and token counts (first-party standard
    /// pricing). Equivalent to [`Self::estimate_cost_with_card`] with the
    /// default [`RateCard`] and no cache tokens.
    pub fn estimate_cost(
        &self,
        model_name: &str,
        input_tokens: usize,
        output_tokens: usize,
    ) -> Result<CostEstimate, CostError> {
        self.estimate_cost_with_card(
            model_name,
            TokenUsage {
                input_tokens,
                output_tokens,
                ..Default::default()
            },
            RateCard::default(),
        )
    }

    /// Estimate cost under a specific [`RateCard`] with full token accounting
    /// (including prompt-cache reads/writes). This is the rate-card engine that
    /// [`Self::estimate_cost`] delegates to.
    pub fn estimate_cost_with_card(
        &self,
        model_name: &str,
        usage: TokenUsage,
        card: RateCard,
    ) -> Result<CostEstimate, CostError> {
        let key = self
            .resolve_model_key(model_name)
            .ok_or_else(|| CostError::UnknownModel(model_name.to_string()))?;
        let pricing = self
            .pricing_table
            .get(&key)
            .ok_or_else(|| CostError::UnknownModel(model_name.to_string()))?;
        let extras = self.extras.get(&key);

        let billable = usage.input_tokens
            + usage.output_tokens
            + usage.cache_read_tokens
            + usage.cache_write_5m_tokens
            + usage.cache_write_1h_tokens;
        if billable == 0 {
            return Err(CostError::InvalidTokenCount);
        }

        // Context-window and max-output checks count only input + output;
        // cache tokens are a billing dimension, not additional context.
        if usage.input_tokens + usage.output_tokens > pricing.context_window {
            return Err(CostError::InvalidTokenCount);
        }
        if let Some(max_output) = pricing.max_output_tokens {
            if usage.output_tokens > max_output {
                return Err(CostError::InvalidTokenCount);
            }
        }

        // 1. Base rates.
        let mut base_in = pricing.input_cost_per_1k_tokens;
        let mut base_out = pricing.output_cost_per_1k_tokens;

        // 2. Long-context tier (base replacement): highest threshold below the
        //    input size wins.
        if let Some(ex) = extras {
            if let Some(tier) = ex
                .long_context_tiers
                .iter()
                .filter(|t| usage.input_tokens > t.threshold_tokens)
                .max_by_key(|t| t.threshold_tokens)
            {
                base_in = tier.input_per_1k;
                base_out = tier.output_per_1k;
            }
        }

        // 3. Fast-mode (absolute base override, not a multiplier).
        if card.fast_mode {
            if let Some(fm) = extras.and_then(|e| e.fast_mode_rates) {
                base_in = fm.input_per_1k;
                base_out = fm.output_per_1k;
            }
        }

        // 4. Tier multiplier (Cached is handled separately on the input leg).
        let tier_mult = match card.tier {
            PricingTier::Standard | PricingTier::Cached => 1.0,
            PricingTier::Batch | PricingTier::Flex => 0.5,
            PricingTier::Priority => match extras.and_then(|e| e.priority_multiplier) {
                Some(m) => m,
                None => {
                    return Err(CostError::TierUnavailable {
                        model: model_name.to_string(),
                        tier: "priority".to_string(),
                    })
                }
            },
        };

        // 5. Platform regional-endpoint premium (+10% on eligible models).
        let mut platform_mult = 1.0;
        if card.regional_endpoint
            && extras
                .map(|e| e.flags.supports_regional_premium)
                .unwrap_or(false)
        {
            platform_mult *= 1.10;
        }

        // 6. Data-residency premium (+10% on eligible models).
        let mut residency_mult = 1.0;
        if card.data_residency == DataResidency::Us
            && extras
                .map(|e| e.flags.supports_data_residency)
                .unwrap_or(false)
        {
            residency_mult *= 1.10;
        }

        let mult = tier_mult * platform_mult * residency_mult;

        // 7. Effective per-1K rates. For the Cached tier, fresh input is billed
        //    at the cache-read rate (tier_mult is 1.0, so `mult` carries only
        //    the platform/residency premiums).
        let eff_in = if card.tier == PricingTier::Cached {
            extras
                .and_then(|e| e.cache_rates)
                .map(|c| c.read_per_1k)
                .unwrap_or(base_in)
                * mult
        } else {
            base_in * mult
        };
        let eff_out = base_out * mult;

        // 8. Explicit cache legs. Batch halves them; priority never scales them.
        let cache_leg_mult = if card.tier == PricingTier::Batch {
            0.5
        } else {
            1.0
        };
        let mut cache_cost = 0.0;
        if let Some(cr) = extras.and_then(|e| e.cache_rates) {
            cache_cost +=
                (usage.cache_read_tokens as f64 / 1000.0) * cr.read_per_1k * cache_leg_mult;
            if let Some(w5) = cr.write_5m_per_1k {
                cache_cost += (usage.cache_write_5m_tokens as f64 / 1000.0) * w5 * cache_leg_mult;
            }
            if let Some(w1) = cr.write_1h_per_1k {
                cache_cost += (usage.cache_write_1h_tokens as f64 / 1000.0) * w1 * cache_leg_mult;
            }
        }

        let input_cost = (usage.input_tokens as f64 / 1000.0) * eff_in;
        let output_cost = (usage.output_tokens as f64 / 1000.0) * eff_out;
        let total_cost = input_cost + output_cost + cache_cost;

        Ok(CostEstimate {
            model_name: model_name.to_string(),
            input_tokens: usage.input_tokens,
            output_tokens: usage.output_tokens,
            input_cost,
            output_cost,
            cache_cost,
            total_cost,
            currency: "USD".to_string(),
        })
    }

    /// Ergonomic shim: estimate cost under a [`RateCard`] without building a
    /// [`TokenUsage`] (no explicit cache tokens).
    pub fn estimate_cost_on(
        &self,
        model_name: &str,
        input_tokens: usize,
        output_tokens: usize,
        card: RateCard,
    ) -> Result<CostEstimate, CostError> {
        self.estimate_cost_with_card(
            model_name,
            TokenUsage {
                input_tokens,
                output_tokens,
                ..Default::default()
            },
            card,
        )
    }

    /// Representative rate-card identifiers understood by [`RateCard::parse`].
    /// Any `platform × tier × modifier` combination is also accepted.
    pub fn available_rate_cards(&self) -> Vec<String> {
        [
            "standard",
            "batch",
            "cached",
            "priority",
            "flex",
            "first_party:fast",
            "first_party:standard,us",
            "bedrock:standard",
            "bedrock:batch",
            "bedrock:standard,regional",
            "vertex:standard",
            "vertex:batch",
            "vertex:standard,regional",
            "azure:standard",
            "azure:standard,regional",
        ]
        .into_iter()
        .map(String::from)
        .collect()
    }

    /// Estimate the time-based context-cache *storage* cost (separate from the
    /// per-call read/write token costs). Returns 0.0 for models without a
    /// storage rate. Errors only on an unknown model.
    pub fn estimate_cache_storage_cost(
        &self,
        model_name: &str,
        cached_tokens: usize,
        hours: f64,
    ) -> Result<f64, CostError> {
        self.pricing_table
            .get(model_name)
            .ok_or_else(|| CostError::UnknownModel(model_name.to_string()))?;
        let rate = self
            .extras
            .get(model_name)
            .and_then(|e| e.cache_storage_per_mtok_hour)
            .unwrap_or(0.0);
        Ok((cached_tokens as f64 / 1_000_000.0) * rate * hours)
    }

    /// Estimate cost from text (estimates tokens) under standard pricing.
    pub fn estimate_cost_from_text(
        &self,
        model_name: &str,
        input_text: &str,
        estimated_output_tokens: usize,
    ) -> Result<CostEstimate, CostError> {
        self.estimate_cost_from_text_with_card(
            model_name,
            input_text,
            estimated_output_tokens,
            RateCard::default(),
        )
    }

    /// Estimate cost from text under a specific [`RateCard`].
    pub fn estimate_cost_from_text_with_card(
        &self,
        model_name: &str,
        input_text: &str,
        estimated_output_tokens: usize,
        card: RateCard,
    ) -> Result<CostEstimate, CostError> {
        let input_tokens = self.estimate_tokens(input_text);
        self.estimate_cost_on(model_name, input_tokens, estimated_output_tokens, card)
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

    /// Calculate monthly cost projection under standard pricing.
    pub fn project_monthly_cost(
        &self,
        model_name: &str,
        daily_input_tokens: usize,
        daily_output_tokens: usize,
        days_per_month: f64,
    ) -> Result<CostProjection, CostError> {
        self.project_monthly_cost_with_card(
            model_name,
            daily_input_tokens,
            daily_output_tokens,
            days_per_month,
            RateCard::default(),
        )
    }

    /// Calculate monthly cost projection under a specific [`RateCard`].
    pub fn project_monthly_cost_with_card(
        &self,
        model_name: &str,
        daily_input_tokens: usize,
        daily_output_tokens: usize,
        days_per_month: f64,
        card: RateCard,
    ) -> Result<CostProjection, CostError> {
        let daily_cost =
            self.estimate_cost_on(model_name, daily_input_tokens, daily_output_tokens, card)?;
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
    #[error("Unknown rate card: {0}")]
    UnknownRateCard(String),
    #[error("Pricing tier '{tier}' is not available for model '{model}'")]
    TierUnavailable { model: String, tier: String },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_model_id_strips_platform_qualifiers() {
        for (raw, expected) in [
            ("us.amazon.nova-pro-v1:0", "nova-pro"),
            ("us.amazon.nova-micro-v1:0", "nova-micro"),
            ("eu.anthropic.claude-sonnet-4-6", "claude-sonnet-4-6"),
            (
                "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "claude-haiku-4-5",
            ),
            ("amazon.titan-embed-text-v2:0", "titan-embed-text"),
            ("cohere.cohere-embed-v3:0", "cohere-embed"),
            ("gpt-4o", "gpt-4o"),
        ] {
            assert_eq!(CostCalculator::normalize_model_id(raw), expected, "{raw}");
        }
    }

    #[test]
    fn platform_qualified_bedrock_ids_price_via_normalization() {
        let calc = CostCalculator::new();
        let direct = calc.estimate_cost("nova-pro", 100_000, 1_000).unwrap();
        let qualified = calc
            .estimate_cost("us.amazon.nova-pro-v1:0", 100_000, 1_000)
            .unwrap();
        assert_eq!(direct.total_cost, qualified.total_cost);
        // 100k input at 0.8/MTok + 1k output at 3.2/MTok
        assert!((direct.total_cost - (0.08 + 0.0032)).abs() < 1e-9);
    }

    #[test]
    fn nova_and_embedding_models_price_at_bedrock_list_rates() {
        let calc = CostCalculator::new();
        for (model, input_rate, output_rate, input_tokens, output_tokens) in [
            ("nova-premier", 2.5, 12.5, 100_000, 1_000),
            ("nova-pro", 0.8, 3.2, 100_000, 1_000),
            ("nova-lite", 0.06, 0.24, 100_000, 1_000),
            ("nova-micro", 0.035, 0.14, 100_000, 1_000),
            ("titan-embed-text", 0.1, 0.0, 8_000, 0),
            ("cohere-embed", 0.1, 0.0, 8_000, 0),
        ] {
            let est = calc
                .estimate_cost(model, input_tokens, output_tokens)
                .unwrap();
            let expected = (input_tokens as f64) * input_rate / 1_000_000.0
                + (output_tokens as f64) * output_rate / 1_000_000.0;
            assert!(
                (est.total_cost - expected).abs() < 1e-9,
                "{model}: {} vs {expected}",
                est.total_cost
            );
        }
    }

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

        // Test all default models (legacy + modern) can be used for cost estimation
        let test_models = [
            // Legacy
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
            // Modern (2026)
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-opus-4-5",
            "claude-opus-4-1",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
            "claude-haiku-3-5",
            "gpt-5.5",
            "gpt-5.5-pro",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-5.4-pro",
            "gemini-3.5-flash",
            "gemini-3.1-pro",
            "gemini-3.1-flash-lite",
            "gemini-3-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ];

        for model in &test_models {
            let result = calculator.estimate_cost(model, 1000, 500);
            assert!(result.is_ok(), "Model {} should be available", model);
        }
    }

    #[test]
    fn test_rate_card_default_equals_standard() {
        let calc = CostCalculator::new();
        let plain = calc.estimate_cost("claude-opus-4-8", 1000, 500).unwrap();
        let standard = calc
            .estimate_cost_on(
                "claude-opus-4-8",
                1000,
                500,
                RateCard::parse("standard").unwrap(),
            )
            .unwrap();
        let default = calc
            .estimate_cost_on("claude-opus-4-8", 1000, 500, RateCard::default())
            .unwrap();
        assert_eq!(plain.total_cost, standard.total_cost);
        assert_eq!(plain.total_cost, default.total_cost);
        assert_eq!(plain.cache_cost, 0.0);
    }

    #[test]
    fn test_rate_card_batch_is_half() {
        let calc = CostCalculator::new();
        // 500K in / 50K out stays within the 1M window and 64K max-output.
        let standard = calc
            .estimate_cost("claude-opus-4-8", 500_000, 50_000)
            .unwrap();
        let batch = calc
            .estimate_cost_on(
                "claude-opus-4-8",
                500_000,
                50_000,
                RateCard::parse("batch").unwrap(),
            )
            .unwrap();
        assert!((batch.total_cost - 0.5 * standard.total_cost).abs() < 1e-9);
    }

    #[test]
    fn test_rate_card_cache_read_is_tenth_of_input() {
        let calc = CostCalculator::new();
        let usage = TokenUsage {
            input_tokens: 0,
            output_tokens: 1000,
            cache_read_tokens: 100_000,
            ..Default::default()
        };
        let est = calc
            .estimate_cost_with_card("claude-opus-4-8", usage, RateCard::default())
            .unwrap();
        // Cache-read rate (0.0005/1K) is 0.1x the base input rate (0.005/1K).
        let base_input_for_100k = (100_000.0 / 1000.0) * 0.005;
        assert!((est.cache_cost - 0.1 * base_input_for_100k).abs() < 1e-9);
    }

    #[test]
    fn test_rate_card_bedrock_regional_premium() {
        let calc = CostCalculator::new();
        let standard = calc.estimate_cost("claude-opus-4-8", 1000, 500).unwrap();
        let regional = calc
            .estimate_cost_on(
                "claude-opus-4-8",
                1000,
                500,
                RateCard::parse("bedrock:regional").unwrap(),
            )
            .unwrap();
        assert!((regional.total_cost - 1.10 * standard.total_cost).abs() < 1e-9);
    }

    #[test]
    fn test_rate_card_data_residency_premium() {
        let calc = CostCalculator::new();
        let standard = calc.estimate_cost("claude-sonnet-4-6", 1000, 500).unwrap();
        let us = calc
            .estimate_cost_on(
                "claude-sonnet-4-6",
                1000,
                500,
                RateCard::parse("first_party:standard,us").unwrap(),
            )
            .unwrap();
        assert!((us.total_cost - 1.10 * standard.total_cost).abs() < 1e-9);
    }

    #[test]
    fn test_rate_card_fast_mode_override() {
        let calc = CostCalculator::new();
        // opus-4-8 fast-mode rates are 10/50 per MTok (0.010/0.050 per 1K).
        let fast = calc
            .estimate_cost_on(
                "claude-opus-4-8",
                1000,
                1000,
                RateCard::parse("fast").unwrap(),
            )
            .unwrap();
        let expected = (1000.0 / 1000.0) * 0.010 + (1000.0 / 1000.0) * 0.050;
        assert!((fast.total_cost - expected).abs() < 1e-9);
        let standard = calc.estimate_cost("claude-opus-4-8", 1000, 1000).unwrap();
        assert!(fast.total_cost > standard.total_cost);
    }

    #[test]
    fn test_rate_card_long_context_tier() {
        let calc = CostCalculator::new();
        // gemini-2.5-pro: base 1.25/10 per MTok, >200K tier 2.5/15 per MTok.
        let below = calc.estimate_cost("gemini-2.5-pro", 150_000, 1000).unwrap();
        let above = calc.estimate_cost("gemini-2.5-pro", 250_000, 1000).unwrap();
        assert!((below.input_cost - (150_000.0 / 1000.0) * 0.00125).abs() < 1e-9);
        assert!((above.input_cost - (250_000.0 / 1000.0) * 0.0025).abs() < 1e-9);
    }

    #[test]
    fn test_rate_card_priority_and_errors() {
        // Unknown token in a rate card.
        assert!(matches!(
            RateCard::parse("totally-bogus"),
            Err(CostError::UnknownRateCard(_))
        ));

        let calc = CostCalculator::new();
        // gpt-5.5 has a priority multiplier (2.5x); gpt-5.4-pro does not.
        let standard = calc.estimate_cost("gpt-5.5", 1000, 500).unwrap();
        let priority = calc
            .estimate_cost_on("gpt-5.5", 1000, 500, RateCard::parse("priority").unwrap())
            .unwrap();
        assert!((priority.total_cost - 2.5 * standard.total_cost).abs() < 1e-9);

        let unavailable = calc.estimate_cost_on(
            "gpt-5.4-pro",
            1000,
            500,
            RateCard::parse("priority").unwrap(),
        );
        assert!(matches!(
            unavailable,
            Err(CostError::TierUnavailable { .. })
        ));
    }

    #[test]
    fn test_cache_storage_cost() {
        let calc = CostCalculator::new();
        // gemini-2.5-pro storage is $4.50 per MTok per hour.
        let cost = calc
            .estimate_cache_storage_cost("gemini-2.5-pro", 1_000_000, 2.0)
            .unwrap();
        assert!((cost - 9.0).abs() < 1e-9);
        // Models without a storage rate return 0.0 (not an error).
        let none = calc
            .estimate_cache_storage_cost("gpt-4", 1_000_000, 2.0)
            .unwrap();
        assert_eq!(none, 0.0);
    }
}
