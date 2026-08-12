//! # Control Plane Abstraction
//!
//! Defines the `ControlPlane` trait for looking up client authentication and
//! authorization records.  Implementations include:
//!
//! - **`RemoteControlPlane`**  reads client records from a remote control
//!   repository (the production source of truth).
//! - **`LocalControlPlane`**  resolves clients from a static in-memory list
//!   (useful for dev/test or as a fallback).
//! - **`FallbackControlPlane`**  chains a *primary* and *secondary* control
//!   plane: the primary is tried first and, on any error, the secondary is
//!   consulted.  This lets you run the remote backend as the primary with
//!   local config as the fallback.
//!
//! ## Client record schema
//!
//! Records live at `clients/by-key/{sha256(api_key)}.json` in the control
//! repository.  The JSON schema is defined by [`ClientRecord`].
//!
//! ## Extending
//!
//! To add a new backend (e.g. PostgreSQL, Redis, Vault), implement the
//! `ControlPlane` trait and wire it into the server's `AppState`.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use thiserror::Error;

#[cfg(feature = "async")]
use async_trait::async_trait;

//  Errors

#[derive(Error, Debug)]
pub enum ControlPlaneError {
    #[error("Client not found for the provided API key")]
    NotFound,

    #[error("Client record is inactive (disabled)")]
    Inactive,

    #[error("Backend unreachable: {0}")]
    Unreachable(String),

    #[error("Malformed client record: {0}")]
    MalformedRecord(String),

    #[error("Internal error: {0}")]
    Internal(String),
}

//  Client record schema

/// Canonical client record stored in the control plane.
///
/// In the remote backend this is the JSON payload at
/// `clients/by-key/{sha256(api_key)}.json`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClientRecord {
    /// Unique human-readable identifier (e.g. "acme", "internal-tools").
    pub client_id: String,

    /// Whether the client is currently active.  Inactive clients fail auth
    /// even if the key matches.
    #[serde(default = "default_active")]
    pub is_active: bool,

    /// Granted permission strings (e.g. `["read", "write", "replay", "delete"]`).
    #[serde(default = "default_permissions")]
    pub permissions: Vec<String>,

    /// Optional per-client rate limit (requests per second).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rate_limit_rps: Option<u32>,

    /// Arbitrary key-value metadata.
    #[serde(default)]
    pub metadata: HashMap<String, String>,
}

fn default_active() -> bool {
    true
}

fn default_permissions() -> Vec<String> {
    vec![
        "read".into(),
        "write".into(),
        "replay".into(),
        "delete".into(),
    ]
}

//  Trait

/// Abstraction over the source of truth for client authentication and
/// authorization.
///
/// Implementors resolve an API key to a [`ClientRecord`].  The server calls
/// this on the `POST /api/v1/auth/validate` path and, optionally, in the
/// per-request middleware.
#[cfg(feature = "async")]
#[async_trait]
pub trait ControlPlane: Send + Sync {
    /// Look up a client by raw API key.
    ///
    /// Implementations MUST:
    /// 1. Hash the key (SHA-256) before any network/storage lookup.
    /// 2. Return `Err(NotFound)` when no record matches.
    /// 3. Return `Err(Inactive)` when the record exists but `is_active` is
    ///    false.
    async fn lookup_client(&self, api_key: &str) -> Result<ClientRecord, ControlPlaneError>;

    /// Quick connectivity check.  Returns `true` if the backend is reachable.
    async fn health_check(&self) -> bool {
        true
    }

    /// Human-readable backend name for logging (e.g. "remote", "local").
    fn backend_name(&self) -> &str;
}

//  Helpers

/// SHA-256 hash of an API key, returned as a lowercase hex string.
pub fn hash_api_key(api_key: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(api_key.as_bytes());
    let result = hasher.finalize();
    result.iter().map(|b| format!("{:02x}", b)).collect()
}

//  Local (in-memory) implementation

/// Resolves clients from a static list  typically parsed from env vars.
///
/// This is the simplest control plane: no network calls, no hashing lookup.
/// It uses constant-time comparison for security.
pub struct LocalControlPlane {
    clients: Vec<LocalClient>,
}

/// A client entry for local (in-memory) lookup.
#[derive(Clone, Debug)]
pub struct LocalClient {
    pub client_id: String,
    pub api_key: String,
    pub permissions: Vec<String>,
    pub rate_limit_rps: Option<u32>,
}

impl LocalControlPlane {
    /// Create from a list of client entries.
    pub fn new(clients: Vec<LocalClient>) -> Self {
        Self { clients }
    }

    /// Constant-time string comparison.
    fn constant_time_eq(a: &str, b: &str) -> bool {
        if a.len() != b.len() {
            return false;
        }
        a.as_bytes()
            .iter()
            .zip(b.as_bytes().iter())
            .fold(0u8, |acc, (x, y)| acc | (x ^ y))
            == 0
    }
}

#[cfg(feature = "async")]
#[async_trait]
impl ControlPlane for LocalControlPlane {
    async fn lookup_client(&self, api_key: &str) -> Result<ClientRecord, ControlPlaneError> {
        for client in &self.clients {
            if Self::constant_time_eq(&client.api_key, api_key) {
                return Ok(ClientRecord {
                    client_id: client.client_id.clone(),
                    is_active: true,
                    permissions: client.permissions.clone(),
                    rate_limit_rps: client.rate_limit_rps,
                    metadata: HashMap::new(),
                });
            }
        }
        Err(ControlPlaneError::NotFound)
    }

    fn backend_name(&self) -> &str {
        "local"
    }
}

//  Fallback (chained) implementation

/// Chains a primary and secondary control plane.
///
/// On `lookup_client`, the primary is tried first.  If it returns `NotFound`
/// or `Unreachable`, the secondary is consulted.  Errors like `Inactive` or
/// `MalformedRecord` from the primary are NOT retried  those indicate the
/// record *was* found but is invalid.
#[cfg(feature = "async")]
pub struct FallbackControlPlane {
    primary: Box<dyn ControlPlane>,
    secondary: Box<dyn ControlPlane>,
}

#[cfg(feature = "async")]
impl FallbackControlPlane {
    pub fn new(primary: Box<dyn ControlPlane>, secondary: Box<dyn ControlPlane>) -> Self {
        Self { primary, secondary }
    }
}

#[cfg(feature = "async")]
#[async_trait]
impl ControlPlane for FallbackControlPlane {
    async fn lookup_client(&self, api_key: &str) -> Result<ClientRecord, ControlPlaneError> {
        match self.primary.lookup_client(api_key).await {
            Ok(record) => Ok(record),
            // Record found but inactive  don't retry, this is authoritative
            Err(ControlPlaneError::Inactive) => Err(ControlPlaneError::Inactive),
            // Record found but malformed  don't retry
            Err(ControlPlaneError::MalformedRecord(msg)) => {
                Err(ControlPlaneError::MalformedRecord(msg))
            }
            // Not found or unreachable  try secondary
            Err(_primary_err) => self.secondary.lookup_client(api_key).await,
        }
    }

    async fn health_check(&self) -> bool {
        // Healthy if either backend is reachable
        self.primary.health_check().await || self.secondary.health_check().await
    }

    fn backend_name(&self) -> &str {
        "fallback"
    }
}

//  Tests

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hash_api_key_deterministic() {
        let h1 = hash_api_key("sk-test-key-123");
        let h2 = hash_api_key("sk-test-key-123");
        assert_eq!(h1, h2);
    }

    #[test]
    fn test_hash_api_key_different_keys_different_hashes() {
        let h1 = hash_api_key("sk-key-a");
        let h2 = hash_api_key("sk-key-b");
        assert_ne!(h1, h2);
    }

    #[test]
    fn test_hash_api_key_is_hex() {
        let h = hash_api_key("sk-test");
        assert_eq!(h.len(), 64); // SHA-256 = 32 bytes = 64 hex chars
        assert!(h.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn test_hash_api_key_known_value() {
        // SHA-256("hello") = 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
        let h = hash_api_key("hello");
        assert_eq!(
            h,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
    }

    #[test]
    fn test_client_record_deserialize_minimal() {
        let json = r#"{"client_id": "acme"}"#;
        let record: ClientRecord = serde_json::from_str(json).unwrap();
        assert_eq!(record.client_id, "acme");
        assert!(record.is_active); // default
        assert_eq!(record.permissions.len(), 4); // defaults
        assert!(record.rate_limit_rps.is_none());
        assert!(record.metadata.is_empty());
    }

    #[test]
    fn test_client_record_deserialize_full() {
        let json = r#"{
            "client_id": "acme",
            "is_active": false,
            "permissions": ["read"],
            "rate_limit_rps": 50,
            "metadata": {"tier": "enterprise"}
        }"#;
        let record: ClientRecord = serde_json::from_str(json).unwrap();
        assert_eq!(record.client_id, "acme");
        assert!(!record.is_active);
        assert_eq!(record.permissions, vec!["read"]);
        assert_eq!(record.rate_limit_rps, Some(50));
        assert_eq!(record.metadata.get("tier"), Some(&"enterprise".to_string()));
    }

    #[test]
    fn test_client_record_serialize_roundtrip() {
        let record = ClientRecord {
            client_id: "test".into(),
            is_active: true,
            permissions: vec!["read".into(), "write".into()],
            rate_limit_rps: Some(100),
            metadata: HashMap::new(),
        };
        let json = serde_json::to_string(&record).unwrap();
        let back: ClientRecord = serde_json::from_str(&json).unwrap();
        assert_eq!(back.client_id, "test");
        assert_eq!(back.permissions, vec!["read", "write"]);
        assert_eq!(back.rate_limit_rps, Some(100));
    }

    #[test]
    fn test_client_record_rate_limit_omitted_in_json() {
        let record = ClientRecord {
            client_id: "test".into(),
            is_active: true,
            permissions: vec![],
            rate_limit_rps: None,
            metadata: HashMap::new(),
        };
        let json = serde_json::to_string(&record).unwrap();
        assert!(!json.contains("rate_limit_rps"));
    }

    #[test]
    fn test_client_record_missing_client_id_fails() {
        let json = r#"{"permissions": ["read"]}"#;
        let result: Result<ClientRecord, _> = serde_json::from_str(json);
        assert!(result.is_err());
    }

    // --- LocalControlPlane tests ---

    #[tokio::test]
    async fn test_local_lookup_found() {
        let cp = LocalControlPlane::new(vec![LocalClient {
            client_id: "acme".into(),
            api_key: "sk-acme-123".into(),
            permissions: vec!["read".into(), "write".into()],
            rate_limit_rps: Some(100),
        }]);
        let record = cp.lookup_client("sk-acme-123").await.unwrap();
        assert_eq!(record.client_id, "acme");
        assert!(record.is_active);
        assert_eq!(record.permissions, vec!["read", "write"]);
        assert_eq!(record.rate_limit_rps, Some(100));
    }

    #[tokio::test]
    async fn test_local_lookup_not_found() {
        let cp = LocalControlPlane::new(vec![LocalClient {
            client_id: "acme".into(),
            api_key: "sk-acme-123".into(),
            permissions: vec![],
            rate_limit_rps: None,
        }]);
        let result = cp.lookup_client("sk-wrong").await;
        assert!(matches!(result, Err(ControlPlaneError::NotFound)));
    }

    #[tokio::test]
    async fn test_local_lookup_empty_list() {
        let cp = LocalControlPlane::new(vec![]);
        let result = cp.lookup_client("sk-anything").await;
        assert!(matches!(result, Err(ControlPlaneError::NotFound)));
    }

    #[tokio::test]
    async fn test_local_lookup_multiple_clients() {
        let cp = LocalControlPlane::new(vec![
            LocalClient {
                client_id: "acme".into(),
                api_key: "sk-acme".into(),
                permissions: vec!["read".into()],
                rate_limit_rps: None,
            },
            LocalClient {
                client_id: "beta".into(),
                api_key: "sk-beta".into(),
                permissions: vec!["write".into()],
                rate_limit_rps: None,
            },
        ]);
        let r1 = cp.lookup_client("sk-acme").await.unwrap();
        assert_eq!(r1.client_id, "acme");

        let r2 = cp.lookup_client("sk-beta").await.unwrap();
        assert_eq!(r2.client_id, "beta");
    }

    #[tokio::test]
    async fn test_local_constant_time_prevents_substring_match() {
        let cp = LocalControlPlane::new(vec![LocalClient {
            client_id: "acme".into(),
            api_key: "sk-acme-123".into(),
            permissions: vec![],
            rate_limit_rps: None,
        }]);
        // Substring should NOT match
        assert!(cp.lookup_client("sk-acme").await.is_err());
        assert!(cp.lookup_client("sk-acme-1234").await.is_err());
        assert!(cp.lookup_client("sk-acme-12").await.is_err());
    }

    #[tokio::test]
    async fn test_local_backend_name() {
        let cp = LocalControlPlane::new(vec![]);
        assert_eq!(cp.backend_name(), "local");
    }

    #[tokio::test]
    async fn test_local_health_check() {
        let cp = LocalControlPlane::new(vec![]);
        assert!(cp.health_check().await);
    }

    // --- FallbackControlPlane tests ---

    /// A mock control plane that always returns a fixed record.
    struct MockControlPlane {
        name: &'static str,
        record: Option<ClientRecord>,
        error: Option<ControlPlaneError>,
    }

    impl MockControlPlane {
        fn succeeding(name: &'static str, client_id: &str) -> Self {
            Self {
                name,
                record: Some(ClientRecord {
                    client_id: client_id.into(),
                    is_active: true,
                    permissions: vec!["read".into()],
                    rate_limit_rps: None,
                    metadata: HashMap::new(),
                }),
                error: None,
            }
        }

        fn failing(name: &'static str, error: ControlPlaneError) -> Self {
            Self {
                name,
                record: None,
                error: Some(error),
            }
        }
    }

    #[async_trait]
    impl ControlPlane for MockControlPlane {
        async fn lookup_client(&self, _api_key: &str) -> Result<ClientRecord, ControlPlaneError> {
            if let Some(ref record) = self.record {
                Ok(record.clone())
            } else if let Some(ref err) = self.error {
                // Recreate the error since Error doesn't impl Clone
                match err {
                    ControlPlaneError::NotFound => Err(ControlPlaneError::NotFound),
                    ControlPlaneError::Inactive => Err(ControlPlaneError::Inactive),
                    ControlPlaneError::Unreachable(m) => {
                        Err(ControlPlaneError::Unreachable(m.clone()))
                    }
                    ControlPlaneError::MalformedRecord(m) => {
                        Err(ControlPlaneError::MalformedRecord(m.clone()))
                    }
                    ControlPlaneError::Internal(m) => Err(ControlPlaneError::Internal(m.clone())),
                }
            } else {
                Err(ControlPlaneError::NotFound)
            }
        }

        fn backend_name(&self) -> &str {
            self.name
        }
    }

    #[tokio::test]
    async fn test_fallback_primary_succeeds() {
        let primary = MockControlPlane::succeeding("primary", "from-primary");
        let secondary = MockControlPlane::succeeding("secondary", "from-secondary");
        let cp = FallbackControlPlane::new(Box::new(primary), Box::new(secondary));

        let record = cp.lookup_client("any-key").await.unwrap();
        assert_eq!(record.client_id, "from-primary");
    }

    #[tokio::test]
    async fn test_fallback_primary_not_found_falls_through() {
        let primary = MockControlPlane::failing("primary", ControlPlaneError::NotFound);
        let secondary = MockControlPlane::succeeding("secondary", "from-secondary");
        let cp = FallbackControlPlane::new(Box::new(primary), Box::new(secondary));

        let record = cp.lookup_client("any-key").await.unwrap();
        assert_eq!(record.client_id, "from-secondary");
    }

    #[tokio::test]
    async fn test_fallback_primary_unreachable_falls_through() {
        let primary =
            MockControlPlane::failing("primary", ControlPlaneError::Unreachable("timeout".into()));
        let secondary = MockControlPlane::succeeding("secondary", "from-secondary");
        let cp = FallbackControlPlane::new(Box::new(primary), Box::new(secondary));

        let record = cp.lookup_client("any-key").await.unwrap();
        assert_eq!(record.client_id, "from-secondary");
    }

    #[tokio::test]
    async fn test_fallback_primary_inactive_does_not_fall_through() {
        let primary = MockControlPlane::failing("primary", ControlPlaneError::Inactive);
        let secondary = MockControlPlane::succeeding("secondary", "from-secondary");
        let cp = FallbackControlPlane::new(Box::new(primary), Box::new(secondary));

        let result = cp.lookup_client("any-key").await;
        assert!(matches!(result, Err(ControlPlaneError::Inactive)));
    }

    #[tokio::test]
    async fn test_fallback_primary_malformed_does_not_fall_through() {
        let primary = MockControlPlane::failing(
            "primary",
            ControlPlaneError::MalformedRecord("bad json".into()),
        );
        let secondary = MockControlPlane::succeeding("secondary", "from-secondary");
        let cp = FallbackControlPlane::new(Box::new(primary), Box::new(secondary));

        let result = cp.lookup_client("any-key").await;
        assert!(matches!(result, Err(ControlPlaneError::MalformedRecord(_))));
    }

    #[tokio::test]
    async fn test_fallback_both_not_found() {
        let primary = MockControlPlane::failing("primary", ControlPlaneError::NotFound);
        let secondary = MockControlPlane::failing("secondary", ControlPlaneError::NotFound);
        let cp = FallbackControlPlane::new(Box::new(primary), Box::new(secondary));

        let result = cp.lookup_client("any-key").await;
        assert!(matches!(result, Err(ControlPlaneError::NotFound)));
    }

    #[tokio::test]
    async fn test_fallback_backend_name() {
        let primary = MockControlPlane::succeeding("primary", "x");
        let secondary = MockControlPlane::succeeding("secondary", "y");
        let cp = FallbackControlPlane::new(Box::new(primary), Box::new(secondary));
        assert_eq!(cp.backend_name(), "fallback");
    }
}
