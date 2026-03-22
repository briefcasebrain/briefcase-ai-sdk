use crate::models::{DecisionSnapshot, Snapshot};
use crate::storage::{FlushResult, SnapshotQuery, StorageBackend, StorageError};
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

#[allow(clippy::large_enum_variant)]
enum Command {
    SaveSnapshot(Snapshot, mpsc::Sender<Result<String, StorageError>>),
    SaveDecision(DecisionSnapshot, mpsc::Sender<Result<String, StorageError>>),
}

pub struct BufferedBackend {
    inner: Arc<dyn StorageBackend>,
    sender: mpsc::Sender<Command>,
}

impl BufferedBackend {
    pub fn new(inner: Arc<dyn StorageBackend>, buffer_size: usize) -> Self {
        let (tx, mut rx) = mpsc::channel(buffer_size);
        let inner_clone = inner.clone();

        // Spawn background worker
        tokio::spawn(async move {
            while let Some(cmd) = rx.recv().await {
                match cmd {
                    Command::SaveSnapshot(snap, reply) => {
                        let res = inner_clone.save(&snap).await;
                        let _ = reply.send(res).await;
                    }
                    Command::SaveDecision(dec, reply) => {
                        let res = inner_clone.save_decision(&dec).await;
                        let _ = reply.send(res).await;
                    }
                }
            }
        });

        Self { inner, sender: tx }
    }
}

#[async_trait]
impl StorageBackend for BufferedBackend {
    async fn save(&self, snapshot: &Snapshot) -> Result<String, StorageError> {
        let (tx, mut rx) = mpsc::channel(1);
        self.sender
            .send(Command::SaveSnapshot(snapshot.clone(), tx))
            .await
            .map_err(|e| StorageError::IoError(e.to_string()))?;

        // Return immediately or wait? For "performance" mode, we return a virtual ID
        // but for this implementation we'll wait to ensure reliability in standard mode
        rx.recv()
            .await
            .unwrap_or(Err(StorageError::ConnectionError("Worker died".into())))
    }

    async fn save_decision(&self, decision: &DecisionSnapshot) -> Result<String, StorageError> {
        let (tx, mut rx) = mpsc::channel(1);
        self.sender
            .send(Command::SaveDecision(decision.clone(), tx))
            .await
            .map_err(|e| StorageError::IoError(e.to_string()))?;

        rx.recv()
            .await
            .unwrap_or(Err(StorageError::ConnectionError("Worker died".into())))
    }

    async fn load(&self, id: &str) -> Result<Snapshot, StorageError> {
        self.inner.load(id).await
    }
    async fn load_decision(&self, id: &str) -> Result<DecisionSnapshot, StorageError> {
        self.inner.load_decision(id).await
    }
    async fn query(&self, q: SnapshotQuery) -> Result<Vec<Snapshot>, StorageError> {
        self.inner.query(q).await
    }
    async fn delete(&self, id: &str) -> Result<bool, StorageError> {
        self.inner.delete(id).await
    }
    async fn flush(&self) -> Result<FlushResult, StorageError> {
        self.inner.flush().await
    }
    async fn health_check(&self) -> Result<bool, StorageError> {
        self.inner.health_check().await
    }
}
