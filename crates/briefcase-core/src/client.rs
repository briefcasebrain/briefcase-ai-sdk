//! Unified BriefcaseClient for authenticated access to the Briefcase AI platform.
//!
//! The `BriefcaseClient` validates an API key against the Briefcase server,
//! caches the result, and provides permission-gated access to storage and
//! other SDK features. Plain http server URLs are accepted only for loopback
//! hosts; remote servers require https unless
//! [`ClientConfig::allow_insecure_http`] is set.
//!
//! # Example
//!
//! ```rust,no_run
//! use briefcase_core::client::BriefcaseClient;
//!
//! # #[tokio::main]
//! # async fn main() -> Result<(), Box<dyn std::error::Error>> {
//! let client = BriefcaseClient::new("sk-my-key", "https://briefcase.example.com").await?;
//! println!("Authenticated as: {}", client.client_id());
//! assert!(client.has_permission("read"));
//! # Ok(())
//! # }
//! ```

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use thiserror::Error;

#[cfg(feature = "sqlite-storage")]
use crate::models::{DecisionSnapshot, Snapshot};
#[cfg(feature = "sqlite-storage")]
use crate::storage::{SnapshotQuery, StorageBackend, StorageError};

//  Data Structures

/// Information about a validated client returned by the server.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ValidatedClient {
    pub client_id: String,
    pub permissions: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rate_limit_rps: Option<u32>,
    #[serde(default)]
    pub metadata: HashMap<String, String>,
}

/// Full response from the `POST /api/v1/auth/validate` endpoint.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthResponse {
    pub valid: bool,
    pub client: ValidatedClient,
    pub expires_at: DateTime<Utc>,
}

/// Configuration for the client's HTTP behaviour and cache policy.
///
/// Non-exhaustive: construct via `ClientConfig::default()` and set fields, so
/// future additions are not breaking changes.
#[derive(Debug, Clone)]
#[non_exhaustive]
pub struct ClientConfig {
    /// HTTP request timeout in seconds (default: 30).
    pub timeout_secs: u64,
    /// How long to cache a successful validation (default: 3600 = 1 hour).
    pub cache_ttl_secs: u64,
    /// Maximum number of HTTP retries on transient errors (default: 3).
    pub max_retries: u32,
    /// Allow plain http to non-loopback hosts (default: false). The API key
    /// travels in the request body, so enabling this sends it in cleartext.
    pub allow_insecure_http: bool,
}

impl Default for ClientConfig {
    fn default() -> Self {
        Self {
            timeout_secs: 30,
            cache_ttl_secs: 3600,
            max_retries: 3,
            allow_insecure_http: false,
        }
    }
}

/// Errors that can occur during client operations.
#[derive(Error, Debug)]
pub enum ClientError {
    #[error("Authentication failed: {0}")]
    AuthFailed(String),

    #[error("Server unreachable: {0}")]
    ServerUnreachable(String),

    #[error("Permission denied: requires '{0}'")]
    PermissionDenied(String),

    #[error("Validation expired")]
    Expired,

    #[error("No storage backend bound")]
    NoStorage,

    #[error("Invalid argument: {0}")]
    InvalidArgument(String),

    #[cfg(feature = "sqlite-storage")]
    #[error("Storage error: {0}")]
    Storage(#[from] StorageError),
}

/// True for hosts that resolve to the local machine: "localhost" or a
/// loopback IP (127.0.0.0/8, ::1). IPv6 hosts arrive bracketed from the URL
/// parser and are unbracketed before parsing.
fn is_loopback_host(host: &str) -> bool {
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    let bare = host
        .strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host);
    bare.parse::<std::net::IpAddr>()
        .is_ok_and(|ip| ip.is_loopback())
}

//  BriefcaseClient

/// Cached validation entry.
struct CacheEntry {
    client: ValidatedClient,
    cached_at: Instant,
}

/// Authenticated client for the Briefcase AI platform.
///
/// Created via [`BriefcaseClient::new`] or [`BriefcaseClient::with_config`],
/// which validate the API key against the server before returning.
pub struct BriefcaseClient {
    validated: ValidatedClient,
    server_url: String,
    api_key: String,
    http: reqwest::Client,
    cache: Arc<Mutex<Option<CacheEntry>>>,
    cache_ttl: Duration,
    #[cfg(feature = "sqlite-storage")]
    storage: Option<Arc<dyn StorageBackend>>,
}

impl std::fmt::Debug for BriefcaseClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BriefcaseClient")
            .field("validated", &self.validated)
            .field("server_url", &self.server_url)
            .field("api_key", &"[REDACTED]")
            .field("cache_ttl", &self.cache_ttl)
            .finish()
    }
}

impl BriefcaseClient {
    /// Create a new client, validating the API key against the server.
    ///
    /// Uses default configuration (30s timeout, 1h cache TTL, 3 retries).
    pub async fn new(api_key: &str, server_url: &str) -> Result<Self, ClientError> {
        Self::with_config(api_key, server_url, ClientConfig::default()).await
    }

    /// Create a new client with custom configuration.
    pub async fn with_config(
        api_key: &str,
        server_url: &str,
        config: ClientConfig,
    ) -> Result<Self, ClientError> {
        // Validate inputs
        if api_key.trim().is_empty() {
            return Err(ClientError::InvalidArgument(
                "API key must not be empty".into(),
            ));
        }
        if server_url.trim().is_empty() {
            return Err(ClientError::InvalidArgument(
                "Server URL must not be empty".into(),
            ));
        }
        // The API key travels in the request body, so plain http is only
        // accepted for loopback hosts unless the caller opts in explicitly.
        if !config.allow_insecure_http {
            if let Ok(parsed) = reqwest::Url::parse(server_url.trim()) {
                if parsed.scheme() == "http" && !parsed.host_str().is_some_and(is_loopback_host) {
                    return Err(ClientError::InvalidArgument(
                        "Server URL uses http with a non-loopback host; the API key would be sent in cleartext. Use https, or set ClientConfig.allow_insecure_http.".into(),
                    ));
                }
            }
        }

        // Redirects re-send the key-bearing POST body on 307/308, so the same
        // loopback-or-opt-in rule applies to every hop, not just the
        // configured URL.
        let redirect_policy = if config.allow_insecure_http {
            reqwest::redirect::Policy::default()
        } else {
            reqwest::redirect::Policy::custom(|attempt| {
                if attempt.previous().len() >= 10 {
                    return attempt.error("too many redirects");
                }
                let next = attempt.url();
                if next.scheme() == "http" && !next.host_str().is_some_and(is_loopback_host) {
                    return attempt.error(
                        "refusing redirect to plain http on a non-loopback host; the API key would be sent in cleartext",
                    );
                }
                attempt.follow()
            })
        };

        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(config.timeout_secs))
            .redirect(redirect_policy)
            .build()
            .map_err(|e| ClientError::ServerUnreachable(e.to_string()))?;

        let url = format!("{}/api/v1/auth/validate", server_url.trim_end_matches('/'));

        let auth_response = Self::do_validate(&http, &url, api_key, config.max_retries).await?;

        let validated = auth_response.client;
        let cache_ttl = Duration::from_secs(config.cache_ttl_secs);

        let cache = Arc::new(Mutex::new(Some(CacheEntry {
            client: validated.clone(),
            cached_at: Instant::now(),
        })));

        Ok(Self {
            validated,
            server_url: server_url.trim_end_matches('/').to_string(),
            api_key: api_key.to_string(),
            http,
            cache,
            cache_ttl,
            #[cfg(feature = "sqlite-storage")]
            storage: None,
        })
    }

    /// The authenticated client ID.
    pub fn client_id(&self) -> &str {
        &self.validated.client_id
    }

    /// Granted permissions.
    pub fn permissions(&self) -> &[String] {
        &self.validated.permissions
    }

    /// Check whether this client has a specific permission.
    pub fn has_permission(&self, perm: &str) -> bool {
        self.validated.permissions.iter().any(|p| p == perm)
    }

    /// Re-validate the API key, using the cache if still fresh.
    pub async fn revalidate(&self) -> Result<ValidatedClient, ClientError> {
        // Check cache
        {
            let guard = self.cache.lock().unwrap();
            if let Some(entry) = guard.as_ref() {
                if entry.cached_at.elapsed() < self.cache_ttl {
                    return Ok(entry.client.clone());
                }
            }
        }

        // Cache miss or expired  call server
        let url = format!("{}/api/v1/auth/validate", self.server_url);
        let auth = Self::do_validate(&self.http, &url, &self.api_key, 3).await?;

        // Update cache
        {
            let mut guard = self.cache.lock().unwrap();
            *guard = Some(CacheEntry {
                client: auth.client.clone(),
                cached_at: Instant::now(),
            });
        }

        Ok(auth.client)
    }

    /// Explicitly invalidate the validation cache.
    pub fn invalidate_cache(&self) {
        let mut guard = self.cache.lock().unwrap();
        *guard = None;
    }

    /// Bind a storage backend for delegated operations.
    #[cfg(feature = "sqlite-storage")]
    pub fn with_storage(mut self, storage: Arc<dyn StorageBackend>) -> Self {
        self.storage = Some(storage);
        self
    }

    //  Delegated storage operations

    /// Save a decision (requires "write" permission and a bound storage backend).
    #[cfg(feature = "sqlite-storage")]
    pub async fn save_decision(&self, decision: &DecisionSnapshot) -> Result<String, ClientError> {
        self.require_permission("write")?;
        let storage = self.require_storage()?;
        storage
            .save_decision(decision)
            .await
            .map_err(ClientError::from)
    }

    /// Load a decision by ID (requires "read" permission and a bound storage backend).
    #[cfg(feature = "sqlite-storage")]
    pub async fn load_decision(&self, decision_id: &str) -> Result<DecisionSnapshot, ClientError> {
        self.require_permission("read")?;
        let storage = self.require_storage()?;
        storage
            .load_decision(decision_id)
            .await
            .map_err(ClientError::from)
    }

    /// Query snapshots (requires "read" permission and a bound storage backend).
    #[cfg(feature = "sqlite-storage")]
    pub async fn query(&self, query: SnapshotQuery) -> Result<Vec<Snapshot>, ClientError> {
        self.require_permission("read")?;
        let storage = self.require_storage()?;
        storage.query(query).await.map_err(ClientError::from)
    }

    /// Delete a snapshot (requires "delete" permission and a bound storage backend).
    #[cfg(feature = "sqlite-storage")]
    pub async fn delete(&self, id: &str) -> Result<bool, ClientError> {
        self.require_permission("delete")?;
        let storage = self.require_storage()?;
        storage.delete(id).await.map_err(ClientError::from)
    }

    //  Internal helpers

    fn require_permission(&self, perm: &str) -> Result<(), ClientError> {
        if self.has_permission(perm) {
            Ok(())
        } else {
            Err(ClientError::PermissionDenied(perm.to_string()))
        }
    }

    #[cfg(feature = "sqlite-storage")]
    fn require_storage(&self) -> Result<&Arc<dyn StorageBackend>, ClientError> {
        self.storage.as_ref().ok_or(ClientError::NoStorage)
    }

    async fn do_validate(
        http: &reqwest::Client,
        url: &str,
        api_key: &str,
        max_retries: u32,
    ) -> Result<AuthResponse, ClientError> {
        let body = serde_json::json!({ "api_key": api_key });
        let mut last_err = None;

        for attempt in 0..=max_retries {
            if attempt > 0 {
                // Exponential backoff: 100ms, 200ms, 400ms,
                let backoff = Duration::from_millis(100 * (1 << (attempt - 1)));
                tokio::time::sleep(backoff).await;
            }

            let result = http.post(url).json(&body).send().await;

            match result {
                Ok(resp) => {
                    let status = resp.status();
                    if status.is_success() {
                        let auth: AuthResponse = resp.json().await.map_err(|e| {
                            ClientError::ServerUnreachable(format!("Invalid response body: {}", e))
                        })?;
                        if !auth.valid {
                            return Err(ClientError::AuthFailed(
                                "Server returned valid=false".into(),
                            ));
                        }
                        return Ok(auth);
                    } else if status == reqwest::StatusCode::UNAUTHORIZED {
                        // 401  bad key, don't retry
                        let text = resp.text().await.unwrap_or_default();
                        return Err(ClientError::AuthFailed(format!(
                            "Invalid API key (401): {}",
                            text
                        )));
                    } else if status.is_server_error() {
                        // 5xx  transient, retry
                        last_err = Some(ClientError::ServerUnreachable(format!(
                            "Server error ({})",
                            status
                        )));
                    } else {
                        // 4xx other  don't retry
                        let text = resp.text().await.unwrap_or_default();
                        return Err(ClientError::AuthFailed(format!(
                            "Unexpected status {}: {}",
                            status, text
                        )));
                    }
                }
                Err(e) => {
                    // Network error  retry
                    last_err = Some(ClientError::ServerUnreachable(e.to_string()));
                }
            }
        }

        Err(last_err.unwrap_or_else(|| ClientError::ServerUnreachable("Unknown error".into())))
    }
}

//  Tests

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn mock_auth_response(client_id: &str, permissions: Vec<&str>) -> serde_json::Value {
        serde_json::json!({
            "valid": true,
            "client": {
                "client_id": client_id,
                "permissions": permissions,
                "rate_limit_rps": 100,
                "metadata": {}
            },
            "expires_at": (Utc::now() + chrono::Duration::hours(1)).to_rfc3339()
        })
    }

    //  Construction / validation

    #[tokio::test]
    async fn test_new_valid_key() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("acme", vec!["read", "write"])),
            )
            .mount(&server)
            .await;

        let client = BriefcaseClient::new("sk-valid", &server.uri())
            .await
            .expect("should succeed");

        assert_eq!(client.client_id(), "acme");
        assert!(client.has_permission("read"));
        assert!(client.has_permission("write"));
        assert!(!client.has_permission("admin"));
    }

    #[tokio::test]
    async fn test_new_invalid_key() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(401)
                    .set_body_json(serde_json::json!({"error": "Invalid API key"})),
            )
            .mount(&server)
            .await;

        let result = BriefcaseClient::new("sk-bad", &server.uri()).await;
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.to_string().contains("Invalid API key"), "Got: {}", err);
    }

    #[tokio::test]
    async fn test_new_server_down() {
        // Port 1 is almost certainly not listening
        let result = BriefcaseClient::with_config(
            "sk-test",
            "http://127.0.0.1:1",
            ClientConfig {
                timeout_secs: 1,
                cache_ttl_secs: 60,
                max_retries: 0, // don't retry  fast test
                ..Default::default()
            },
        )
        .await;

        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::ServerUnreachable(_) => {} // expected
            other => panic!("Expected ServerUnreachable, got: {}", other),
        }
    }

    #[tokio::test]
    async fn test_new_server_500() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(ResponseTemplate::new(500).set_body_string("Internal Server Error"))
            .mount(&server)
            .await;

        let result = BriefcaseClient::with_config(
            "sk-test",
            &server.uri(),
            ClientConfig {
                timeout_secs: 2,
                cache_ttl_secs: 60,
                max_retries: 0,
                ..Default::default()
            },
        )
        .await;

        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::ServerUnreachable(msg) => {
                assert!(msg.contains("500"), "Got: {}", msg);
            }
            other => panic!("Expected ServerUnreachable, got: {}", other),
        }
    }

    #[tokio::test]
    async fn test_new_empty_key() {
        let result = BriefcaseClient::new("", "http://localhost:8080").await;
        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::InvalidArgument(msg) => {
                assert!(msg.contains("API key"), "Got: {}", msg);
            }
            other => panic!("Expected InvalidArgument, got: {}", other),
        }
    }

    #[tokio::test]
    async fn test_new_whitespace_only_key() {
        let result = BriefcaseClient::new("   ", "http://localhost:8080").await;
        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::InvalidArgument(msg) => {
                assert!(msg.contains("API key"), "Got: {}", msg);
            }
            other => panic!("Expected InvalidArgument, got: {}", other),
        }
    }

    #[tokio::test]
    async fn test_new_empty_url() {
        let result = BriefcaseClient::new("sk-test", "").await;
        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::InvalidArgument(msg) => {
                assert!(msg.contains("Server URL"), "Got: {}", msg);
            }
            other => panic!("Expected InvalidArgument, got: {}", other),
        }
    }

    //  Transport security

    #[tokio::test]
    async fn test_http_non_loopback_allowed_when_opted_in() {
        // 192.0.2.1 is TEST-NET-1: unroutable, so the attempt fails at the
        // network layer rather than at the transport-security gate.
        let result = BriefcaseClient::with_config(
            "sk-test",
            "http://192.0.2.1:9",
            ClientConfig {
                timeout_secs: 1,
                cache_ttl_secs: 60,
                max_retries: 0,
                allow_insecure_http: true,
            },
        )
        .await;

        assert!(result.is_err());
        assert!(
            !matches!(result.unwrap_err(), ClientError::InvalidArgument(_)),
            "opt-in must bypass the http rejection"
        );
    }

    #[tokio::test]
    async fn test_redirect_to_remote_http_is_refused() {
        // A trusted endpoint answering with a downgrade redirect must not
        // cause the key-bearing body to be re-sent over remote plain http.
        // 192.0.2.1 is TEST-NET-1, so if the redirect were followed the
        // attempt would fail at the network layer with a non-redirect error.
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(307)
                    .insert_header("location", "http://192.0.2.1:9/api/v1/auth/validate"),
            )
            .mount(&server)
            .await;

        let result = BriefcaseClient::with_config(
            "sk-test",
            &server.uri(),
            ClientConfig {
                timeout_secs: 2,
                cache_ttl_secs: 60,
                max_retries: 0,
                ..Default::default()
            },
        )
        .await;

        let msg = result.unwrap_err().to_string();
        assert!(msg.contains("redirect"), "Got: {}", msg);
    }

    #[tokio::test]
    async fn test_http_non_loopback_rejected() {
        // 192.0.2.1 is TEST-NET-1: never routable, so no key ever leaves.
        let result = BriefcaseClient::with_config(
            "sk-test",
            "http://192.0.2.1:9",
            ClientConfig {
                timeout_secs: 1,
                cache_ttl_secs: 60,
                max_retries: 0,
                ..Default::default()
            },
        )
        .await;

        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::InvalidArgument(msg) => {
                assert!(msg.contains("https"), "Got: {}", msg);
            }
            other => panic!("Expected InvalidArgument, got: {}", other),
        }

        // Same rejection for a hostname; no DNS lookup or connection happens.
        let result = BriefcaseClient::with_config(
            "sk-test",
            "http://api.example.com:8080",
            ClientConfig {
                timeout_secs: 1,
                cache_ttl_secs: 60,
                max_retries: 0,
                ..Default::default()
            },
        )
        .await;

        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::InvalidArgument(msg) => {
                assert!(msg.contains("https"), "Got: {}", msg);
            }
            other => panic!("Expected InvalidArgument, got: {}", other),
        }
    }

    #[tokio::test]
    async fn test_http_loopback_allowed() {
        // Loopback http passes the transport check and fails at connect time.
        for url in ["http://localhost:1", "http://127.0.0.1:1", "http://[::1]:1"] {
            let result = BriefcaseClient::with_config(
                "sk-test",
                url,
                ClientConfig {
                    timeout_secs: 1,
                    cache_ttl_secs: 60,
                    max_retries: 0,
                    ..Default::default()
                },
            )
            .await;

            assert!(result.is_err());
            match result.unwrap_err() {
                ClientError::ServerUnreachable(_) => {} // expected
                other => panic!("Expected ServerUnreachable for {}, got: {}", url, other),
            }
        }
    }

    #[tokio::test]
    async fn test_https_non_loopback_allowed() {
        // https passes the transport check regardless of host.
        let result = BriefcaseClient::with_config(
            "sk-test",
            "https://192.0.2.1:9",
            ClientConfig {
                timeout_secs: 1,
                cache_ttl_secs: 60,
                max_retries: 0,
                ..Default::default()
            },
        )
        .await;

        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::ServerUnreachable(_) => {} // expected
            other => panic!("Expected ServerUnreachable, got: {}", other),
        }
    }

    //  Cache behaviour

    #[tokio::test]
    async fn test_cache_hit() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("acme", vec!["read"])),
            )
            .expect(1) // exactly one HTTP call
            .mount(&server)
            .await;

        let client = BriefcaseClient::new("sk-test", &server.uri())
            .await
            .unwrap();

        // Second call should use cache, not hit the server
        let info = client.revalidate().await.unwrap();
        assert_eq!(info.client_id, "acme");
    }

    #[tokio::test]
    async fn test_cache_expired() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("acme", vec!["read"])),
            )
            .expect(2) // initial + revalidation after expiry
            .mount(&server)
            .await;

        let client = BriefcaseClient::with_config(
            "sk-test",
            &server.uri(),
            ClientConfig {
                timeout_secs: 5,
                cache_ttl_secs: 0, // immediate expiry
                max_retries: 0,
                ..Default::default()
            },
        )
        .await
        .unwrap();

        // Short sleep so Instant::now() moves past cached_at
        tokio::time::sleep(Duration::from_millis(10)).await;

        let info = client.revalidate().await.unwrap();
        assert_eq!(info.client_id, "acme");
    }

    #[tokio::test]
    async fn test_invalidate_cache() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("acme", vec!["read"])),
            )
            .expect(2) // initial + after invalidation
            .mount(&server)
            .await;

        let client = BriefcaseClient::new("sk-test", &server.uri())
            .await
            .unwrap();

        client.invalidate_cache();

        let info = client.revalidate().await.unwrap();
        assert_eq!(info.client_id, "acme");
    }

    //  Permission checks

    #[tokio::test]
    async fn test_permission_check() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("acme", vec!["read", "write", "replay"])),
            )
            .mount(&server)
            .await;

        let client = BriefcaseClient::new("sk-test", &server.uri())
            .await
            .unwrap();

        assert!(client.has_permission("read"));
        assert!(client.has_permission("write"));
        assert!(client.has_permission("replay"));
        assert!(!client.has_permission("delete"));
        assert!(!client.has_permission("admin"));
        assert!(!client.has_permission(""));
    }

    #[tokio::test]
    async fn test_permissions_list() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("acme", vec!["read", "write"])),
            )
            .mount(&server)
            .await;

        let client = BriefcaseClient::new("sk-test", &server.uri())
            .await
            .unwrap();

        assert_eq!(client.permissions(), &["read", "write"]);
    }

    //  Storage delegation

    #[cfg(feature = "sqlite-storage")]
    #[tokio::test]
    async fn test_save_without_storage() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("acme", vec!["read", "write"])),
            )
            .mount(&server)
            .await;

        let client = BriefcaseClient::new("sk-test", &server.uri())
            .await
            .unwrap();

        let decision = DecisionSnapshot::new("test_fn");
        let result = client.save_decision(&decision).await;
        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::NoStorage => {} // expected
            other => panic!("Expected NoStorage, got: {}", other),
        }
    }

    #[cfg(feature = "sqlite-storage")]
    #[tokio::test]
    async fn test_save_without_write_perm() {
        use crate::storage::SqliteBackend;

        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("readonly", vec!["read"])),
            )
            .mount(&server)
            .await;

        let storage = Arc::new(SqliteBackend::in_memory().unwrap());
        let client = BriefcaseClient::new("sk-test", &server.uri())
            .await
            .unwrap()
            .with_storage(storage);

        let decision = DecisionSnapshot::new("test_fn");
        let result = client.save_decision(&decision).await;
        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::PermissionDenied(perm) => assert_eq!(perm, "write"),
            other => panic!("Expected PermissionDenied(write), got: {}", other),
        }
    }

    #[cfg(feature = "sqlite-storage")]
    #[tokio::test]
    async fn test_save_with_storage() {
        use crate::storage::SqliteBackend;

        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("acme", vec!["read", "write"])),
            )
            .mount(&server)
            .await;

        let storage = Arc::new(SqliteBackend::in_memory().unwrap());
        let client = BriefcaseClient::new("sk-test", &server.uri())
            .await
            .unwrap()
            .with_storage(storage);

        let decision = DecisionSnapshot::new("test_fn");
        let id = client.save_decision(&decision).await.unwrap();
        assert!(!id.is_empty());

        // Read it back
        let loaded = client.load_decision(&id).await.unwrap();
        assert_eq!(loaded.function_name, "test_fn");
    }

    #[cfg(feature = "sqlite-storage")]
    #[tokio::test]
    async fn test_load_without_read_perm() {
        use crate::storage::SqliteBackend;

        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("writer", vec!["write"])),
            )
            .mount(&server)
            .await;

        let storage = Arc::new(SqliteBackend::in_memory().unwrap());
        let client = BriefcaseClient::new("sk-test", &server.uri())
            .await
            .unwrap()
            .with_storage(storage);

        let result = client.load_decision("some-id").await;
        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::PermissionDenied(perm) => assert_eq!(perm, "read"),
            other => panic!("Expected PermissionDenied(read), got: {}", other),
        }
    }

    #[cfg(feature = "sqlite-storage")]
    #[tokio::test]
    async fn test_delete_without_perm() {
        use crate::storage::SqliteBackend;

        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(mock_auth_response("acme", vec!["read", "write"])),
            )
            .mount(&server)
            .await;

        let storage = Arc::new(SqliteBackend::in_memory().unwrap());
        let client = BriefcaseClient::new("sk-test", &server.uri())
            .await
            .unwrap()
            .with_storage(storage);

        let result = client.delete("some-id").await;
        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::PermissionDenied(perm) => assert_eq!(perm, "delete"),
            other => panic!("Expected PermissionDenied(delete), got: {}", other),
        }
    }

    //  Concurrency

    #[tokio::test]
    async fn test_concurrent_revalidate() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(mock_auth_response("acme", vec!["read"])),
            )
            .mount(&server)
            .await;

        let client = Arc::new(
            BriefcaseClient::with_config(
                "sk-test",
                &server.uri(),
                ClientConfig {
                    timeout_secs: 5,
                    cache_ttl_secs: 0, // force revalidation each time
                    max_retries: 0,
                    ..Default::default()
                },
            )
            .await
            .unwrap(),
        );

        let mut handles = vec![];
        for _ in 0..10 {
            let c = client.clone();
            handles.push(tokio::spawn(async move { c.revalidate().await }));
        }

        for handle in handles {
            let result = handle.await.unwrap();
            assert!(result.is_ok());
        }
    }

    //  Malformed responses

    #[tokio::test]
    async fn test_malformed_response() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(ResponseTemplate::new(200).set_body_string("this is not json"))
            .mount(&server)
            .await;

        let result = BriefcaseClient::with_config(
            "sk-test",
            &server.uri(),
            ClientConfig {
                timeout_secs: 2,
                cache_ttl_secs: 60,
                max_retries: 0,
                ..Default::default()
            },
        )
        .await;

        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::ServerUnreachable(msg) => {
                assert!(msg.contains("Invalid response body"), "Got: {}", msg);
            }
            other => panic!("Expected ServerUnreachable, got: {}", other),
        }
    }

    #[tokio::test]
    async fn test_response_valid_false() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "valid": false,
                "client": {
                    "client_id": "disabled",
                    "permissions": [],
                    "metadata": {}
                },
                "expires_at": Utc::now().to_rfc3339()
            })))
            .mount(&server)
            .await;

        let result = BriefcaseClient::with_config(
            "sk-test",
            &server.uri(),
            ClientConfig {
                timeout_secs: 2,
                cache_ttl_secs: 60,
                max_retries: 0,
                ..Default::default()
            },
        )
        .await;

        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::AuthFailed(msg) => {
                assert!(msg.contains("valid=false"), "Got: {}", msg);
            }
            other => panic!("Expected AuthFailed, got: {}", other),
        }
    }

    //  Config defaults

    #[test]
    fn test_default_config_values() {
        let config = ClientConfig::default();
        assert_eq!(config.timeout_secs, 30);
        assert_eq!(config.cache_ttl_secs, 3600);
        assert_eq!(config.max_retries, 3);
    }

    //  URL normalization

    #[tokio::test]
    async fn test_trailing_slash_stripped() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/validate"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(mock_auth_response("acme", vec!["read"])),
            )
            .mount(&server)
            .await;

        let url_with_slash = format!("{}/", server.uri());
        let client = BriefcaseClient::new("sk-test", &url_with_slash)
            .await
            .unwrap();
        assert_eq!(client.client_id(), "acme");
    }
}
