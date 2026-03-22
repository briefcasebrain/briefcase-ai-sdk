//! Version Control System (VCS) storage provider abstraction.
//!
//! This module provides a modular architecture for integrating multiple version
//! control systems as `StorageBackend` implementations. A two-layer design
//! separates concerns:
//!
//! 1. **`VcsProvider` trait** — Simple interface for VCS-specific operations
//!    (read/write objects, create versions, health checks).
//!
//! 2. **`VcsStorageBackend<P: VcsProvider>`** — Generic adapter that implements
//!    `StorageBackend` on top of any `VcsProvider`, handling JSON serialization,
//!    query filtering, path management, and batch flush semantics.
//!
//! ## Adding a New Provider
//!
//! 1. Implement the `VcsProvider` trait (~150-250 lines)
//! 2. Wrap it with `VcsStorageBackend::new(provider)`
//! 3. The adapter provides the full `StorageBackend` implementation

pub mod adapter;
pub mod provider;

// Re-export core types
pub use adapter::VcsStorageBackend;
pub use provider::{ObjectMetadata, VcsProvider, VcsProviderConfig};

use super::StorageError;

/// Create a `VcsProvider` instance from configuration.
///
/// Dispatches to the appropriate provider constructor based on `config.provider_type`.
/// Provider implementations are available in the enterprise package.
pub async fn create_vcs_provider(
    config: VcsProviderConfig,
) -> Result<Box<dyn VcsProvider>, StorageError> {
    Err(StorageError::ConnectionError(format!(
        "Unknown or disabled VCS provider: '{}'. \
         Provider implementations are available in briefcase-ai-enterprise.",
        config.provider_type
    )))
}

/// Create a `VcsStorageBackend` from configuration.
pub async fn create_vcs_backend(
    config: VcsProviderConfig,
) -> Result<VcsStorageBackend<Box<dyn VcsProvider>>, StorageError> {
    let provider = create_vcs_provider(config).await?;
    Ok(VcsStorageBackend::new(provider))
}

/// Create a `VcsStorageBackend` from environment variables.
pub async fn create_vcs_backend_from_env(
    provider_type: &str,
) -> Result<VcsStorageBackend<Box<dyn VcsProvider>>, StorageError> {
    let config = VcsProviderConfig::from_env(provider_type);
    create_vcs_backend(config).await
}

/// List all VCS provider types that are compiled into this build.
pub fn available_providers() -> Vec<&'static str> {
    Vec::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_available_providers_returns_list() {
        let providers = available_providers();
        assert!(providers.is_empty());
    }

    #[tokio::test]
    async fn test_unknown_provider_returns_error() {
        let config = VcsProviderConfig::new("nonexistent_vcs");
        let result = create_vcs_provider(config).await;
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(matches!(err, StorageError::ConnectionError(_)));
    }
}
