#[cfg(feature = "async")]
use super::{FlushResult, StorageBackend};
use super::{SnapshotQuery, StorageError};
use crate::models::{DecisionSnapshot, Snapshot, SnapshotType};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json;
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;
#[cfg(feature = "async")]
use tokio::task;

#[derive(Debug, Clone, PartialEq)]
pub enum CompressionType {
    None,
    Gzip,
}

/// How long a statement waits on a lock held by another connection before
/// failing with SQLITE_BUSY.
const BUSY_TIMEOUT: Duration = Duration::from_secs(5);

/// Format of the `created_at` column. Fixed-width UTC, so string comparison
/// in SQL orders rows chronologically; every write and every range bound must
/// go through [`format_created_at`].
const CREATED_AT_FORMAT: &str = "%Y-%m-%d %H:%M:%S%.3f";

pub(crate) fn format_created_at(timestamp: &chrono::DateTime<chrono::Utc>) -> String {
    timestamp.format(CREATED_AT_FORMAT).to_string()
}

/// Schema migrations, applied in order. `PRAGMA user_version` records how many
/// have run, so each runs exactly once per database; append new ones at the
/// end and never edit or reorder an entry that has shipped.
const MIGRATIONS: &[&str] = &[
    // v1: batch writes used to store `created_at` as RFC 3339 while single
    // writes and range queries used CREATED_AT_FORMAT, so batch-written rows
    // fell outside every time-range query. Rewrite them to the one format.
    r#"UPDATE snapshots
       SET created_at = strftime('%Y-%m-%d %H:%M:%f', created_at)
       WHERE created_at LIKE '%T%'
         AND strftime('%Y-%m-%d %H:%M:%f', created_at) IS NOT NULL"#,
    // v2: tables for the opt-in hash-chained mode (`chain` feature). Empty
    // and unused unless a handle opts in, so plain databases behave exactly
    // as before. The triggers stop ordinary UPDATE/DELETE statements; they
    // are a guard against accidents, not against someone who can drop them
    // (that is what `verify` is for).
    r#"CREATE TABLE IF NOT EXISTS chain_entries (
           seq INTEGER PRIMARY KEY,
           record_id TEXT NOT NULL UNIQUE,
           kind TEXT NOT NULL,
           hash_scheme TEXT NOT NULL,
           record_json TEXT NOT NULL,
           record_hash TEXT NOT NULL,
           prev_hash TEXT NOT NULL UNIQUE,
           entry_hash TEXT NOT NULL,
           recorded_at TEXT NOT NULL
       );
       CREATE TABLE IF NOT EXISTS chain_checkpoints (
           checkpoint_no INTEGER PRIMARY KEY,
           from_seq INTEGER NOT NULL,
           seq INTEGER NOT NULL,
           head_hash TEXT NOT NULL,
           merkle_root TEXT NOT NULL,
           prev_checkpoint_hash TEXT NOT NULL,
           created_at TEXT NOT NULL,
           hash_scheme TEXT NOT NULL,
           key_id TEXT,
           public_key TEXT,
           signature TEXT,
           checkpoint_hash TEXT NOT NULL
       );
       CREATE TABLE IF NOT EXISTS chain_meta (
           key TEXT PRIMARY KEY,
           value TEXT NOT NULL
       );
       CREATE TRIGGER IF NOT EXISTS chain_entries_no_update
           BEFORE UPDATE ON chain_entries
           BEGIN SELECT RAISE(ABORT, 'chain_entries is append-only'); END;
       CREATE TRIGGER IF NOT EXISTS chain_entries_no_delete
           BEFORE DELETE ON chain_entries
           BEGIN SELECT RAISE(ABORT, 'chain_entries is append-only'); END;
       CREATE TRIGGER IF NOT EXISTS chain_checkpoints_no_update
           BEFORE UPDATE ON chain_checkpoints
           BEGIN SELECT RAISE(ABORT, 'chain_checkpoints is append-only'); END;
       CREATE TRIGGER IF NOT EXISTS chain_checkpoints_no_delete
           BEFORE DELETE ON chain_checkpoints
           BEGIN SELECT RAISE(ABORT, 'chain_checkpoints is append-only'); END;"#,
];

pub struct SqliteBackend {
    pub conn: Arc<Mutex<Connection>>,
    /// See [`SqliteBackend::with_append_only`].
    pub(crate) append_only: bool,
    /// Set by [`SqliteBackend::with_chain`].
    #[cfg(feature = "chain")]
    pub(crate) chain: Option<Arc<super::chain::ChainConfig>>,
}

/// The two write statements. `INSERT OR REPLACE` is the default and lets a
/// save overwrite the row holding the same id; a plain `INSERT` fails on the
/// primary key instead, which append-only mode turns into
/// [`StorageError::AlreadyExists`].
const UPSERT_SQL: &str = r#"
            INSERT OR REPLACE INTO snapshots (
                id, snapshot_type, data_json, created_at, created_by, checksum
            ) VALUES (?, ?, ?, ?, ?, ?)
            "#;
pub(crate) const INSERT_ONLY_SQL: &str = r#"
            INSERT INTO snapshots (
                id, snapshot_type, data_json, created_at, created_by, checksum
            ) VALUES (?, ?, ?, ?, ?, ?)
            "#;

/// Map a failed write. A primary-key conflict can only happen under the plain
/// `INSERT` of append-only mode, and it means the id is already stored.
pub(crate) fn insert_error(id: &str, e: rusqlite::Error) -> StorageError {
    if let rusqlite::Error::SqliteFailure(ref code, _) = e {
        if code.extended_code == rusqlite::ffi::SQLITE_CONSTRAINT_PRIMARYKEY {
            return StorageError::AlreadyExists(format!(
                "snapshot {} is already stored and this backend is append-only",
                id
            ));
        }
    }
    StorageError::ConnectionError(format!("Failed to insert snapshot: {}", e))
}

impl SqliteBackend {
    /// Create or open a SQLite database at the given path.
    ///
    /// On Unix a new database file is created owner-only (0600) before SQLite
    /// opens it, so it is never readable by other users at any point; existing
    /// files and WAL siblings are tightened best-effort (a file another owner
    /// shares with this process stays usable even though chmod fails).
    /// Directory permissions are the caller's responsibility.
    pub fn new(path: impl AsRef<Path>) -> Result<Self, StorageError> {
        let path = path.as_ref();
        Self::precreate_owner_only(path);
        let conn = Connection::open(path).map_err(|e| {
            StorageError::ConnectionError(format!("Failed to open database: {}", e))
        })?;

        let backend = Self {
            conn: Arc::new(Mutex::new(conn)),
            append_only: false,
            #[cfg(feature = "chain")]
            chain: None,
        };

        // Run migrations
        {
            let conn_guard = backend.lock_conn();
            Self::run_migrations(&conn_guard)?;
        }

        Self::restrict_file_permissions(path);

        Ok(backend)
    }

    /// Create the database file with mode 0600 if it does not exist yet.
    /// SQLite treats an empty file as an empty database. Failures are left
    /// for Connection::open to surface with its own error.
    #[cfg(unix)]
    fn precreate_owner_only(path: &Path) {
        use std::os::unix::fs::OpenOptionsExt;

        let _ = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path);
    }

    #[cfg(not(unix))]
    fn precreate_owner_only(_path: &Path) {}

    /// Best-effort: restrict the database file and any existing -wal/-shm
    /// siblings to owner-only access. SQLite creates future siblings with the
    /// database file's permissions.
    #[cfg(unix)]
    fn restrict_file_permissions(path: &Path) {
        use std::os::unix::fs::PermissionsExt;

        let mut targets = vec![path.to_path_buf()];
        for suffix in ["-wal", "-shm"] {
            let mut os = path.as_os_str().to_os_string();
            os.push(suffix);
            targets.push(std::path::PathBuf::from(os));
        }

        for target in targets {
            if target.exists() {
                let _ = std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o600));
            }
        }
    }

    #[cfg(not(unix))]
    fn restrict_file_permissions(_path: &Path) {}

    /// Create an in-memory database (for testing)
    pub fn in_memory() -> Result<Self, StorageError> {
        let conn = Connection::open(":memory:").map_err(|e| {
            StorageError::ConnectionError(format!("Failed to create in-memory database: {}", e))
        })?;

        let backend = Self {
            conn: Arc::new(Mutex::new(conn)),
            append_only: false,
            #[cfg(feature = "chain")]
            chain: None,
        };

        // Run migrations
        {
            let conn_guard = backend.lock_conn();
            Self::run_migrations(&conn_guard)?;
        }

        Ok(backend)
    }

    /// Opt in to append-only writes. Off by default, where saving an id that
    /// is already stored replaces the row. When on, such a save fails with
    /// [`StorageError::AlreadyExists`] and leaves the stored row untouched (a
    /// batch save is rejected as a whole), and `delete` fails with
    /// [`StorageError::PermissionDenied`], so a recorded decision cannot be
    /// rewritten through this handle. The flag belongs to the handle, not the
    /// database file: another handle opened without it can still overwrite.
    pub fn with_append_only(mut self, append_only: bool) -> Self {
        self.append_only = append_only;
        self
    }

    /// Whether this handle refuses to overwrite or delete stored snapshots.
    pub fn is_append_only(&self) -> bool {
        self.append_only
    }

    fn write_sql(&self) -> &'static str {
        if self.append_only {
            INSERT_ONLY_SQL
        } else {
            UPSERT_SQL
        }
    }

    /// Lock the connection. A panic in another thread while it held the lock
    /// poisons the mutex, but the connection itself is still usable (SQLite
    /// rolls back the interrupted statement), so recover the guard rather
    /// than turning every later call into a panic.
    pub(crate) fn lock_conn(&self) -> MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Run database migrations
    fn run_migrations(conn: &Connection) -> Result<(), StorageError> {
        conn.busy_timeout(BUSY_TIMEOUT).map_err(|e| {
            StorageError::ConnectionError(format!("Failed to set busy timeout: {}", e))
        })?;

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

        Self::apply_versioned_migrations(conn)
    }

    /// Apply every entry of [`MIGRATIONS`] the database has not seen yet.
    /// Each runs in its own transaction together with the `user_version`
    /// bump, so a failure leaves the database at the last completed version.
    fn apply_versioned_migrations(conn: &Connection) -> Result<(), StorageError> {
        let read_version = |conn: &Connection| -> Result<usize, rusqlite::Error> {
            let version: i64 = conn.pragma_query_value(None, "user_version", |row| row.get(0))?;
            Ok(usize::try_from(version).unwrap_or(0))
        };
        let failed = |version: usize, e: rusqlite::Error| {
            StorageError::ConnectionError(format!(
                "Migration to schema version {} failed: {}",
                version, e
            ))
        };

        let applied = read_version(conn).map_err(|e| failed(0, e))?;
        for (index, sql) in MIGRATIONS.iter().enumerate().skip(applied) {
            let version = index + 1;
            let migrate = || -> Result<(), rusqlite::Error> {
                conn.execute_batch("BEGIN IMMEDIATE")?;
                // Another process may have migrated while this one waited
                // for the write lock; check again now that it is held.
                if read_version(conn)? < version {
                    conn.execute_batch(sql)?;
                    conn.pragma_update(None, "user_version", version as i64)?;
                }
                conn.execute_batch("COMMIT")
            };
            if let Err(e) = migrate() {
                let _ = conn.execute_batch("ROLLBACK");
                return Err(failed(version, e));
            }
        }

        Ok(())
    }

    pub fn save_internal(&self, snapshot: &Snapshot) -> Result<String, StorageError> {
        #[cfg(feature = "chain")]
        if self.chain.is_some() {
            return self
                .save_snapshots_chained(std::slice::from_ref(snapshot))
                .map(|mut ids| ids.remove(0));
        }
        let conn_guard = self.lock_conn();
        let snapshot_id = snapshot.metadata.snapshot_id.to_string();

        let data_json = serde_json::to_string(snapshot)
            .map_err(|e| StorageError::SerializationError(e.to_string()))?;

        conn_guard
            .execute(
                self.write_sql(),
                params![
                    snapshot_id,
                    format!("{:?}", snapshot.snapshot_type),
                    data_json,
                    format_created_at(&snapshot.metadata.timestamp),
                    snapshot.metadata.created_by,
                    snapshot.metadata.checksum,
                ],
            )
            .map_err(|e| insert_error(&snapshot_id, e))?;

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

    /// Write many decisions inside one transaction, so a batch costs one
    /// commit rather than one per decision. Either all rows land or none do.
    pub fn save_decisions_internal(
        &self,
        decisions: &[DecisionSnapshot],
    ) -> Result<Vec<String>, StorageError> {
        #[cfg(feature = "chain")]
        if self.chain.is_some() {
            let snapshots: Vec<Snapshot> = decisions
                .iter()
                .map(|decision| Snapshot {
                    metadata: decision.metadata.clone(),
                    decisions: vec![decision.clone()],
                    snapshot_type: SnapshotType::Decision,
                })
                .collect();
            return self.save_snapshots_chained(&snapshots);
        }
        let write_sql = self.write_sql();
        let mut conn_guard = self.lock_conn();
        let tx = conn_guard
            .transaction()
            .map_err(|e| StorageError::ConnectionError(format!("Failed to begin: {}", e)))?;

        let mut ids = Vec::with_capacity(decisions.len());
        for decision in decisions {
            let snapshot = Snapshot {
                metadata: decision.metadata.clone(),
                decisions: vec![decision.clone()],
                snapshot_type: SnapshotType::Decision,
            };
            let snapshot_id = snapshot.metadata.snapshot_id.to_string();
            let data_json = serde_json::to_string(&snapshot)
                .map_err(|e| StorageError::SerializationError(e.to_string()))?;

            tx.execute(
                write_sql,
                params![
                    snapshot_id,
                    format!("{:?}", snapshot.snapshot_type),
                    data_json,
                    format_created_at(&snapshot.metadata.timestamp),
                    snapshot.metadata.created_by,
                    snapshot.metadata.checksum,
                ],
            )
            .map_err(|e| insert_error(&snapshot_id, e))?;
            ids.push(snapshot_id);
        }

        tx.commit()
            .map_err(|e| StorageError::ConnectionError(format!("Failed to commit: {}", e)))?;
        Ok(ids)
    }

    /// Delete a snapshot by id; `Ok(false)` when no such id is stored.
    /// Refused with `PermissionDenied` in append-only mode.
    pub fn delete_internal(&self, snapshot_id: &str) -> Result<bool, StorageError> {
        if self.append_only {
            return Err(StorageError::PermissionDenied(format!(
                "cannot delete snapshot {}: this backend is append-only",
                snapshot_id
            )));
        }
        let rows_affected = self
            .lock_conn()
            .execute("DELETE FROM snapshots WHERE id = ?", params![snapshot_id])
            .map_err(|e| {
                StorageError::ConnectionError(format!("Failed to delete snapshot: {}", e))
            })?;

        Ok(rows_affected > 0)
    }

    pub fn load_internal(&self, snapshot_id: &str) -> Result<Snapshot, StorageError> {
        let conn_guard = self.lock_conn();

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
        let conn_guard = self.lock_conn();

        let mut sql = "SELECT data_json FROM snapshots WHERE 1=1".to_string();
        let mut params_vec: Vec<String> = Vec::new();

        // Build WHERE clause
        if let Some(start_time) = query.start_time {
            sql.push_str(" AND created_at >= ?");
            params_vec.push(format_created_at(&start_time));
        }

        if let Some(end_time) = query.end_time {
            sql.push_str(" AND created_at <= ?");
            params_vec.push(format_created_at(&end_time));
        }

        // Add ordering and pagination
        sql.push_str(" ORDER BY created_at DESC");

        // Content filters run in Rust after the SQL query; when any are
        // present, pagination also runs in Rust so LIMIT/OFFSET consumes
        // filtered matches instead of raw rows.
        let has_content_filters = query.function_name.is_some()
            || query.module_name.is_some()
            || query.model_name.is_some()
            || query.tags.is_some();

        if !has_content_filters {
            // SQLite only accepts OFFSET after LIMIT; -1 means "no limit".
            match (query.limit, query.offset) {
                (Some(limit), _) => {
                    sql.push_str(" LIMIT ?");
                    params_vec.push(limit.to_string());
                }
                (None, Some(_)) => sql.push_str(" LIMIT -1"),
                (None, None) => {}
            }

            if let Some(offset) = query.offset {
                sql.push_str(" OFFSET ?");
                params_vec.push(offset.to_string());
            }
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

        // With content filters the page is cut in Rust, so the scan stops as
        // soon as it holds offset+limit matches. Without that bound a limited
        // query over a large table would deserialize every row in the range
        // before discarding all but the page.
        let wanted = if has_content_filters {
            query
                .limit
                .map(|limit| limit.saturating_add(query.offset.unwrap_or(0)))
        } else {
            None
        };

        let mut snapshots = Vec::new();
        for row in rows {
            let data_json =
                row.map_err(|e| StorageError::ConnectionError(format!("Row error: {}", e)))?;
            let snapshot: Snapshot = serde_json::from_str(&data_json)
                .map_err(|e| StorageError::SerializationError(e.to_string()))?;

            // Apply additional filters that require checking the snapshot content
            if self.matches_query_filters(&snapshot, &query) {
                snapshots.push(snapshot);
                if wanted.is_some_and(|wanted| snapshots.len() >= wanted) {
                    break;
                }
            }
        }

        if has_content_filters {
            let offset = query.offset.unwrap_or(0);
            if offset >= snapshots.len() {
                return Ok(Vec::new());
            }
            snapshots.drain(..offset);
            if let Some(limit) = query.limit {
                snapshots.truncate(limit);
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

    async fn save_decisions(
        &self,
        decisions: &[DecisionSnapshot],
    ) -> Result<Vec<String>, StorageError> {
        let batch = decisions.to_vec();
        let self_clone = self.clone();

        task::spawn_blocking(move || self_clone.save_decisions_internal(&batch))
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

        task::spawn_blocking(move || self_clone.delete_internal(&id))
            .await
            .map_err(|e| StorageError::ConnectionError(format!("Task join error: {}", e)))?
    }

    async fn flush(&self) -> Result<FlushResult, StorageError> {
        let self_clone = self.clone();

        task::spawn_blocking(move || {
            let conn_guard = self_clone.lock_conn();

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
            let conn_guard = self_clone.lock_conn();

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
            append_only: self.append_only,
            #[cfg(feature = "chain")]
            chain: self.chain.clone(),
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
    async fn test_default_backend_still_replaces_by_id() {
        let backend = SqliteBackend::in_memory().unwrap();
        assert!(!backend.is_append_only());

        let first = DecisionSnapshot::new("original");
        let mut second = DecisionSnapshot::new("rewritten");
        second.metadata.snapshot_id = first.metadata.snapshot_id;

        let id = backend.save_decision(&first).await.unwrap();
        assert_eq!(backend.save_decision(&second).await.unwrap(), id);
        let stored = backend.load_decision(&id).await.unwrap();
        assert_eq!(stored.function_name, "rewritten");
        assert!(backend.delete(&id).await.unwrap());
    }

    #[tokio::test]
    async fn test_append_only_rejects_an_existing_id() {
        let backend = SqliteBackend::in_memory().unwrap().with_append_only(true);
        assert!(backend.is_append_only());
        // Clones share the flag along with the connection.
        assert!(backend.clone().is_append_only());

        let first = DecisionSnapshot::new("original");
        let mut second = DecisionSnapshot::new("rewritten");
        second.metadata.snapshot_id = first.metadata.snapshot_id;

        let id = backend.save_decision(&first).await.unwrap();
        let err = backend.save_decision(&second).await.unwrap_err();
        assert!(matches!(err, StorageError::AlreadyExists(_)), "{err:?}");

        // Same through `save`, and the stored row is untouched.
        let mut snapshot = Snapshot::new(SnapshotType::Session);
        snapshot.metadata.snapshot_id = first.metadata.snapshot_id;
        let err = backend.save(&snapshot).await.unwrap_err();
        assert!(matches!(err, StorageError::AlreadyExists(_)), "{err:?}");
        let stored = backend.load_decision(&id).await.unwrap();
        assert_eq!(stored.function_name, "original");

        // A new id still saves.
        backend
            .save_decision(&DecisionSnapshot::new("another"))
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn test_append_only_rejects_a_batch_as_a_whole() {
        let backend = SqliteBackend::in_memory().unwrap().with_append_only(true);
        let stored = DecisionSnapshot::new("original");
        backend.save_decision(&stored).await.unwrap();

        let fresh = DecisionSnapshot::new("fresh");
        let fresh_id = fresh.metadata.snapshot_id.to_string();
        let mut clash = DecisionSnapshot::new("rewritten");
        clash.metadata.snapshot_id = stored.metadata.snapshot_id;

        let err = backend.save_decisions(&[fresh, clash]).await.unwrap_err();
        assert!(matches!(err, StorageError::AlreadyExists(_)), "{err:?}");
        // The transaction rolled back: the fresh decision did not land.
        assert!(matches!(
            backend.load_decision(&fresh_id).await,
            Err(StorageError::NotFound(_))
        ));
    }

    #[tokio::test]
    async fn test_append_only_refuses_delete() {
        let backend = SqliteBackend::in_memory().unwrap().with_append_only(true);
        let id = backend
            .save_decision(&DecisionSnapshot::new("original"))
            .await
            .unwrap();

        let err = backend.delete(&id).await.unwrap_err();
        assert!(matches!(err, StorageError::PermissionDenied(_)), "{err:?}");
        assert!(backend.load_decision(&id).await.is_ok());
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
    async fn test_query_content_filter_with_pagination_keeps_matches() {
        let backend = SqliteBackend::in_memory().unwrap();

        let make = |fn_name: &str, hours_ago: i64| {
            let mut snapshot = Snapshot::new(SnapshotType::Session);
            snapshot.add_decision(DecisionSnapshot::new(fn_name));
            snapshot.metadata.timestamp = chrono::Utc::now() - chrono::Duration::hours(hours_ago);
            snapshot
        };

        // Only the oldest of three snapshots matches the filter
        backend.save(&make("target", 3)).await.unwrap();
        backend.save(&make("other", 2)).await.unwrap();
        backend.save(&make("other", 1)).await.unwrap();

        let query = SnapshotQuery::new()
            .with_function_name("target")
            .with_limit(2);
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].decisions[0].function_name, "target");

        // Offset applies to filtered matches, not raw rows
        let query = SnapshotQuery::new()
            .with_function_name("target")
            .with_offset(1);
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 0);
    }

    #[tokio::test]
    async fn test_query_content_filter_stops_once_the_page_is_full() {
        // A content-filtered query must stop reading as soon as it has
        // offset+limit matches, rather than deserializing every row in the
        // range and truncating afterwards. The unreadable older row stands in
        // for the rows a large table would otherwise pay to deserialize: if
        // the scan reaches it, the query fails instead of returning the page.
        let backend = SqliteBackend::in_memory().unwrap();

        let make = |hours_ago: i64| {
            let mut snapshot = Snapshot::new(SnapshotType::Session);
            snapshot.add_decision(DecisionSnapshot::new("target"));
            snapshot.metadata.timestamp = chrono::Utc::now() - chrono::Duration::hours(hours_ago);
            snapshot
        };

        backend.save(&make(1)).await.unwrap();
        backend.save(&make(2)).await.unwrap();
        {
            let conn = backend.lock_conn();
            conn.execute(
                "INSERT INTO snapshots (id, snapshot_type, created_at, data_json) \
                 VALUES ('poison', 'Session', '1999-01-01 00:00:00.000', 'not json')",
                [],
            )
            .unwrap();
        }

        let query = SnapshotQuery::new()
            .with_function_name("target")
            .with_limit(2);
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 2);
    }

    #[cfg(unix)]
    #[test]
    fn test_new_creates_owner_only_files() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("decisions.db");
        let _backend = SqliteBackend::new(&db_path).unwrap();

        let mode = std::fs::metadata(&db_path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "db file mode {:o}", mode);

        for suffix in ["-wal", "-shm"] {
            let sibling = dir.path().join(format!("decisions.db{}", suffix));
            if sibling.exists() {
                let mode = std::fs::metadata(&sibling).unwrap().permissions().mode() & 0o777;
                assert_eq!(mode, 0o600, "{} mode {:o}", sibling.display(), mode);
            }
        }
    }

    #[cfg(unix)]
    #[test]
    fn test_new_tightens_existing_loose_file() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("decisions.db");
        drop(SqliteBackend::new(&db_path).unwrap());
        std::fs::set_permissions(&db_path, std::fs::Permissions::from_mode(0o644)).unwrap();

        drop(SqliteBackend::new(&db_path).unwrap());

        let mode = std::fs::metadata(&db_path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "db file mode {:o}", mode);
    }

    #[tokio::test]
    async fn test_batch_saved_decisions_are_found_by_time_range() {
        let backend = SqliteBackend::in_memory().unwrap();
        let single = DecisionSnapshot::new("single");
        let batched = vec![DecisionSnapshot::new("b1"), DecisionSnapshot::new("b2")];

        backend.save_decision(&single).await.unwrap();
        backend.save_decisions(&batched).await.unwrap();

        let now = chrono::Utc::now();
        let query = SnapshotQuery::new().with_time_range(
            now - chrono::Duration::hours(1),
            now + chrono::Duration::hours(1),
        );
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 3, "batch-saved rows must be in range");

        // A range that excludes them must still exclude them.
        let query = SnapshotQuery::new().with_time_range(
            now - chrono::Duration::hours(3),
            now - chrono::Duration::hours(2),
        );
        assert_eq!(backend.query(query).await.unwrap().len(), 0);
    }

    #[test]
    fn test_migration_normalises_legacy_rfc3339_created_at() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("legacy.db");

        let mut snapshot = Snapshot::new(SnapshotType::Decision);
        snapshot.add_decision(DecisionSnapshot::new("legacy"));
        let ts = chrono::Utc::now() - chrono::Duration::minutes(5);
        snapshot.metadata.timestamp = ts;

        // A database written before versioned migrations existed: version 0,
        // with a row in the old batch-write format.
        {
            let backend = SqliteBackend::new(&db_path).unwrap();
            let conn = backend.lock_conn();
            conn.execute(
                "INSERT INTO snapshots (id, snapshot_type, data_json, created_at) \
                 VALUES (?, 'Decision', ?, ?)",
                params![
                    snapshot.metadata.snapshot_id.to_string(),
                    serde_json::to_string(&snapshot).unwrap(),
                    ts.to_rfc3339(),
                ],
            )
            .unwrap();
            conn.pragma_update(None, "user_version", 0).unwrap();
        }

        let backend = SqliteBackend::new(&db_path).unwrap();
        let query = SnapshotQuery::new().with_time_range(
            ts - chrono::Duration::minutes(1),
            ts + chrono::Duration::minutes(1),
        );
        let results = backend.query_internal(query).unwrap();
        assert_eq!(results.len(), 1);

        let conn = backend.lock_conn();
        let stored: String = conn
            .query_row("SELECT created_at FROM snapshots", [], |r| r.get(0))
            .unwrap();
        assert!(!stored.contains('T'), "created_at not normalised: {stored}");
        assert_eq!(&stored[..19], &format_created_at(&ts)[..19]);
        let version: i64 = conn
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .unwrap();
        assert_eq!(version, MIGRATIONS.len() as i64);
    }

    #[tokio::test]
    async fn test_query_offset_without_limit() {
        let backend = SqliteBackend::in_memory().unwrap();
        for _ in 0..3 {
            backend.save(&create_test_snapshot().await).await.unwrap();
        }

        let results = backend
            .query(SnapshotQuery::new().with_offset(1))
            .await
            .unwrap();
        assert_eq!(results.len(), 2);
    }

    #[test]
    fn test_poisoned_lock_does_not_panic_later_calls() {
        let backend = SqliteBackend::in_memory().unwrap();
        let clone = backend.clone();
        let _ = std::thread::spawn(move || {
            let _guard = clone.conn.lock().unwrap();
            panic!("poison the connection mutex");
        })
        .join();

        assert!(backend.conn.is_poisoned());
        let id = backend
            .save_decision_internal(&DecisionSnapshot::new("after_poison"))
            .unwrap();
        assert!(backend.load_internal(&id).is_ok());
    }

    #[test]
    fn test_busy_timeout_is_set() {
        let backend = SqliteBackend::in_memory().unwrap();
        let timeout: i64 = backend
            .lock_conn()
            .pragma_query_value(None, "busy_timeout", |r| r.get(0))
            .unwrap();
        assert_eq!(timeout, BUSY_TIMEOUT.as_millis() as i64);
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
