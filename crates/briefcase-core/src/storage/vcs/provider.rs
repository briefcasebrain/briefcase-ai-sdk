//! VCS Provider trait and configuration.
//!
//! The `VcsProvider` trait defines a minimal interface for version control systems.
//! Each VCS implementation (DVC, Nessie, Pachyderm, etc.) implements this trait,
//! and the generic `VcsStorageBackend<P>` adapter handles serialization, query
//! filtering, and batching on top.

use super::super::StorageError;
#[cfg(feature = "async")]
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Metadata about an object stored in a VCS backend.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObjectMetadata {
    /// Size in bytes.
    pub size_bytes: u64,
    /// Checksum (SHA256 or provider-native).
    pub checksum: Option<String>,
    /// Unix timestamp of creation.
    pub created_at: Option<i64>,
    /// Unix timestamp of last modification.
    pub modified_at: Option<i64>,
    /// Provider-specific metadata (e.g., ETag, content-type).
    pub custom: HashMap<String, String>,
}

/// Low-level abstraction over version control system operations.
///
/// Each VCS implementation provides concrete methods to read/write objects,
/// create versions (commits), and list contents. The `VcsStorageBackend<P>`
/// generic adapter builds `StorageBackend` semantics on top of these primitives.
///
/// # Implementor Notes
///
/// - `write_object` and `read_object` work with raw bytes (JSON serialization
///   is handled by the adapter layer).
/// - `create_version` should create a commit/tag/checkpoint and return a
///   version identifier (e.g., git SHA, Nessie hash, Pachyderm commit ID).
/// - `list_objects` should return all object paths under a prefix.
/// - Errors should be mapped to `StorageError` variants.
#[cfg(feature = "async")]
#[async_trait]
pub trait VcsProvider: Send + Sync + std::fmt::Debug {
    /// Upload raw bytes to the given path.
    ///
    /// Path format: `"snapshots/{id}.json"` or `"decisions/{id}.json"`.
    /// Overwrites if the path already exists.
    async fn write_object(&self, path: &str, data: &[u8]) -> Result<(), StorageError>;

    /// Download raw bytes from the given path.
    ///
    /// Returns `StorageError::NotFound` if the object does not exist.
    async fn read_object(&self, path: &str) -> Result<Vec<u8>, StorageError>;

    /// List all object paths under a prefix.
    ///
    /// For example, `list_objects("snapshots/")` should return paths like
    /// `["snapshots/abc123.json", "snapshots/def456.json"]`.
    async fn list_objects(&self, prefix: &str) -> Result<Vec<String>, StorageError>;

    /// Delete the object at the given path.
    ///
    /// Returns `true` if deleted, `false` if not found (no error on missing).
    async fn delete_object(&self, path: &str) -> Result<bool, StorageError>;

    /// Create a version (commit/tag/checkpoint) for the current state.
    ///
    /// Returns a version identifier (e.g., git commit SHA, Nessie hash).
    /// The `message` parameter is a human-readable commit message.
    async fn create_version(&self, message: &str) -> Result<String, StorageError>;

    /// Get metadata for a single object.
    ///
    /// Default implementation returns `IoError`; providers override if supported.
    async fn get_object_metadata(&self, _path: &str) -> Result<ObjectMetadata, StorageError> {
        Err(StorageError::IoError(
            "get_object_metadata not supported by this provider".into(),
        ))
    }

    /// Check connectivity and authentication.
    ///
    /// Returns `true` if the backend is reachable and credentials are valid.
    async fn health_check(&self) -> Result<bool, StorageError>;

    /// Human-readable provider name for logging and diagnostics.
    fn provider_name(&self) -> &'static str;

    /// Configuration summary for logging (e.g., `"Nessie: endpoint=..., branch=main"`).
    fn config_summary(&self) -> String;
}

/// Configuration for creating a `VcsProvider` instance.
///
/// Supports loading from environment variables with the pattern
/// `BRIEFCASE_{PROVIDER}_{KEY}` (e.g., `BRIEFCASE_NESSIE_ENDPOINT`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VcsProviderConfig {
    /// Provider type identifier: `"dvc"`, `"nessie"`, `"pachyderm"`, etc.
    pub provider_type: String,

    /// Service endpoint URL.
    pub endpoint: Option<String>,

    /// Authentication access key.
    pub access_key: Option<String>,

    /// Authentication secret key.
    pub secret_key: Option<String>,

    /// Bearer/API token.
    pub token: Option<String>,

    /// Repository or container name.
    pub repository: Option<String>,

    /// Branch or ref name.
    pub branch: Option<String>,

    /// Provider-specific options not covered by the common fields.
    pub extra: HashMap<String, String>,
}

impl VcsProviderConfig {
    /// Create a new empty config for the given provider type.
    pub fn new(provider_type: impl Into<String>) -> Self {
        Self {
            provider_type: provider_type.into(),
            endpoint: None,
            access_key: None,
            secret_key: None,
            token: None,
            repository: None,
            branch: None,
            extra: HashMap::new(),
        }
    }

    /// Load configuration from environment variables.
    ///
    /// Reads variables matching `BRIEFCASE_{PROVIDER}_{KEY}`, for example:
    /// - `BRIEFCASE_NESSIE_ENDPOINT`
    /// - `BRIEFCASE_NESSIE_ACCESS_KEY`
    /// - `BRIEFCASE_NESSIE_PRIVATE_KEY`
    /// - `BRIEFCASE_NESSIE_TOKEN`
    /// - `BRIEFCASE_NESSIE_REPOSITORY`
    /// - `BRIEFCASE_NESSIE_BRANCH`
    ///
    /// Any additional `BRIEFCASE_{PROVIDER}_*` variables are captured in `extra`.
    pub fn from_env(provider_type: &str) -> Self {
        let prefix = format!("BRIEFCASE_{}", provider_type.to_uppercase());

        let endpoint = std::env::var(format!("{}_ENDPOINT", prefix)).ok();
        let access_key = std::env::var(format!("{}_ACCESS_KEY", prefix)).ok();
        let secret_key = std::env::var(format!("{}_PRIVATE_KEY", prefix)).ok();
        let token = std::env::var(format!("{}_TOKEN", prefix)).ok();
        let repository = std::env::var(format!("{}_REPOSITORY", prefix)).ok();
        let branch = std::env::var(format!("{}_BRANCH", prefix)).ok();

        let standard_suffixes = [
            "ENDPOINT",
            "ACCESS_KEY",
            "PRIVATE_KEY",
            "TOKEN",
            "REPOSITORY",
            "BRANCH",
        ];

        let mut extra = HashMap::new();
        let prefix_with_underscore = format!("{}_", prefix);
        for (key, value) in std::env::vars() {
            if let Some(suffix) = key.strip_prefix(&prefix_with_underscore) {
                if !standard_suffixes.contains(&suffix) {
                    extra.insert(suffix.to_lowercase(), value);
                }
            }
        }

        Self {
            provider_type: provider_type.to_string(),
            endpoint,
            access_key,
            secret_key,
            token,
            repository,
            branch,
            extra,
        }
    }

    /// Builder: set endpoint.
    pub fn with_endpoint(mut self, endpoint: impl Into<String>) -> Self {
        self.endpoint = Some(endpoint.into());
        self
    }

    /// Builder: set access key.
    pub fn with_access_key(mut self, key: impl Into<String>) -> Self {
        self.access_key = Some(key.into());
        self
    }

    /// Builder: set secret key.
    pub fn with_secret_key(mut self, key: impl Into<String>) -> Self {
        self.secret_key = Some(key.into());
        self
    }

    /// Builder: set token.
    pub fn with_token(mut self, token: impl Into<String>) -> Self {
        self.token = Some(token.into());
        self
    }

    /// Builder: set repository.
    pub fn with_repository(mut self, repo: impl Into<String>) -> Self {
        self.repository = Some(repo.into());
        self
    }

    /// Builder: set branch.
    pub fn with_branch(mut self, branch: impl Into<String>) -> Self {
        self.branch = Some(branch.into());
        self
    }

    /// Builder: set a provider-specific extra option.
    pub fn with_extra(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.extra.insert(key.into(), value.into());
        self
    }
}

#[cfg(feature = "async")]
#[async_trait]
impl VcsProvider for Box<dyn VcsProvider> {
    async fn write_object(&self, path: &str, data: &[u8]) -> Result<(), StorageError> {
        (**self).write_object(path, data).await
    }

    async fn read_object(&self, path: &str) -> Result<Vec<u8>, StorageError> {
        (**self).read_object(path).await
    }

    async fn list_objects(&self, prefix: &str) -> Result<Vec<String>, StorageError> {
        (**self).list_objects(prefix).await
    }

    async fn delete_object(&self, path: &str) -> Result<bool, StorageError> {
        (**self).delete_object(path).await
    }

    async fn create_version(&self, message: &str) -> Result<String, StorageError> {
        (**self).create_version(message).await
    }

    async fn get_object_metadata(&self, path: &str) -> Result<ObjectMetadata, StorageError> {
        (**self).get_object_metadata(path).await
    }

    async fn health_check(&self) -> Result<bool, StorageError> {
        (**self).health_check().await
    }

    fn provider_name(&self) -> &'static str {
        (**self).provider_name()
    }

    fn config_summary(&self) -> String {
        (**self).config_summary()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_config_new() {
        let config = VcsProviderConfig::new("nessie");
        assert_eq!(config.provider_type, "nessie");
        assert!(config.endpoint.is_none());
        assert!(config.extra.is_empty());
    }

    #[test]
    fn test_config_builder() {
        let config = VcsProviderConfig::new("dvc")
            .with_endpoint("https://dvc.example.com")
            .with_repository("my-repo")
            .with_branch("main")
            .with_extra("remote_name", "myremote");

        assert_eq!(config.provider_type, "dvc");
        assert_eq!(config.endpoint.unwrap(), "https://dvc.example.com");
        assert_eq!(config.repository.unwrap(), "my-repo");
        assert_eq!(config.branch.unwrap(), "main");
        assert_eq!(config.extra.get("remote_name").unwrap(), "myremote");
    }

    #[test]
    fn test_config_from_env() {
        // Set env vars for test
        std::env::set_var("BRIEFCASE_TESTPROV_ENDPOINT", "https://test.example.com");
        std::env::set_var("BRIEFCASE_TESTPROV_REPOSITORY", "test-repo");
        std::env::set_var("BRIEFCASE_TESTPROV_CUSTOM_FIELD", "custom_value");

        let config = VcsProviderConfig::from_env("testprov");

        assert_eq!(config.provider_type, "testprov");
        assert_eq!(config.endpoint.unwrap(), "https://test.example.com");
        assert_eq!(config.repository.unwrap(), "test-repo");
        assert_eq!(config.extra.get("custom_field").unwrap(), "custom_value");

        // Clean up
        std::env::remove_var("BRIEFCASE_TESTPROV_ENDPOINT");
        std::env::remove_var("BRIEFCASE_TESTPROV_REPOSITORY");
        std::env::remove_var("BRIEFCASE_TESTPROV_CUSTOM_FIELD");
    }
}
