use crate::models::{DecisionSnapshot, Snapshot};
#[cfg(feature = "async")]
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use std::collections::HashMap;
use thiserror::Error;

pub mod buffered;
#[cfg(feature = "sqlite-storage")]
pub mod sqlite;
pub mod sync;
#[cfg(feature = "vcs-storage")]
pub mod vcs;

#[cfg(feature = "sqlite-storage")]
pub use sqlite::SqliteBackend;
#[cfg(feature = "sqlite-storage")]
pub use sync::SyncSqliteBackend;
pub use sync::{MemoryStorageBackend, SyncStorageBackend};

#[cfg(feature = "async")]
#[async_trait]
pub trait StorageBackend: Send + Sync {
    /// Save a snapshot, return its ID
    async fn save(&self, snapshot: &Snapshot) -> Result<String, StorageError>;

    /// Save a single decision snapshot
    async fn save_decision(&self, decision: &DecisionSnapshot) -> Result<String, StorageError>;

    /// Load a snapshot by ID
    async fn load(&self, snapshot_id: &str) -> Result<Snapshot, StorageError>;

    /// Load a decision by ID
    async fn load_decision(&self, decision_id: &str) -> Result<DecisionSnapshot, StorageError>;

    /// Query snapshots with filters
    async fn query(&self, query: SnapshotQuery) -> Result<Vec<Snapshot>, StorageError>;

    /// Delete a snapshot
    async fn delete(&self, snapshot_id: &str) -> Result<bool, StorageError>;

    /// Flush pending writes (for batching backends)
    async fn flush(&self) -> Result<FlushResult, StorageError>;

    /// Check health/connectivity
    async fn health_check(&self) -> Result<bool, StorageError>;
}

#[derive(Debug, Clone, Default)]
pub struct SnapshotQuery {
    pub function_name: Option<String>,
    pub module_name: Option<String>,
    pub model_name: Option<String>,
    pub start_time: Option<DateTime<Utc>>,
    pub end_time: Option<DateTime<Utc>>,
    pub tags: Option<HashMap<String, String>>,
    pub limit: Option<usize>,
    pub offset: Option<usize>,
}

impl SnapshotQuery {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_function_name(mut self, function_name: impl Into<String>) -> Self {
        self.function_name = Some(function_name.into());
        self
    }

    pub fn with_module_name(mut self, module_name: impl Into<String>) -> Self {
        self.module_name = Some(module_name.into());
        self
    }

    pub fn with_model_name(mut self, model_name: impl Into<String>) -> Self {
        self.model_name = Some(model_name.into());
        self
    }

    pub fn with_time_range(mut self, start: DateTime<Utc>, end: DateTime<Utc>) -> Self {
        self.start_time = Some(start);
        self.end_time = Some(end);
        self
    }

    pub fn with_tag(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        if self.tags.is_none() {
            self.tags = Some(HashMap::new());
        }
        self.tags.as_mut().unwrap().insert(key.into(), value.into());
        self
    }

    pub fn with_limit(mut self, limit: usize) -> Self {
        self.limit = Some(limit);
        self
    }

    pub fn with_offset(mut self, offset: usize) -> Self {
        self.offset = Some(offset);
        self
    }
}

#[derive(Debug, Clone)]
pub struct FlushResult {
    pub snapshots_written: usize,
    pub bytes_written: usize,
    pub checkpoint_id: Option<String>,
}

#[derive(Error, Debug, Clone, PartialEq)]
pub enum StorageError {
    #[error("Not found: {0}")]
    NotFound(String),
    #[error("Connection error: {0}")]
    ConnectionError(String),
    #[error("Serialization error: {0}")]
    SerializationError(String),
    #[error("Permission denied: {0}")]
    PermissionDenied(String),
    #[error("Quota exceeded")]
    QuotaExceeded,
    #[error("Invalid query: {0}")]
    InvalidQuery(String),
    #[error("IO error: {0}")]
    IoError(String),
}

impl From<serde_json::Error> for StorageError {
    fn from(err: serde_json::Error) -> Self {
        StorageError::SerializationError(err.to_string())
    }
}

impl From<std::io::Error> for StorageError {
    fn from(err: std::io::Error) -> Self {
        StorageError::IoError(err.to_string())
    }
}
