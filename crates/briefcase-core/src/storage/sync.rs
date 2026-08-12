//! Synchronous storage backend implementations
//!
//! This module provides synchronous variants of the storage backends
//! for use cases that don't require async functionality.

use super::{FlushResult, SnapshotQuery, StorageError};
use crate::models::{DecisionSnapshot, Snapshot};
use std::collections::HashMap;

/// Synchronous storage backend trait
pub trait SyncStorageBackend: Send + Sync {
    /// Save a snapshot, return its ID
    fn save(&self, snapshot: &Snapshot) -> Result<String, StorageError>;

    /// Save a single decision snapshot
    fn save_decision(&self, decision: &DecisionSnapshot) -> Result<String, StorageError>;

    /// Load a snapshot by ID
    fn load(&self, snapshot_id: &str) -> Result<Snapshot, StorageError>;

    /// Load a decision by ID
    fn load_decision(&self, decision_id: &str) -> Result<DecisionSnapshot, StorageError>;

    /// Query snapshots with filters
    fn query(&self, query: SnapshotQuery) -> Result<Vec<Snapshot>, StorageError>;

    /// Delete a snapshot
    fn delete(&self, snapshot_id: &str) -> Result<bool, StorageError>;

    /// Flush pending writes (for batching backends)
    fn flush(&self) -> Result<FlushResult, StorageError>;

    /// Check health/connectivity
    fn health_check(&self) -> Result<bool, StorageError>;
}

/// Synchronous SQLite backend implementation
#[cfg(feature = "sqlite-storage")]
pub struct SyncSqliteBackend {
    inner: super::sqlite::SqliteBackend,
}

#[cfg(feature = "sqlite-storage")]
impl SyncSqliteBackend {
    /// Create or open a SQLite database at the given path
    pub fn new(path: impl AsRef<std::path::Path>) -> Result<Self, StorageError> {
        let inner = super::sqlite::SqliteBackend::new(path)?;
        Ok(Self { inner })
    }

    /// Create an in-memory database (for testing)
    pub fn in_memory() -> Result<Self, StorageError> {
        let inner = super::sqlite::SqliteBackend::in_memory()?;
        Ok(Self { inner })
    }
}

#[cfg(feature = "sqlite-storage")]
impl SyncStorageBackend for SyncSqliteBackend {
    fn save(&self, snapshot: &Snapshot) -> Result<String, StorageError> {
        self.inner.save_internal(snapshot)
    }

    fn save_decision(&self, decision: &DecisionSnapshot) -> Result<String, StorageError> {
        self.inner.save_decision_internal(decision)
    }

    fn load(&self, snapshot_id: &str) -> Result<Snapshot, StorageError> {
        self.inner.load_internal(snapshot_id)
    }

    fn load_decision(&self, decision_id: &str) -> Result<DecisionSnapshot, StorageError> {
        let snapshot = self.load(decision_id)?;
        if let Some(decision) = snapshot.decisions.first() {
            Ok(decision.clone())
        } else {
            Err(StorageError::NotFound(format!(
                "Decision {} not found",
                decision_id
            )))
        }
    }

    fn query(&self, query: SnapshotQuery) -> Result<Vec<Snapshot>, StorageError> {
        self.inner.query_internal(query)
    }

    fn delete(&self, snapshot_id: &str) -> Result<bool, StorageError> {
        let conn_guard = self.inner.conn.lock().unwrap();

        let rows_affected = conn_guard
            .execute(
                "DELETE FROM snapshots WHERE id = ?",
                rusqlite::params![snapshot_id],
            )
            .map_err(|e| {
                StorageError::ConnectionError(format!("Failed to delete snapshot: {}", e))
            })?;

        Ok(rows_affected > 0)
    }

    fn flush(&self) -> Result<FlushResult, StorageError> {
        let conn_guard = self.inner.conn.lock().unwrap();

        // Force WAL checkpoint; the pragma returns a status row
        conn_guard
            .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()))
            .map_err(|e| {
                StorageError::ConnectionError(format!("Failed to checkpoint WAL: {}", e))
            })?;

        // Get stats
        let snapshot_count: i64 = conn_guard
            .query_row("SELECT COUNT(*) FROM snapshots", [], |row| row.get(0))
            .unwrap_or(0);

        Ok(FlushResult {
            snapshots_written: snapshot_count as usize,
            bytes_written: 0, // SQLite doesn't easily report this
            checkpoint_id: None,
        })
    }

    fn health_check(&self) -> Result<bool, StorageError> {
        let conn_guard = self.inner.conn.lock().unwrap();

        // Simple query to check connection
        let _: i64 = conn_guard
            .query_row("SELECT 1", [], |row| row.get(0))
            .map_err(|e| StorageError::ConnectionError(format!("Health check failed: {}", e)))?;

        Ok(true)
    }
}

/// In-memory synchronous storage backend for simple use cases
pub struct MemoryStorageBackend {
    snapshots: std::sync::Mutex<HashMap<String, Snapshot>>,
}

impl MemoryStorageBackend {
    /// Create a new in-memory storage backend
    pub fn new() -> Self {
        Self {
            snapshots: std::sync::Mutex::new(HashMap::new()),
        }
    }
}

impl Default for MemoryStorageBackend {
    fn default() -> Self {
        Self::new()
    }
}

impl SyncStorageBackend for MemoryStorageBackend {
    fn save(&self, snapshot: &Snapshot) -> Result<String, StorageError> {
        let snapshot_id = snapshot.metadata.snapshot_id.to_string();
        let mut snapshots = self.snapshots.lock().unwrap();
        snapshots.insert(snapshot_id.clone(), snapshot.clone());
        Ok(snapshot_id)
    }

    fn save_decision(&self, decision: &DecisionSnapshot) -> Result<String, StorageError> {
        // For simplicity, save decisions as individual snapshots
        let snapshot = Snapshot {
            metadata: decision.metadata.clone(),
            decisions: vec![decision.clone()],
            snapshot_type: crate::models::SnapshotType::Decision,
        };
        self.save(&snapshot)
    }

    fn load(&self, snapshot_id: &str) -> Result<Snapshot, StorageError> {
        let snapshots = self.snapshots.lock().unwrap();
        snapshots
            .get(snapshot_id)
            .cloned()
            .ok_or_else(|| StorageError::NotFound(format!("Snapshot {} not found", snapshot_id)))
    }

    fn load_decision(&self, decision_id: &str) -> Result<DecisionSnapshot, StorageError> {
        let snapshot = self.load(decision_id)?;
        if let Some(decision) = snapshot.decisions.first() {
            Ok(decision.clone())
        } else {
            Err(StorageError::NotFound(format!(
                "Decision {} not found",
                decision_id
            )))
        }
    }

    fn query(&self, query: SnapshotQuery) -> Result<Vec<Snapshot>, StorageError> {
        let snapshots = self.snapshots.lock().unwrap();
        let mut results = Vec::new();

        for (_, snapshot) in snapshots.iter() {
            if matches_query(snapshot, &query) {
                results.push(snapshot.clone());
            }
        }

        // Sort by timestamp (newest first)
        results.sort_by_key(|b| std::cmp::Reverse(b.metadata.timestamp));

        // Apply pagination
        let offset = query.offset.unwrap_or(0);
        let limit = query.limit.unwrap_or(usize::MAX);
        let end = offset.saturating_add(limit).min(results.len());

        Ok(results.get(offset..end).unwrap_or(&[]).to_vec())
    }

    fn delete(&self, snapshot_id: &str) -> Result<bool, StorageError> {
        let mut snapshots = self.snapshots.lock().unwrap();
        Ok(snapshots.remove(snapshot_id).is_some())
    }

    fn flush(&self) -> Result<FlushResult, StorageError> {
        let snapshots = self.snapshots.lock().unwrap();
        Ok(FlushResult {
            snapshots_written: snapshots.len(),
            bytes_written: 0, // Not meaningful for in-memory
            checkpoint_id: None,
        })
    }

    fn health_check(&self) -> Result<bool, StorageError> {
        // Always healthy for in-memory backend
        Ok(true)
    }
}

/// Check if a snapshot matches query filters
fn matches_query(snapshot: &Snapshot, query: &SnapshotQuery) -> bool {
    // Check time range
    if let Some(start_time) = query.start_time {
        if snapshot.metadata.timestamp < start_time {
            return false;
        }
    }

    if let Some(end_time) = query.end_time {
        if snapshot.metadata.timestamp > end_time {
            return false;
        }
    }

    // Check content filters
    if query.function_name.is_some()
        || query.module_name.is_some()
        || query.model_name.is_some()
        || query.tags.is_some()
    {
        for decision in &snapshot.decisions {
            if let Some(function_name) = &query.function_name {
                if decision.function_name != *function_name {
                    continue;
                }
            }

            if let Some(module_name) = &query.module_name {
                if decision.module_name.as_ref() != Some(module_name) {
                    continue;
                }
            }

            if let Some(model_name) = &query.model_name {
                if let Some(model_params) = &decision.model_parameters {
                    if model_params.model_name != *model_name {
                        continue;
                    }
                } else {
                    continue;
                }
            }

            if let Some(query_tags) = &query.tags {
                let mut all_tags_match = true;
                for (key, value) in query_tags {
                    if decision.tags.get(key) != Some(value) {
                        all_tags_match = false;
                        break;
                    }
                }
                if !all_tags_match {
                    continue;
                }
            }

            // If we get here, this decision matches all filters
            return true;
        }

        // No decisions matched the filters
        false
    } else {
        // No content filters, so it matches
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::*;
    use serde_json::json;

    fn create_test_snapshot() -> Snapshot {
        let input = Input::new("test_input", json!("value"), "string");
        let output = Output::new("test_output", json!("result"), "string");
        let model_params = ModelParameters::new("gpt-4");

        let decision = DecisionSnapshot::new("test_function")
            .with_module("test_module")
            .add_input(input)
            .add_output(output)
            .with_model_parameters(model_params)
            .add_tag("env", "test");

        let mut snapshot = Snapshot::new(SnapshotType::Session);
        snapshot.add_decision(decision);
        snapshot
    }

    #[test]
    fn test_memory_backend_basic_operations() {
        let backend = MemoryStorageBackend::new();
        let snapshot = create_test_snapshot();

        // Save and load
        let snapshot_id = backend.save(&snapshot).unwrap();
        let loaded_snapshot = backend.load(&snapshot_id).unwrap();

        assert_eq!(snapshot.decisions.len(), loaded_snapshot.decisions.len());
        assert_eq!(snapshot.snapshot_type, loaded_snapshot.snapshot_type);

        // Health check
        assert!(backend.health_check().unwrap());

        // Delete
        assert!(backend.delete(&snapshot_id).unwrap());

        // Should not find deleted snapshot
        let result = backend.load(&snapshot_id);
        assert!(matches!(result, Err(StorageError::NotFound(_))));
    }

    #[test]
    fn test_memory_backend_query_by_function_name() {
        let backend = MemoryStorageBackend::new();
        let snapshot = create_test_snapshot();
        backend.save(&snapshot).unwrap();

        let query = SnapshotQuery::new().with_function_name("test_function");
        let results = backend.query(query).unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].decisions[0].function_name, "test_function");
    }

    #[test]
    fn test_memory_backend_query_offset_without_limit() {
        let backend = MemoryStorageBackend::new();
        for _ in 0..3 {
            backend.save(&create_test_snapshot()).unwrap();
        }

        let query = SnapshotQuery::new().with_offset(1);
        let results = backend.query(query).unwrap();
        assert_eq!(results.len(), 2);

        let query = SnapshotQuery::new().with_offset(5);
        let results = backend.query(query).unwrap();
        assert_eq!(results.len(), 0);
    }

    #[cfg(feature = "sqlite-storage")]
    #[test]
    fn test_sync_sqlite_flush_on_file_db() {
        let dir = tempfile::tempdir().unwrap();
        let backend = SyncSqliteBackend::new(dir.path().join("test.db")).unwrap();
        backend.save(&create_test_snapshot()).unwrap();

        let result = backend.flush().unwrap();
        assert_eq!(result.snapshots_written, 1);
    }

    #[cfg(feature = "sqlite-storage")]
    #[test]
    fn test_sync_sqlite_backend() {
        let backend = SyncSqliteBackend::in_memory().unwrap();
        let snapshot = create_test_snapshot();

        // Save and load
        let snapshot_id = backend.save(&snapshot).unwrap();
        let loaded_snapshot = backend.load(&snapshot_id).unwrap();

        assert_eq!(snapshot.decisions.len(), loaded_snapshot.decisions.len());
        assert_eq!(snapshot.snapshot_type, loaded_snapshot.snapshot_type);

        // Health check
        assert!(backend.health_check().unwrap());

        // Delete
        assert!(backend.delete(&snapshot_id).unwrap());

        // Should not find deleted snapshot
        let result = backend.load(&snapshot_id);
        assert!(matches!(result, Err(StorageError::NotFound(_))));
    }
}
