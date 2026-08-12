//! # Briefcase AI Core Library
//!
//! High-performance AI observability, replay, and decision tracking for Rust applications.
//!
//! ## Features
//!
//! - **AI Decision Tracking**: Capture inputs, outputs, and context for every AI decision
//! - **Deterministic Replay**: Reproduce AI decisions exactly with full context preservation
//! - **Comprehensive Observability**: Monitor model performance, drift, and behavior
//! - **Enterprise Security**: Built-in data sanitization and privacy controls
//! - **Flexible Storage**: SQLite, cloud storage, and custom backends
//! - **Cost Management**: Track and optimize AI model usage costs
//! - **Cross-Platform**: Works on Linux, macOS, Windows, and WebAssembly
//!
//! ## Quick Start
//!
//! ```rust
//! use briefcase_core::*;
//! use briefcase_core::storage::StorageBackend;
//! use serde_json::json;
//!
//! # #[tokio::main]
//! # async fn main() -> Result<(), Box<dyn std::error::Error>> {
//! // Create a decision snapshot
//! let decision = DecisionSnapshot::new("ai_function")
//!     .add_input(Input::new("user_query", json!("Hello world"), "string"))
//!     .add_output(Output::new("response", json!("Hello back!"), "string").with_confidence(0.95))
//!     .with_execution_time(120.5);
//!
//! // Save to storage (requires storage feature)
//! #[cfg(feature = "sqlite-storage")]
//! {
//!     let storage = storage::SqliteBackend::in_memory()?;
//!     let decision_id = storage.save_decision(&decision).await?;
//!     println!("Saved decision: {}", decision_id);
//! }
//! # Ok(())
//! # }
//! ```
//!
//! ## Feature Flags
//!
//! - `recording` - Baseline decision capture pipeline (enabled by default)
//! - `async` - Enable async/await runtime support (enabled by default)
//! - `storage` - Enable persistent storage APIs (enabled by default)
//! - `sqlite-storage` - SQLite storage backend (implied by `storage`)
//! - `vcs-storage` - Version-control storage abstractions
//! - `replay` - Deterministic replay engine
//! - `drift` - Drift monitoring utilities
//! - `sanitize` - Built-in PII sanitization
//! - `otel` - OpenTelemetry instrumentation helpers
//! - `tokens` - Token counting utilities
//! - `networking` - HTTP client support for remote services
//! - `compression` - Compression helpers for storage backends

/// Cost calculation and management functionality
#[cfg(feature = "drift")]
pub mod cost;

/// Drift detection and model performance monitoring
#[cfg(feature = "drift")]
pub mod drift;

/// Core data models and structures
pub mod models;

/// Token counting and estimation
#[cfg(feature = "tokens")]
pub mod tokens;

/// Telemetry helpers for reporting usage
#[cfg(feature = "otel")]
pub mod telemetry;

/// The default base URL for the Briefcase AI API.
pub const DEFAULT_API_URL: &str = "http://localhost:8080";

/// Decision replay and validation
#[cfg(feature = "replay")]
pub mod replay;

/// Data sanitization and privacy controls
#[cfg(feature = "sanitize")]
pub mod sanitization;

// Storage module only available with storage features
#[cfg(any(feature = "sqlite-storage", feature = "vcs-storage"))]
/// Storage backends for persisting decisions and snapshots
pub mod storage;

// Control plane abstraction for auth/authz backends (remote, local, etc.)
#[cfg(feature = "async")]
/// Control plane trait and implementations for client lookup
pub mod control;

// Client authentication module (requires networking for server validation)
#[cfg(feature = "networking")]
/// Unified client for authenticated access to the Briefcase AI platform
pub mod client;

// Re-export all public types for convenience
#[cfg(feature = "drift")]
pub use cost::*;
#[cfg(feature = "drift")]
pub use drift::*;
pub use models::*;
#[cfg(feature = "replay")]
pub use replay::*;
#[cfg(feature = "sanitize")]
pub use sanitization::*;
#[cfg(feature = "tokens")]
pub use tokens::*;

use thiserror::Error;

#[derive(Error, Debug)]
pub enum BriefcaseError {
    #[cfg(any(feature = "sqlite-storage", feature = "vcs-storage"))]
    #[error("Storage error: {0}")]
    Storage(#[from] storage::StorageError),

    #[cfg(feature = "replay")]
    #[error("Replay error: {0}")]
    Replay(#[from] replay::ReplayError),

    #[cfg(feature = "drift")]
    #[error("Cost calculation error: {0}")]
    Cost(#[from] cost::CostError),

    #[cfg(feature = "sanitize")]
    #[error("Sanitization error: {0}")]
    Sanitization(#[from] sanitization::SanitizationError),

    #[error("Serialization error: {0}")]
    Serialization(#[from] serde_json::Error),

    #[error("Invalid input: {0}")]
    InvalidInput(String),

    #[cfg(feature = "networking")]
    #[error("Client error: {0}")]
    Client(#[from] client::ClientError),
}
