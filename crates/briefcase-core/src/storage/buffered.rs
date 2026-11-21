//! Storage backend that groups decision writes into batches.
//!
//! Decisions accumulate in memory until the batch is full or [`flush`] is
//! called, then go to the wrapped backend in one `save_decisions` round trip.
//! Reads see pending decisions: `load_decision` consults the buffer first, and
//! `load`/`query` write the buffer through before reading, so an id returned
//! by `save_decision` resolves immediately and queries never miss it.
//!
//! Buffering trades durability for throughput: decisions that have not been
//! flushed live only in memory and are lost if the process dies. Call
//! [`flush`] before exit, or construct with `buffer_size` 1 to write through.
//!
//! [`flush`]: BufferedBackend::flush

use crate::models::{DecisionSnapshot, Snapshot};
use crate::storage::{FlushResult, SnapshotQuery, StorageBackend, StorageError};
use async_trait::async_trait;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

pub struct BufferedBackend {
    inner: Arc<dyn StorageBackend>,
    batch_size: usize,
    pending: Mutex<Vec<DecisionSnapshot>>,
}

impl BufferedBackend {
    /// Wrap `inner`, writing through once `batch_size` decisions accumulate.
    /// A `batch_size` of 0 or 1 disables buffering.
    pub fn new(inner: Arc<dyn StorageBackend>, batch_size: usize) -> Self {
        Self {
            inner,
            batch_size: batch_size.max(1),
            pending: Mutex::new(Vec::new()),
        }
    }

    /// Every critical section leaves the buffer consistent, so a poisoned
    /// lock is recovered rather than propagated as a panic.
    fn lock_pending(&self) -> MutexGuard<'_, Vec<DecisionSnapshot>> {
        self.pending.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Decisions accepted but not yet written to the wrapped backend.
    pub fn pending(&self) -> usize {
        self.lock_pending().len()
    }

    /// The backend being buffered, for reads that should bypass the buffer.
    pub fn inner(&self) -> &Arc<dyn StorageBackend> {
        &self.inner
    }

    fn take_pending(&self) -> Vec<DecisionSnapshot> {
        std::mem::take(&mut *self.lock_pending())
    }

    async fn write_batch(&self, batch: Vec<DecisionSnapshot>) -> Result<usize, StorageError> {
        if batch.is_empty() {
            return Ok(0);
        }
        match self.inner.save_decisions(&batch).await {
            Ok(ids) => Ok(ids.len()),
            Err(e) => {
                // Put the batch back so the caller can retry rather than
                // silently losing records to a transient backend failure.
                let mut pending = self.lock_pending();
                let mut restored = batch;
                restored.append(&mut pending);
                *pending = restored;
                Err(e)
            }
        }
    }
}

#[async_trait]
impl StorageBackend for BufferedBackend {
    /// Snapshots are not buffered; they go straight through.
    async fn save(&self, snapshot: &Snapshot) -> Result<String, StorageError> {
        self.inner.save(snapshot).await
    }

    /// Buffer the decision and return its id immediately. The id is the
    /// decision's own snapshot id, so it is valid before the write lands.
    async fn save_decision(&self, decision: &DecisionSnapshot) -> Result<String, StorageError> {
        let id = decision.metadata.snapshot_id.to_string();
        let full = {
            let mut pending = self.lock_pending();
            pending.push(decision.clone());
            pending.len() >= self.batch_size
        };
        if full {
            let batch = self.take_pending();
            self.write_batch(batch).await?;
        }
        Ok(id)
    }

    async fn save_decisions(
        &self,
        decisions: &[DecisionSnapshot],
    ) -> Result<Vec<String>, StorageError> {
        self.inner.save_decisions(decisions).await
    }

    /// Pending decisions are written through first, so a decision saved a
    /// moment ago is loadable as a snapshot like any flushed one.
    async fn load(&self, id: &str) -> Result<Snapshot, StorageError> {
        let batch = self.take_pending();
        self.write_batch(batch).await?;
        self.inner.load(id).await
    }

    async fn load_decision(&self, id: &str) -> Result<DecisionSnapshot, StorageError> {
        let buffered = {
            let pending = self.lock_pending();
            pending
                .iter()
                .find(|d| d.metadata.snapshot_id.to_string() == id)
                .cloned()
        };
        match buffered {
            Some(decision) => Ok(decision),
            None => self.inner.load_decision(id).await,
        }
    }

    /// Pending decisions are written through before the query runs, so the
    /// wrapped backend's filtering, ordering and pagination apply to them too.
    async fn query(&self, q: SnapshotQuery) -> Result<Vec<Snapshot>, StorageError> {
        let batch = self.take_pending();
        self.write_batch(batch).await?;
        self.inner.query(q).await
    }

    async fn delete(&self, id: &str) -> Result<bool, StorageError> {
        self.lock_pending()
            .retain(|d| d.metadata.snapshot_id.to_string() != id);
        self.inner.delete(id).await
    }

    /// Write everything pending, then flush the wrapped backend.
    ///
    /// `snapshots_written` counts the decisions this call wrote. Backends
    /// report their own totals there (`SqliteBackend` returns the table's row
    /// count), so the inner figure is deliberately not carried through.
    async fn flush(&self) -> Result<FlushResult, StorageError> {
        let batch = self.take_pending();
        let written = self.write_batch(batch).await?;
        let inner = self.inner.flush().await?;
        Ok(FlushResult {
            snapshots_written: written,
            bytes_written: inner.bytes_written,
            checkpoint_id: inner.checkpoint_id,
        })
    }

    async fn health_check(&self) -> Result<bool, StorageError> {
        self.inner.health_check().await
    }
}

impl Drop for BufferedBackend {
    fn drop(&mut self) {
        // Drop cannot await, so the batch cannot be written here. Losing audit
        // records in silence is worse than a line on stderr.
        let pending = self.pending.lock().map(|p| p.len()).unwrap_or(0);
        if pending > 0 {
            eprintln!(
                "briefcase: BufferedBackend dropped with {pending} unflushed decision(s); \
                 call flush() before exit to persist them"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::Input;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Counts how many times the wrapped backend was actually written to.
    struct CountingBackend {
        /// Calls that reached the backend, batched or not.
        writes: AtomicUsize,
        /// Decisions durably written across those calls.
        decisions: AtomicUsize,
    }

    impl CountingBackend {
        fn new() -> Self {
            Self {
                writes: AtomicUsize::new(0),
                decisions: AtomicUsize::new(0),
            }
        }
    }

    #[async_trait]
    impl StorageBackend for CountingBackend {
        async fn save(&self, snapshot: &Snapshot) -> Result<String, StorageError> {
            self.writes.fetch_add(1, Ordering::SeqCst);
            self.decisions
                .fetch_add(snapshot.decisions.len().max(1), Ordering::SeqCst);
            Ok(snapshot.metadata.snapshot_id.to_string())
        }
        async fn save_decision(&self, decision: &DecisionSnapshot) -> Result<String, StorageError> {
            self.writes.fetch_add(1, Ordering::SeqCst);
            self.decisions.fetch_add(1, Ordering::SeqCst);
            Ok(decision.metadata.snapshot_id.to_string())
        }
        async fn save_decisions(
            &self,
            decisions: &[DecisionSnapshot],
        ) -> Result<Vec<String>, StorageError> {
            // One call, however many decisions: that is what batching buys.
            self.writes.fetch_add(1, Ordering::SeqCst);
            self.decisions.fetch_add(decisions.len(), Ordering::SeqCst);
            Ok(decisions
                .iter()
                .map(|d| d.metadata.snapshot_id.to_string())
                .collect())
        }
        async fn load(&self, _id: &str) -> Result<Snapshot, StorageError> {
            Err(StorageError::NotFound("load".into()))
        }
        async fn load_decision(&self, _id: &str) -> Result<DecisionSnapshot, StorageError> {
            Err(StorageError::NotFound("load_decision".into()))
        }
        async fn query(&self, _q: SnapshotQuery) -> Result<Vec<Snapshot>, StorageError> {
            Ok(Vec::new())
        }
        async fn delete(&self, _id: &str) -> Result<bool, StorageError> {
            Ok(false)
        }
        async fn flush(&self) -> Result<FlushResult, StorageError> {
            // Backends report their own totals here (SqliteBackend returns the
            // whole table's row count), which must not be added to ours.
            Ok(FlushResult {
                snapshots_written: 99,
                bytes_written: 0,
                checkpoint_id: None,
            })
        }
        async fn health_check(&self) -> Result<bool, StorageError> {
            Ok(true)
        }
    }

    fn decision(n: usize) -> DecisionSnapshot {
        DecisionSnapshot::new(format!("f{n}")).add_input(Input::new(
            "i",
            serde_json::json!(n),
            "int",
        ))
    }

    #[tokio::test]
    async fn buffers_writes_until_the_batch_is_full() {
        let inner = Arc::new(CountingBackend::new());
        let buffered = BufferedBackend::new(inner.clone(), 4);

        for n in 0..3 {
            buffered.save_decision(&decision(n)).await.unwrap();
        }
        assert_eq!(
            inner.writes.load(Ordering::SeqCst),
            0,
            "three decisions under a batch size of four should still be buffered"
        );

        buffered.save_decision(&decision(3)).await.unwrap();
        assert_eq!(
            inner.writes.load(Ordering::SeqCst),
            1,
            "the fourth decision should trigger exactly one batched write"
        );
        assert_eq!(inner.decisions.load(Ordering::SeqCst), 4);
    }

    #[tokio::test]
    async fn flush_writes_a_partial_batch() {
        let inner = Arc::new(CountingBackend::new());
        let buffered = BufferedBackend::new(inner.clone(), 100);

        buffered.save_decision(&decision(0)).await.unwrap();
        buffered.save_decision(&decision(1)).await.unwrap();
        assert_eq!(inner.writes.load(Ordering::SeqCst), 0);

        let result = buffered.flush().await.unwrap();
        assert_eq!(
            result.snapshots_written, 2,
            "flush reports the decisions it wrote, not the backend's own tally"
        );
        assert_eq!(inner.decisions.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn save_decision_returns_the_id_before_the_write_lands() {
        let inner = Arc::new(CountingBackend::new());
        let buffered = BufferedBackend::new(inner.clone(), 100);
        let d = decision(0);
        let expected = d.metadata.snapshot_id.to_string();

        let returned = buffered.save_decision(&d).await.unwrap();

        assert_eq!(
            returned, expected,
            "the caller needs a usable id immediately"
        );
        assert_eq!(inner.writes.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn reads_see_buffered_decisions_before_they_are_flushed() {
        let inner = Arc::new(CountingBackend::new());
        let buffered = BufferedBackend::new(inner.clone(), 100);
        let d = decision(7);
        let id = buffered.save_decision(&d).await.unwrap();

        // The wrapped backend cannot serve this yet, so the buffer must.
        let loaded = buffered.load_decision(&id).await.unwrap();
        assert_eq!(loaded.function_name, "f7");
    }

    #[cfg(feature = "sqlite-storage")]
    #[tokio::test]
    async fn query_and_load_see_unflushed_decisions() {
        let inner = Arc::new(crate::storage::SqliteBackend::in_memory().unwrap());
        let buffered = BufferedBackend::new(inner, 100);
        let id = buffered.save_decision(&decision(1)).await.unwrap();
        buffered.save_decision(&decision(2)).await.unwrap();

        let found = buffered
            .query(SnapshotQuery::new().with_function_name("f1"))
            .await
            .unwrap();
        assert_eq!(found.len(), 1, "a pending decision must be queryable");
        assert_eq!(buffered.query(SnapshotQuery::new()).await.unwrap().len(), 2);

        let id3 = buffered.save_decision(&decision(3)).await.unwrap();
        assert_eq!(buffered.load(&id3).await.unwrap().decisions.len(), 1);
        assert!(buffered.load(&id).await.is_ok());
    }

    #[tokio::test]
    async fn a_batch_size_of_one_writes_through() {
        let inner = Arc::new(CountingBackend::new());
        let buffered = BufferedBackend::new(inner.clone(), 1);
        buffered.save_decision(&decision(0)).await.unwrap();
        assert_eq!(
            inner.decisions.load(Ordering::SeqCst),
            1,
            "buffer_size=1 must not defer anything"
        );
    }

    #[tokio::test]
    async fn pending_count_reports_unflushed_work() {
        let inner = Arc::new(CountingBackend::new());
        let buffered = BufferedBackend::new(inner.clone(), 100);
        buffered.save_decision(&decision(0)).await.unwrap();
        buffered.save_decision(&decision(1)).await.unwrap();
        assert_eq!(buffered.pending(), 2);
        buffered.flush().await.unwrap();
        assert_eq!(buffered.pending(), 0);
    }
}
