//! Generic `StorageBackend` adapter for any `VcsProvider`.
//!
//! `VcsStorageBackend<P>` implements the full `StorageBackend` trait by delegating
//! low-level read/write/list/commit operations to a `VcsProvider`. This centralizes:
//!
//! - JSON serialization/deserialization of snapshots and decisions
//! - Object path management (`snapshots/{id}.json`, `decisions/{id}.json`)
//! - Query filtering (function name, module, model, tags, time range)
//! - Batch pending writes with flush-to-commit semantics
//!
//! Each VCS integration only needs to implement `VcsProvider` (~150 lines),
//! and gets the full `StorageBackend` implementation for free.

use super::super::{FlushResult, SnapshotQuery, StorageBackend, StorageError};
use super::provider::VcsProvider;
use crate::models::{DecisionSnapshot, Snapshot};
#[cfg(feature = "async")]
use async_trait::async_trait;
use std::sync::{Arc, Mutex};

/// Generic storage backend adapter that wraps any `VcsProvider`.
///
/// # Example
///
/// ```rust,ignore
/// use briefcase_core::storage::vcs::{VcsStorageBackend, VcsProvider, VcsProviderConfig};
///
/// // Implement VcsProvider for your backend, then:
/// let provider = MyProvider::new(config)?;
/// let backend = VcsStorageBackend::new(provider);
///
/// // Now `backend` implements `StorageBackend`
/// let id = backend.save_decision(&decision).await?;
/// ```
pub struct VcsStorageBackend<P: VcsProvider> {
    provider: Arc<P>,
    pending_writes: Arc<Mutex<Vec<Snapshot>>>,
}

impl<P: VcsProvider> VcsStorageBackend<P> {
    /// Create a new adapter wrapping the given provider.
    pub fn new(provider: P) -> Self {
        Self {
            provider: Arc::new(provider),
            pending_writes: Arc::new(Mutex::new(Vec::new())),
        }
    }

    /// Get a reference to the underlying provider.
    pub fn provider(&self) -> &P {
        &self.provider
    }

    /// Get the number of pending (unflushed) writes.
    pub fn pending_count(&self) -> usize {
        self.pending_writes.lock().unwrap().len()
    }

    // --- Path helpers ---

    fn snapshot_path(snapshot_id: &str) -> String {
        format!("snapshots/{}.json", snapshot_id)
    }

    fn decision_path(decision_id: &str) -> String {
        format!("decisions/{}.json", decision_id)
    }

    // --- Serialization helpers ---

    fn serialize_snapshot(snapshot: &Snapshot) -> Result<Vec<u8>, StorageError> {
        serde_json::to_vec(snapshot).map_err(|e| StorageError::SerializationError(e.to_string()))
    }

    fn deserialize_snapshot(data: &[u8]) -> Result<Snapshot, StorageError> {
        serde_json::from_slice(data).map_err(|e| StorageError::SerializationError(e.to_string()))
    }

    fn serialize_decision(decision: &DecisionSnapshot) -> Result<Vec<u8>, StorageError> {
        serde_json::to_vec(decision).map_err(|e| StorageError::SerializationError(e.to_string()))
    }

    fn deserialize_decision(data: &[u8]) -> Result<DecisionSnapshot, StorageError> {
        serde_json::from_slice(data).map_err(|e| StorageError::SerializationError(e.to_string()))
    }

    // --- Query filtering ---

    /// Check if a snapshot matches the given query filters.
    ///
    /// Applies time range, function name, module name, model name, and tag filters.
    /// Mirrors the filtering logic in `SqliteBackend::matches_query_filters`.
    fn matches_query(snapshot: &Snapshot, query: &SnapshotQuery) -> bool {
        // Time range filtering
        if let Some(ref start) = query.start_time {
            if snapshot.metadata.timestamp < *start {
                return false;
            }
        }
        if let Some(ref end) = query.end_time {
            if snapshot.metadata.timestamp > *end {
                return false;
            }
        }

        // Decision-level filtering: at least one decision must match all criteria
        let has_decision_filters = query.function_name.is_some()
            || query.module_name.is_some()
            || query.model_name.is_some()
            || query.tags.is_some();

        if has_decision_filters {
            let mut any_match = false;

            for decision in &snapshot.decisions {
                let mut this_matches = true;

                if let Some(ref fn_name) = query.function_name {
                    if decision.function_name != *fn_name {
                        this_matches = false;
                    }
                }

                if let Some(ref mod_name) = query.module_name {
                    if decision.module_name.as_ref() != Some(mod_name) {
                        this_matches = false;
                    }
                }

                if let Some(ref model_name) = query.model_name {
                    match &decision.model_parameters {
                        Some(params) if params.model_name == *model_name => {}
                        _ => {
                            this_matches = false;
                        }
                    }
                }

                if let Some(ref query_tags) = query.tags {
                    for (key, value) in query_tags {
                        if decision.tags.get(key) != Some(value) {
                            this_matches = false;
                            break;
                        }
                    }
                }

                if this_matches {
                    any_match = true;
                    break;
                }
            }

            if !any_match {
                return false;
            }
        }

        true
    }
}

#[cfg(feature = "async")]
#[async_trait]
impl<P: VcsProvider + 'static> StorageBackend for VcsStorageBackend<P> {
    async fn save(&self, snapshot: &Snapshot) -> Result<String, StorageError> {
        let snapshot_id = snapshot.metadata.snapshot_id.to_string();

        // Add to pending writes batch (flushed on `flush()`)
        {
            let mut pending = self
                .pending_writes
                .lock()
                .map_err(|e| StorageError::IoError(format!("lock poisoned: {}", e)))?;
            pending.push(snapshot.clone());
        }

        Ok(snapshot_id)
    }

    async fn save_decision(&self, decision: &DecisionSnapshot) -> Result<String, StorageError> {
        let decision_id = decision.metadata.snapshot_id.to_string();
        let path = Self::decision_path(&decision_id);
        let data = Self::serialize_decision(decision)?;
        self.provider.write_object(&path, &data).await?;
        Ok(decision_id)
    }

    async fn load(&self, snapshot_id: &str) -> Result<Snapshot, StorageError> {
        let path = Self::snapshot_path(snapshot_id);
        let data = self.provider.read_object(&path).await?;
        Self::deserialize_snapshot(&data)
    }

    async fn load_decision(&self, decision_id: &str) -> Result<DecisionSnapshot, StorageError> {
        let path = Self::decision_path(decision_id);
        let data = self.provider.read_object(&path).await?;
        Self::deserialize_decision(&data)
    }

    async fn query(&self, query: SnapshotQuery) -> Result<Vec<Snapshot>, StorageError> {
        let paths = self.provider.list_objects("snapshots/").await?;

        let mut results = Vec::new();
        let mut matched_count: usize = 0;
        let offset = query.offset.unwrap_or(0);
        let limit = query.limit.unwrap_or(usize::MAX);

        for path in &paths {
            // Extract snapshot ID from path: "snapshots/abc123.json" -> "abc123"
            let snapshot_id = match path.split('/').next_back().and_then(|f| f.strip_suffix(".json")) {
                Some(id) => id,
                None => continue,
            };

            let snapshot = match self.load(snapshot_id).await {
                Ok(s) => s,
                Err(_) => continue, // Skip corrupted/unreadable snapshots
            };

            if Self::matches_query(&snapshot, &query) {
                if matched_count >= offset {
                    results.push(snapshot);
                    if results.len() >= limit {
                        break;
                    }
                }
                matched_count += 1;
            }
        }

        // Sort by timestamp (newest first)
        results.sort_by(|a, b| b.metadata.timestamp.cmp(&a.metadata.timestamp));

        Ok(results)
    }

    async fn delete(&self, snapshot_id: &str) -> Result<bool, StorageError> {
        let path = Self::snapshot_path(snapshot_id);
        self.provider.delete_object(&path).await
    }

    async fn flush(&self) -> Result<FlushResult, StorageError> {
        let pending = {
            let mut guard = self
                .pending_writes
                .lock()
                .map_err(|e| StorageError::IoError(format!("lock poisoned: {}", e)))?;
            let snapshots = guard.clone();
            guard.clear();
            snapshots
        };

        if pending.is_empty() {
            return Ok(FlushResult {
                snapshots_written: 0,
                bytes_written: 0,
                checkpoint_id: None,
            });
        }

        let mut total_bytes = 0;

        for snapshot in &pending {
            let snapshot_id = snapshot.metadata.snapshot_id.to_string();
            let path = Self::snapshot_path(&snapshot_id);
            let data = Self::serialize_snapshot(snapshot)?;
            total_bytes += data.len();
            self.provider.write_object(&path, &data).await?;
        }

        let message = format!(
            "Briefcase AI flush: {} snapshot(s) via {}",
            pending.len(),
            self.provider.provider_name()
        );
        let version_id = self.provider.create_version(&message).await?;

        Ok(FlushResult {
            snapshots_written: pending.len(),
            bytes_written: total_bytes,
            checkpoint_id: Some(version_id),
        })
    }

    async fn health_check(&self) -> Result<bool, StorageError> {
        self.provider.health_check().await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::*;
    use chrono::Utc;
    use serde_json::json;
    use std::collections::HashMap;
    use std::sync::Mutex as StdMutex;
    use uuid::Uuid;

    // --- Mock Provider for testing the adapter layer ---

    #[derive(Debug)]
    struct MockProvider {
        objects: Arc<StdMutex<HashMap<String, Vec<u8>>>>,
        version_counter: Arc<StdMutex<u64>>,
    }

    impl MockProvider {
        fn new() -> Self {
            Self {
                objects: Arc::new(StdMutex::new(HashMap::new())),
                version_counter: Arc::new(StdMutex::new(0)),
            }
        }
    }

    #[async_trait]
    impl VcsProvider for MockProvider {
        async fn write_object(&self, path: &str, data: &[u8]) -> Result<(), StorageError> {
            self.objects
                .lock()
                .unwrap()
                .insert(path.to_string(), data.to_vec());
            Ok(())
        }

        async fn read_object(&self, path: &str) -> Result<Vec<u8>, StorageError> {
            self.objects
                .lock()
                .unwrap()
                .get(path)
                .cloned()
                .ok_or_else(|| StorageError::NotFound(format!("not found: {}", path)))
        }

        async fn list_objects(&self, prefix: &str) -> Result<Vec<String>, StorageError> {
            let objects = self.objects.lock().unwrap();
            let paths: Vec<String> = objects
                .keys()
                .filter(|k| k.starts_with(prefix))
                .cloned()
                .collect();
            Ok(paths)
        }

        async fn delete_object(&self, path: &str) -> Result<bool, StorageError> {
            Ok(self.objects.lock().unwrap().remove(path).is_some())
        }

        async fn create_version(&self, _message: &str) -> Result<String, StorageError> {
            let mut counter = self.version_counter.lock().unwrap();
            *counter += 1;
            Ok(format!("v{}", counter))
        }

        async fn health_check(&self) -> Result<bool, StorageError> {
            Ok(true)
        }

        fn provider_name(&self) -> &'static str {
            "mock"
        }

        fn config_summary(&self) -> String {
            "Mock provider for testing".to_string()
        }
    }

    // --- Helper to create test snapshots ---

    fn make_test_decision(fn_name: &str, model: &str) -> DecisionSnapshot {
        DecisionSnapshot {
            metadata: SnapshotMetadata {
                snapshot_id: Uuid::new_v4(),
                timestamp: Utc::now(),
                schema_version: "1.0".to_string(),
                sdk_version: "2.1.19".to_string(),
                created_by: Some("test".to_string()),
                checksum: None,
            },
            context: ExecutionContext {
                runtime_version: None,
                dependencies: HashMap::new(),
                random_seed: None,
                environment_variables: HashMap::new(),
                hardware_info: HashMap::new(),
            },
            function_name: fn_name.to_string(),
            module_name: Some("test_module".to_string()),
            inputs: vec![Input::new("query", json!("test"), "string")],
            outputs: vec![Output::new("result", json!("ok"), "string")],
            model_parameters: Some(ModelParameters {
                model_name: model.to_string(),
                model_version: Some("1.0".to_string()),
                provider: Some("test".to_string()),
                parameters: HashMap::new(),
                hyperparameters: HashMap::new(),
                weights_hash: None,
            }),
            execution_time_ms: Some(100.0),
            error: None,
            error_type: None,
            tags: HashMap::new(),
            scorecard: None,
            experiment: None,
            agent: None,
            hardware: None,
        }
    }

    fn make_test_snapshot(decisions: Vec<DecisionSnapshot>) -> Snapshot {
        Snapshot {
            metadata: SnapshotMetadata {
                snapshot_id: Uuid::new_v4(),
                timestamp: Utc::now(),
                schema_version: "1.0".to_string(),
                sdk_version: "2.1.19".to_string(),
                created_by: Some("test".to_string()),
                checksum: None,
            },
            decisions,
            snapshot_type: SnapshotType::Decision,
        }
    }

    // --- Tests ---

    #[tokio::test]
    async fn test_save_decision_and_load() {
        let backend = VcsStorageBackend::new(MockProvider::new());
        let decision = make_test_decision("classify", "gpt-4");

        let id = backend.save_decision(&decision).await.unwrap();
        let loaded = backend.load_decision(&id).await.unwrap();

        assert_eq!(loaded.function_name, "classify");
        assert_eq!(loaded.model_parameters.unwrap().model_name, "gpt-4");
    }

    #[tokio::test]
    async fn test_save_batches_and_flush_writes() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let snap1 = make_test_snapshot(vec![make_test_decision("fn1", "model1")]);
        let snap2 = make_test_snapshot(vec![make_test_decision("fn2", "model2")]);

        // Saves are batched (not yet written to provider)
        let id1 = backend.save(&snap1).await.unwrap();
        let id2 = backend.save(&snap2).await.unwrap();
        assert_eq!(backend.pending_count(), 2);

        // Load should fail before flush
        assert!(backend.load(&id1).await.is_err());

        // Flush writes everything
        let result = backend.flush().await.unwrap();
        assert_eq!(result.snapshots_written, 2);
        assert!(result.checkpoint_id.is_some());
        assert_eq!(backend.pending_count(), 0);

        // Now load succeeds
        let loaded = backend.load(&id1).await.unwrap();
        assert_eq!(loaded.decisions[0].function_name, "fn1");

        let loaded2 = backend.load(&id2).await.unwrap();
        assert_eq!(loaded2.decisions[0].function_name, "fn2");
    }

    #[tokio::test]
    async fn test_flush_empty_is_noop() {
        let backend = VcsStorageBackend::new(MockProvider::new());
        let result = backend.flush().await.unwrap();
        assert_eq!(result.snapshots_written, 0);
        assert_eq!(result.bytes_written, 0);
        assert!(result.checkpoint_id.is_none());
    }

    #[tokio::test]
    async fn test_delete() {
        let backend = VcsStorageBackend::new(MockProvider::new());
        let decision = make_test_decision("fn", "model");
        let snap = make_test_snapshot(vec![decision]);
        let id = snap.metadata.snapshot_id.to_string();

        backend.save(&snap).await.unwrap();
        backend.flush().await.unwrap();

        assert!(backend.load(&id).await.is_ok());
        assert!(backend.delete(&id).await.unwrap());
        assert!(backend.load(&id).await.is_err());

        // Delete non-existent returns false
        assert!(!backend.delete("nonexistent").await.unwrap());
    }

    #[tokio::test]
    async fn test_query_with_function_filter() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let snap1 = make_test_snapshot(vec![make_test_decision("classify", "gpt-4")]);
        let snap2 = make_test_snapshot(vec![make_test_decision("summarize", "claude-3")]);

        backend.save(&snap1).await.unwrap();
        backend.save(&snap2).await.unwrap();
        backend.flush().await.unwrap();

        let query = SnapshotQuery::new().with_function_name("classify");
        let results = backend.query(query).await.unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].decisions[0].function_name, "classify");
    }

    #[tokio::test]
    async fn test_query_with_model_filter() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let snap1 = make_test_snapshot(vec![make_test_decision("fn1", "gpt-4")]);
        let snap2 = make_test_snapshot(vec![make_test_decision("fn2", "claude-3")]);

        backend.save(&snap1).await.unwrap();
        backend.save(&snap2).await.unwrap();
        backend.flush().await.unwrap();

        let query = SnapshotQuery::new().with_model_name("claude-3");
        let results = backend.query(query).await.unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].decisions[0].function_name, "fn2");
    }

    #[tokio::test]
    async fn test_query_with_pagination() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        for i in 0..5 {
            let snap = make_test_snapshot(vec![make_test_decision(&format!("fn{}", i), "model")]);
            backend.save(&snap).await.unwrap();
        }
        backend.flush().await.unwrap();

        let query = SnapshotQuery::new().with_limit(2);
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 2);
    }

    #[tokio::test]
    async fn test_health_check() {
        let backend = VcsStorageBackend::new(MockProvider::new());
        assert!(backend.health_check().await.unwrap());
    }

    #[tokio::test]
    async fn test_load_not_found() {
        let backend = VcsStorageBackend::new(MockProvider::new());
        let result = backend.load("nonexistent").await;
        assert!(matches!(result, Err(StorageError::NotFound(_))));
    }

    // --- Time range filtering tests ---

    fn make_test_decision_with_module(
        fn_name: &str,
        model: &str,
        module: Option<&str>,
    ) -> DecisionSnapshot {
        let mut d = make_test_decision(fn_name, model);
        d.module_name = module.map(|s| s.to_string());
        d
    }

    fn make_test_decision_with_tags(
        fn_name: &str,
        model: &str,
        tags: Vec<(&str, &str)>,
    ) -> DecisionSnapshot {
        let mut d = make_test_decision(fn_name, model);
        d.tags = tags
            .into_iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        d
    }

    fn make_snapshot_at_time(
        decisions: Vec<DecisionSnapshot>,
        timestamp: chrono::DateTime<Utc>,
    ) -> Snapshot {
        Snapshot {
            metadata: SnapshotMetadata {
                snapshot_id: Uuid::new_v4(),
                timestamp,
                schema_version: "1.0".to_string(),
                sdk_version: "2.1.19".to_string(),
                created_by: Some("test".to_string()),
                checksum: None,
            },
            decisions,
            snapshot_type: SnapshotType::Decision,
        }
    }

    #[tokio::test]
    async fn test_query_with_time_range() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let t1 = Utc::now() - chrono::Duration::hours(3);
        let t2 = Utc::now() - chrono::Duration::hours(1);
        let t3 = Utc::now();

        let snap_old = make_snapshot_at_time(vec![make_test_decision("old", "m")], t1);
        let snap_mid = make_snapshot_at_time(vec![make_test_decision("mid", "m")], t2);
        let snap_new = make_snapshot_at_time(vec![make_test_decision("new", "m")], t3);

        backend.save(&snap_old).await.unwrap();
        backend.save(&snap_mid).await.unwrap();
        backend.save(&snap_new).await.unwrap();
        backend.flush().await.unwrap();

        // Query for last 2 hours only
        let query = SnapshotQuery::new().with_time_range(
            Utc::now() - chrono::Duration::hours(2),
            Utc::now() + chrono::Duration::minutes(1),
        );
        let results = backend.query(query).await.unwrap();

        assert_eq!(results.len(), 2);
        // Should not contain the old snapshot
        for r in &results {
            assert_ne!(r.decisions[0].function_name, "old");
        }
    }

    #[tokio::test]
    async fn test_query_with_module_filter() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let snap1 = make_test_snapshot(vec![make_test_decision_with_module(
            "fn1",
            "m",
            Some("billing"),
        )]);
        let snap2 = make_test_snapshot(vec![make_test_decision_with_module(
            "fn2",
            "m",
            Some("auth"),
        )]);
        let snap3 = make_test_snapshot(vec![make_test_decision_with_module(
            "fn3", "m", None, // no module
        )]);

        backend.save(&snap1).await.unwrap();
        backend.save(&snap2).await.unwrap();
        backend.save(&snap3).await.unwrap();
        backend.flush().await.unwrap();

        let query = SnapshotQuery::new().with_module_name("billing");
        let results = backend.query(query).await.unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].decisions[0].function_name, "fn1");
    }

    #[tokio::test]
    async fn test_query_with_tag_filter() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let snap1 = make_test_snapshot(vec![make_test_decision_with_tags(
            "fn1",
            "m",
            vec![("env", "prod"), ("team", "ml")],
        )]);
        let snap2 = make_test_snapshot(vec![make_test_decision_with_tags(
            "fn2",
            "m",
            vec![("env", "staging")],
        )]);
        let snap3 = make_test_snapshot(vec![make_test_decision_with_tags(
            "fn3",
            "m",
            vec![("env", "prod"), ("team", "infra")],
        )]);

        backend.save(&snap1).await.unwrap();
        backend.save(&snap2).await.unwrap();
        backend.save(&snap3).await.unwrap();
        backend.flush().await.unwrap();

        // Filter by env=prod AND team=ml
        let query = SnapshotQuery::new()
            .with_tag("env", "prod")
            .with_tag("team", "ml");
        let results = backend.query(query).await.unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].decisions[0].function_name, "fn1");
    }

    #[tokio::test]
    async fn test_query_with_tag_partial_match_excluded() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        // Snapshot has env=prod but NOT team=ml
        let snap = make_test_snapshot(vec![make_test_decision_with_tags(
            "fn1",
            "m",
            vec![("env", "prod")],
        )]);

        backend.save(&snap).await.unwrap();
        backend.flush().await.unwrap();

        // Query requires both env=prod AND team=ml
        let query = SnapshotQuery::new()
            .with_tag("env", "prod")
            .with_tag("team", "ml");
        let results = backend.query(query).await.unwrap();

        assert_eq!(results.len(), 0);
    }

    #[tokio::test]
    async fn test_query_combined_filters() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let snap1 = make_test_snapshot(vec![make_test_decision_with_tags(
            "classify",
            "gpt-4",
            vec![("env", "prod")],
        )]);
        let snap2 = make_test_snapshot(vec![make_test_decision_with_tags(
            "classify",
            "claude-3",
            vec![("env", "prod")],
        )]);
        let snap3 = make_test_snapshot(vec![make_test_decision_with_tags(
            "summarize",
            "gpt-4",
            vec![("env", "prod")],
        )]);

        backend.save(&snap1).await.unwrap();
        backend.save(&snap2).await.unwrap();
        backend.save(&snap3).await.unwrap();
        backend.flush().await.unwrap();

        // function=classify AND model=gpt-4 AND env=prod
        let query = SnapshotQuery::new()
            .with_function_name("classify")
            .with_model_name("gpt-4")
            .with_tag("env", "prod");
        let results = backend.query(query).await.unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].decisions[0].function_name, "classify");
        assert_eq!(
            results[0].decisions[0]
                .model_parameters
                .as_ref()
                .unwrap()
                .model_name,
            "gpt-4"
        );
    }

    #[tokio::test]
    async fn test_query_offset_and_limit() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        for i in 0..10 {
            let snap = make_test_snapshot(vec![make_test_decision(&format!("fn{}", i), "model")]);
            backend.save(&snap).await.unwrap();
        }
        backend.flush().await.unwrap();

        // offset=3, limit=2 -> should get 2 results starting from the 4th match
        let query = SnapshotQuery::new().with_offset(3).with_limit(2);
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 2);

        // offset beyond total -> empty
        let query = SnapshotQuery::new().with_offset(100).with_limit(5);
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 0);
    }

    #[tokio::test]
    async fn test_query_no_filters_returns_all() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        for i in 0..3 {
            let snap = make_test_snapshot(vec![make_test_decision(&format!("fn{}", i), "model")]);
            backend.save(&snap).await.unwrap();
        }
        backend.flush().await.unwrap();

        let query = SnapshotQuery::new();
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 3);
    }

    #[tokio::test]
    async fn test_query_no_match_returns_empty() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let snap = make_test_snapshot(vec![make_test_decision("classify", "gpt-4")]);
        backend.save(&snap).await.unwrap();
        backend.flush().await.unwrap();

        let query = SnapshotQuery::new().with_function_name("nonexistent_function");
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 0);
    }

    // --- Serialization error tests ---

    #[tokio::test]
    async fn test_load_decision_not_found() {
        let backend = VcsStorageBackend::new(MockProvider::new());
        let result = backend.load_decision("nonexistent").await;
        assert!(matches!(result, Err(StorageError::NotFound(_))));
    }

    #[tokio::test]
    async fn test_flush_bytes_written_accuracy() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let snap = make_test_snapshot(vec![make_test_decision("fn1", "model1")]);
        let expected_bytes = serde_json::to_vec(&snap).unwrap().len();

        backend.save(&snap).await.unwrap();
        let result = backend.flush().await.unwrap();

        assert_eq!(result.snapshots_written, 1);
        assert_eq!(result.bytes_written, expected_bytes);
        assert!(result.checkpoint_id.is_some());
    }

    #[tokio::test]
    async fn test_flush_creates_version_with_provider_name() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let snap = make_test_snapshot(vec![make_test_decision("fn1", "m")]);
        backend.save(&snap).await.unwrap();

        let result = backend.flush().await.unwrap();
        // Version ID should be "v1" from MockProvider's counter
        assert_eq!(result.checkpoint_id.unwrap(), "v1");
    }

    #[tokio::test]
    async fn test_multiple_flushes_increment_version() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let snap1 = make_test_snapshot(vec![make_test_decision("fn1", "m")]);
        backend.save(&snap1).await.unwrap();
        let r1 = backend.flush().await.unwrap();
        assert_eq!(r1.checkpoint_id.unwrap(), "v1");

        let snap2 = make_test_snapshot(vec![make_test_decision("fn2", "m")]);
        backend.save(&snap2).await.unwrap();
        let r2 = backend.flush().await.unwrap();
        assert_eq!(r2.checkpoint_id.unwrap(), "v2");
    }

    // --- Error propagation tests ---

    /// Mock that fails on write
    #[derive(Debug)]
    struct FailingWriteProvider;

    #[async_trait]
    impl VcsProvider for FailingWriteProvider {
        async fn write_object(&self, _path: &str, _data: &[u8]) -> Result<(), StorageError> {
            Err(StorageError::ConnectionError("write failed".into()))
        }
        async fn read_object(&self, _path: &str) -> Result<Vec<u8>, StorageError> {
            Err(StorageError::NotFound("not found".into()))
        }
        async fn list_objects(&self, _prefix: &str) -> Result<Vec<String>, StorageError> {
            Ok(vec![])
        }
        async fn delete_object(&self, _path: &str) -> Result<bool, StorageError> {
            Ok(false)
        }
        async fn create_version(&self, _message: &str) -> Result<String, StorageError> {
            Ok("v1".to_string())
        }
        async fn health_check(&self) -> Result<bool, StorageError> {
            Err(StorageError::ConnectionError("unhealthy".into()))
        }
        fn provider_name(&self) -> &'static str {
            "failing"
        }
        fn config_summary(&self) -> String {
            "Failing provider".to_string()
        }
    }

    #[tokio::test]
    async fn test_flush_propagates_write_error() {
        let backend = VcsStorageBackend::new(FailingWriteProvider);

        let snap = make_test_snapshot(vec![make_test_decision("fn1", "m")]);
        backend.save(&snap).await.unwrap(); // save succeeds (just batches)

        let result = backend.flush().await;
        assert!(result.is_err());
        assert!(matches!(result, Err(StorageError::ConnectionError(_))));
    }

    #[tokio::test]
    async fn test_save_decision_propagates_write_error() {
        let backend = VcsStorageBackend::new(FailingWriteProvider);
        let decision = make_test_decision("fn1", "m");

        let result = backend.save_decision(&decision).await;
        assert!(result.is_err());
        assert!(matches!(result, Err(StorageError::ConnectionError(_))));
    }

    #[tokio::test]
    async fn test_health_check_propagates_error() {
        let backend = VcsStorageBackend::new(FailingWriteProvider);
        let result = backend.health_check().await;
        assert!(result.is_err());
        assert!(matches!(result, Err(StorageError::ConnectionError(_))));
    }

    // --- Path construction tests ---

    #[test]
    fn test_snapshot_path_format() {
        let path = VcsStorageBackend::<MockProvider>::snapshot_path("abc123");
        assert_eq!(path, "snapshots/abc123.json");
    }

    #[test]
    fn test_decision_path_format() {
        let path = VcsStorageBackend::<MockProvider>::decision_path("def456");
        assert_eq!(path, "decisions/def456.json");
    }

    // --- Multi-decision snapshot tests ---

    #[tokio::test]
    async fn test_query_matches_any_decision_in_snapshot() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        // Snapshot with TWO decisions  one matches, one doesn't
        let snap = make_test_snapshot(vec![
            make_test_decision("classify", "gpt-4"),
            make_test_decision("summarize", "claude-3"),
        ]);

        backend.save(&snap).await.unwrap();
        backend.flush().await.unwrap();

        // Should match because one decision has function_name="classify"
        let query = SnapshotQuery::new().with_function_name("classify");
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 1);

        // Should also match on the other decision
        let query = SnapshotQuery::new().with_function_name("summarize");
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 1);

        // Should NOT match a function that doesn't exist in any decision
        let query = SnapshotQuery::new().with_function_name("translate");
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 0);
    }

    #[tokio::test]
    async fn test_decision_without_model_params_excluded_by_model_filter() {
        let backend = VcsStorageBackend::new(MockProvider::new());

        let mut decision = make_test_decision("fn1", "m");
        decision.model_parameters = None; // No model params

        let snap = make_test_snapshot(vec![decision]);
        backend.save(&snap).await.unwrap();
        backend.flush().await.unwrap();

        let query = SnapshotQuery::new().with_model_name("any-model");
        let results = backend.query(query).await.unwrap();
        assert_eq!(results.len(), 0);
    }

    #[tokio::test]
    async fn test_pending_count_after_operations() {
        let backend = VcsStorageBackend::new(MockProvider::new());
        assert_eq!(backend.pending_count(), 0);

        let snap = make_test_snapshot(vec![make_test_decision("fn1", "m")]);
        backend.save(&snap).await.unwrap();
        assert_eq!(backend.pending_count(), 1);

        backend.save(&snap).await.unwrap();
        assert_eq!(backend.pending_count(), 2);

        backend.flush().await.unwrap();
        assert_eq!(backend.pending_count(), 0);
    }
}
