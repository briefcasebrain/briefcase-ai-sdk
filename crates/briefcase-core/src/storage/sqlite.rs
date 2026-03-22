use super::{FlushResult, SnapshotQuery, StorageBackend, StorageError};
use crate::models::{DecisionSnapshot, Snapshot, SnapshotType};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json;
use std::path::Path;
use std::sync::{Arc, Mutex};
#[cfg(feature = "async")]
use tokio::task;

#[derive(Debug, Clone, PartialEq)]
pub enum CompressionType {
    None,
    Gzip,
}

pub struct SqliteBackend {
    pub conn: Arc<Mutex<Connection>>,
}

impl SqliteBackend {
    /// Create or open a SQLite database at the given path
    pub fn new(path: impl AsRef<Path>) -> Result<Self, StorageError> {
        let conn = Connection::open(path).map_err(|e| {
            StorageError::ConnectionError(format!("Failed to open database: {}", e))
        })?;

        let backend = Self {
            conn: Arc::new(Mutex::new(conn)),
        };

        // Run migrations
        {
            let conn_guard = backend.conn.lock().unwrap();
            Self::run_migrations(&conn_guard)?;
        }

        Ok(backend)
    }

    /// Create an in-memory database (for testing)
    pub fn in_memory() -> Result<Self, StorageError> {
        let conn = Connection::open(":memory:").map_err(|e| {
            StorageError::ConnectionError(format!("Failed to create in-memory database: {}", e))
        })?;

        let backend = Self {
            conn: Arc::new(Mutex::new(conn)),
        };

        // Run migrations
        {
            let conn_guard = backend.conn.lock().unwrap();
            Self::run_migrations(&conn_guard)?;
        }

        Ok(backend)
    }

    /// Run database migrations
    fn run_migrations(conn: &Connection) -> Result<(), StorageError> {
        // Enable WAL mode for better concurrent access
        conn.pragma_update(None, "journal_mode", "WAL")
            .map_err(|e| StorageError::ConnectionError(format!("Failed to set WAL mode: {}", e)))?;

        // Enable foreign keys
        conn.pragma_update(None, "foreign_keys", "ON")
            .map_err(|e| {
                StorageError::ConnectionError(format!("Failed to enable foreign keys: {}", e))
            })?;

        // Create snapshots table
        conn.execute(
            r#"
            CREATE TABLE IF NOT EXISTS snapshots (
                id TEXT PRIMARY KEY,
                snapshot_type TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                created_by TEXT,
                checksum TEXT
            )
            "#,
            [],
        )
        .map_err(|e| {
            StorageError::ConnectionError(format!("Failed to create snapshots table: {}", e))
        })?;

        // Create index
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_created_at ON snapshots(created_at)",
            [],
        )
        .map_err(|e| StorageError::ConnectionError(format!("Failed to create index: {}", e)))?;

        Ok(())
    }

    pub fn save_internal(&self, snapshot: &Snapshot) -> Result<String, StorageError> {
        let conn_guard = self.conn.lock().unwrap();
        let snapshot_id = snapshot.metadata.snapshot_id.to_string();

        let data_json = serde_json::to_string(snapshot)
            .map_err(|e| StorageError::SerializationError(e.to_string()))?;

        conn_guard
            .execute(
                r#"
            INSERT OR REPLACE INTO snapshots (
                id, snapshot_type, data_json, created_at, created_by, checksum
            ) VALUES (?, ?, ?, ?, ?, ?)
            "#,
                params![
                    snapshot_id,
                    format!("{:?}", snapshot.snapshot_type),
                    data_json,
                    snapshot
                        .metadata
                        .timestamp
                        .format("%Y-%m-%d %H:%M:%S%.3f")
                        .to_string(),
                    snapshot.metadata.created_by,
                    snapshot.metadata.checksum,
                ],
            )
            .map_err(|e| {
                StorageError::ConnectionError(format!("Failed to insert snapshot: {}", e))
            })?;

        Ok(snapshot_id)
    }

    pub fn save_decision_internal(
        &self,
        decision: &DecisionSnapshot,
    ) -> Result<String, StorageError> {
        // For simplicity, we'll save decisions as individual snapshots
        let snapshot = Snapshot {
            metadata: decision.metadata.clone(),
            decisions: vec![decision.clone()],
            snapshot_type: SnapshotType::Decision,
        };

        self.save_internal(&snapshot)
    }

    pub fn load_internal(&self, snapshot_id: &str) -> Result<Snapshot, StorageError> {
        let conn_guard = self.conn.lock().unwrap();

        let row: Option<(String,)> = conn_guard
            .query_row(
                "SELECT data_json FROM snapshots WHERE id = ?",
                params![snapshot_id],
                |row| Ok((row.get(0)?,)),
            )
            .optional()
            .map_err(|e| {
                StorageError::ConnectionError(format!("Failed to query snapshot: {}", e))
            })?;

        match row {
            Some((data_json,)) => {
                let snapshot: Snapshot = serde_json::from_str(&data_json)
                    .map_err(|e| StorageError::SerializationError(e.to_string()))?;
                Ok(snapshot)
            }
            None => Err(StorageError::NotFound(format!(
                "Snapshot {} not found",
                snapshot_id
            ))),
        }
    }

    pub fn query_internal(&self, query: SnapshotQuery) -> Result<Vec<Snapshot>, StorageError> {
        let conn_guard = self.conn.lock().unwrap();

        let mut sql = "SELECT data_json FROM snapshots WHERE 1=1".to_string();
        let mut params_vec: Vec<String> = Vec::new();

        // Build WHERE clause
        if let Some(start_time) = query.start_time {
            sql.push_str(" AND created_at >= ?");
            params_vec.push(start_time.format("%Y-%m-%d %H:%M:%S%.3f").to_string());
        }

        if let Some(end_time) = query.end_time {
            sql.push_str(" AND created_at <= ?");
            params_vec.push(end_time.format("%Y-%m-%d %H:%M:%S%.3f").to_string());
        }

        // Add ordering and pagination
        sql.push_str(" ORDER BY created_at DESC");

        if let Some(limit) = query.limit {
            sql.push_str(" LIMIT ?");
            params_vec.push(limit.to_string());
        }

        if let Some(offset) = query.offset {
            sql.push_str(" OFFSET ?");
            params_vec.push(offset.to_string());
        }

        // Execute query
        let mut stmt = conn_guard
            .prepare(&sql)
            .map_err(|e| StorageError::InvalidQuery(format!("Invalid query: {}", e)))?;

        let param_refs: Vec<&dyn rusqlite::ToSql> = params_vec
            .iter()
            .map(|p| p as &dyn rusqlite::ToSql)
            .collect();

        let rows = stmt
            .query_map(param_refs.as_slice(), |row| row.get::<_, String>(0))
            .map_err(|e| StorageError::ConnectionError(format!("Query failed: {}", e)))?;

        let mut snapshots = Vec::new();
        for row in rows {
            let data_json =
                row.map_err(|e| StorageError::ConnectionError(format!("Row error: {}", e)))?;
            let snapshot: Snapshot = serde_json::from_str(&data_json)
                .map_err(|e| StorageError::SerializationError(e.to_string()))?;

            // Apply additional filters that require checking the snapshot content
            if self.matches_query_filters(&snapshot, &query) {
                snapshots.push(snapshot);
            }
        }

        Ok(snapshots)
    }

    fn matches_query_filters(&self, snapshot: &Snapshot, query: &SnapshotQuery) -> bool {
        // Check function name, module name, model name, tags in decisions
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
            return false;
        }

        // No content filters, so it matches
        true
    }
}

#[cfg(feature = "async")]
#[async_trait::async_trait]
impl StorageBackend for SqliteBackend {
    async fn save(&self, snapshot: &Snapshot) -> Result<String, StorageError> {
        let snapshot_clone = snapshot.clone();
        let self_clone = self.clone();

        task::spawn_blocking(move || self_clone.save_internal(&snapshot_clone))
            .await
            .map_err(|e| StorageError::ConnectionError(format!("Task join error: {}", e)))?
    }

    async fn save_decision(&self, decision: &DecisionSnapshot) -> Result<String, StorageError> {
        let decision_clone = decision.clone();
        let self_clone = self.clone();

        task::spawn_blocking(move || self_clone.save_decision_internal(&decision_clone))
            .await
            .map_err(|e| StorageError::ConnectionError(format!("Task join error: {}", e)))?
    }

    async fn load(&self, snapshot_id: &str) -> Result<Snapshot, StorageError> {
        let id = snapshot_id.to_string();
        let self_clone = self.clone();

        task::spawn_blocking(move || self_clone.load_internal(&id))
            .await
            .map_err(|e| StorageError::ConnectionError(format!("Task join error: {}", e)))?
    }

    async fn load_decision(&self, decision_id: &str) -> Result<DecisionSnapshot, StorageError> {
        let snapshot = self.load(decision_id).await?;
        if let Some(decision) = snapshot.decisions.first() {
            Ok(decision.clone())
        } else {
            Err(StorageError::NotFound(format!(
                "Decision {} not found",
                decision_id
            )))
        }
    }

    async fn query(&self, query: SnapshotQuery) -> Result<Vec<Snapshot>, StorageError> {
        let self_clone = self.clone();

        task::spawn_blocking(move || self_clone.query_internal(query))
            .await
            .map_err(|e| StorageError::ConnectionError(format!("Task join error: {}", e)))?
    }

    async fn delete(&self, snapshot_id: &str) -> Result<bool, StorageError> {
        let id = snapshot_id.to_string();
        let self_clone = self.clone();

        task::spawn_blocking(move || {
            let conn_guard = self_clone.conn.lock().unwrap();

            let rows_affected = conn_guard
                .execute("DELETE FROM snapshots WHERE id = ?", params![id])
                .map_err(|e| {
                    StorageError::ConnectionError(format!("Failed to delete snapshot: {}", e))
                })?;

            Ok(rows_affected > 0)
        })
        .await
        .map_err(|e| StorageError::ConnectionError(format!("Task join error: {}", e)))?
    }

    async fn flush(&self) -> Result<FlushResult, StorageError> {
        let self_clone = self.clone();

        task::spawn_blocking(move || {
            let conn_guard = self_clone.conn.lock().unwrap();

            // Force WAL checkpoint
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
        })
        .await
        .map_err(|e| StorageError::ConnectionError(format!("Task join error: {}", e)))?
    }

    async fn health_check(&self) -> Result<bool, StorageError> {
        let self_clone = self.clone();

        task::spawn_blocking(move || {
            let conn_guard = self_clone.conn.lock().unwrap();

            // Simple query to check connection
            let _: i64 = conn_guard
                .query_row("SELECT 1", [], |row| row.get(0))
                .map_err(|e| {
                    StorageError::ConnectionError(format!("Health check failed: {}", e))
                })?;

            Ok(true)
        })
        .await
        .map_err(|e| StorageError::ConnectionError(format!("Task join error: {}", e)))?
    }
}

impl Clone for SqliteBackend {
    fn clone(&self) -> Self {
        Self {
            conn: Arc::clone(&self.conn),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::*;
    use serde_json::json;

    async fn create_test_snapshot() -> Snapshot {
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

    #[tokio::test]
    async fn test_sqlite_in_memory() {
        let backend = SqliteBackend::in_memory().unwrap();
        assert!(backend.health_check().await.unwrap());
    }

    #[tokio::test]
    async fn test_save_and_load_snapshot() {
        let backend = SqliteBackend::in_memory().unwrap();
        let snapshot = create_test_snapshot().await;

        let snapshot_id = backend.save(&snapshot).await.unwrap();
        let loaded_snapshot = backend.load(&snapshot_id).await.unwrap();

        assert_eq!(snapshot.decisions.len(), loaded_snapshot.decisions.len());
        assert_eq!(snapshot.snapshot_type, loaded_snapshot.snapshot_type);
    }

    #[tokio::test]
    async fn test_query_by_function_name() {
        let backend = SqliteBackend::in_memory().unwrap();
        let snapshot = create_test_snapshot().await;
        backend.save(&snapshot).await.unwrap();

        let query = SnapshotQuery::new().with_function_name("test_function");
        let results = backend.query(query).await.unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].decisions[0].function_name, "test_function");
    }

    #[tokio::test]
    async fn test_delete_snapshot() {
        let backend = SqliteBackend::in_memory().unwrap();
        let snapshot = create_test_snapshot().await;

        let snapshot_id = backend.save(&snapshot).await.unwrap();
        assert!(backend.delete(&snapshot_id).await.unwrap());

        let result = backend.load(&snapshot_id).await;
        assert!(matches!(result, Err(StorageError::NotFound(_))));
    }
}
