//! Python bindings for the BriefcaseClient.

use crate::runtime::block_on_result;
use briefcase_core::client::{BriefcaseClient, ClientConfig, ValidatedClient};
use pyo3::prelude::*;

/// Validated client information returned by the server.
#[pyclass(name = "ValidatedClient")]
#[derive(Clone)]
pub struct PyValidatedClient {
    pub client_id: String,
    pub permissions: Vec<String>,
    pub rate_limit_rps: Option<u32>,
}

#[pymethods]
impl PyValidatedClient {
    #[getter]
    fn client_id(&self) -> &str {
        &self.client_id
    }

    #[getter]
    fn permissions(&self) -> Vec<String> {
        self.permissions.clone()
    }

    #[getter]
    fn rate_limit_rps(&self) -> Option<u32> {
        self.rate_limit_rps
    }

    fn __repr__(&self) -> String {
        format!(
            "ValidatedClient(client_id='{}', permissions={:?}, rate_limit_rps={:?})",
            self.client_id, self.permissions, self.rate_limit_rps
        )
    }
}

impl From<ValidatedClient> for PyValidatedClient {
    fn from(vc: ValidatedClient) -> Self {
        Self {
            client_id: vc.client_id,
            permissions: vc.permissions,
            rate_limit_rps: vc.rate_limit_rps,
        }
    }
}

/// Authenticated client for the Briefcase AI platform.
///
/// Validates an API key against the server and caches the result.
///
/// Example:
///     import briefcase
///     from briefcase import _native
///
///     briefcase.init()
///     client = _native.BriefcaseClient("sk-my-key", "http://localhost:8080")
///     print(client.client_id)
///     print(client.has_permission("read"))
#[pyclass(name = "BriefcaseClient")]
pub struct PyBriefcaseClient {
    inner: BriefcaseClient,
}

#[pymethods]
impl PyBriefcaseClient {
    /// Create a new authenticated client.
    ///
    /// Args:
    ///     api_key: Your Briefcase AI API key.
    ///     server_url: The Briefcase server URL.
    ///     cache_ttl_secs: How long to cache validation results (default: 3600).
    #[new]
    #[pyo3(signature = (api_key, server_url, cache_ttl_secs=3600))]
    fn new(api_key: String, server_url: String, cache_ttl_secs: u64) -> PyResult<Self> {
        let config = ClientConfig {
            timeout_secs: 30,
            cache_ttl_secs,
            max_retries: 3,
        };
        let inner = block_on_result(BriefcaseClient::with_config(&api_key, &server_url, config))?;
        Ok(Self { inner })
    }

    /// The authenticated client ID.
    #[getter]
    fn client_id(&self) -> &str {
        self.inner.client_id()
    }

    /// Granted permissions.
    #[getter]
    fn permissions(&self) -> Vec<String> {
        self.inner.permissions().to_vec()
    }

    /// Check whether this client has a specific permission.
    fn has_permission(&self, permission: String) -> bool {
        self.inner.has_permission(&permission)
    }

    /// Re-validate the API key against the server (uses cache if fresh).
    fn validate(&self) -> PyResult<PyValidatedClient> {
        let info = block_on_result(self.inner.revalidate())?;
        Ok(info.into())
    }

    /// Explicitly invalidate the validation cache.
    fn invalidate_cache(&self) {
        self.inner.invalidate_cache();
    }

    fn __repr__(&self) -> String {
        format!(
            "BriefcaseClient(client_id='{}', permissions={:?})",
            self.inner.client_id(),
            self.inner.permissions()
        )
    }
}
