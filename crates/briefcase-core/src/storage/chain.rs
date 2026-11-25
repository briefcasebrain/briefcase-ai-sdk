//! Tamper-evident mode for [`SqliteBackend`]: an append-only hash chain with
//! signed Merkle checkpoints.
//!
//! Opt in with [`SqliteBackend::with_chain`]. Every saved decision (or any
//! JSON record passed to [`SqliteBackend::append_record`]) becomes one row of
//! `chain_entries`, written in the same `BEGIN IMMEDIATE` transaction as the
//! snapshot itself:
//!
//! ```text
//! record_hash = sha256(jcs(record))
//! entry_hash  = sha256(jcs({"v":1, "hash_scheme":"jcs-1", "seq":n,
//!                           "record_id":id, "kind":k, "prev_hash":h(n-1),
//!                           "record_hash":r, "recorded_at":t}))
//! ```
//!
//! `jcs` is RFC 8785 ([`crate::integrity::canonical_json_v2`]), so the
//! Python and TypeScript verifiers recompute the same bytes. `prev_hash` of
//! seq 1 is 64 zeros. The immediate transaction takes SQLite's write lock
//! before the tail is read, so writers in different processes queue instead
//! of forking the chain; a UNIQUE index on `prev_hash` backs that up.
//!
//! Every `checkpoint_every` entries (and on demand, [`SqliteBackend::checkpoint`])
//! a checkpoint row records the head, a Merkle root over the entries since
//! the previous checkpoint (see [`crate::integrity::merkle`]), the previous
//! checkpoint's hash and, when a key is configured, an Ed25519 signature over
//! the checkpoint hash. One entry's inclusion can then be proven with a
//! log-sized audit path instead of the whole log.
//!
//! What [`SqliteBackend::verify_chain`] can and cannot see: edits, deletions,
//! reordering and insertions anywhere in the chain, and tampering with
//! checkpoints, are detected. Removing the newest entries (truncation) after
//! the last checkpoint, or a writer holding the signing key rewriting the
//! whole log, is only detectable against a checkpoint kept outside the
//! database: pass it as `against_checkpoint`.

use super::sqlite::{format_created_at, insert_error, SqliteBackend, INSERT_ONLY_SQL};
use super::StorageError;
use crate::integrity::merkle::{self, Hash};
use crate::integrity::signing::{self, SigningKey};
use crate::integrity::{
    canonical_json_v2, parse_canonical_text, sha256_hex, HashScheme, HASH_SCHEME_JCS1,
};
use crate::models::{Snapshot, SnapshotType};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::Arc;

/// `prev_hash` of the first entry and `prev_checkpoint_hash` of the first
/// checkpoint.
pub const GENESIS_HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";

/// Default checkpoint cadence: one signed checkpoint per 100 entries.
pub const DEFAULT_CHECKPOINT_EVERY: u64 = 100;

/// Options for [`SqliteBackend::with_chain`].
#[derive(Debug, Clone)]
pub struct ChainConfig {
    /// Write a checkpoint automatically after this many new entries; 0 means
    /// only when [`SqliteBackend::checkpoint`] is called.
    pub checkpoint_every: u64,
    /// Key that signs checkpoints. Without one, checkpoints carry hashes
    /// only, which proves internal consistency but not who wrote them.
    pub signing_key: Option<SigningKey>,
}

impl Default for ChainConfig {
    fn default() -> Self {
        Self {
            checkpoint_every: DEFAULT_CHECKPOINT_EVERY,
            signing_key: None,
        }
    }
}

impl ChainConfig {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn with_checkpoint_every(mut self, every: u64) -> Self {
        self.checkpoint_every = every;
        self
    }
    pub fn with_signing_key(mut self, key: SigningKey) -> Self {
        self.signing_key = Some(key);
        self
    }
}

/// One link of the chain (the record itself is stored next to it).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ChainEntry {
    pub seq: u64,
    pub record_id: String,
    pub kind: String,
    pub hash_scheme: String,
    pub prev_hash: String,
    pub record_hash: String,
    pub entry_hash: String,
    pub recorded_at: String,
}

/// A checkpoint over entries `from_seq..=seq`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Checkpoint {
    pub checkpoint_no: u64,
    pub from_seq: u64,
    pub seq: u64,
    pub head_hash: String,
    pub merkle_root: String,
    pub prev_checkpoint_hash: String,
    pub created_at: String,
    pub hash_scheme: String,
    pub key_id: Option<String>,
    /// JWK `x` (base64url) of the signing key.
    pub public_key: Option<String>,
    /// base64url Ed25519 signature over the raw bytes of `checkpoint_hash`.
    pub signature: Option<String>,
    pub checkpoint_hash: String,
}

impl Checkpoint {
    /// The signed fields. `public_key` and `signature` are not part of it:
    /// `key_id` (the key's thumbprint) binds the key instead.
    pub fn body(&self) -> Value {
        json!({
            "v": 1,
            "hash_scheme": self.hash_scheme,
            "checkpoint_no": self.checkpoint_no,
            "from_seq": self.from_seq,
            "seq": self.seq,
            "head_hash": self.head_hash,
            "merkle_root": self.merkle_root,
            "prev_checkpoint_hash": self.prev_checkpoint_hash,
            "created_at": self.created_at,
            "key_id": self.key_id,
        })
    }

    /// Recompute `checkpoint_hash` from the body.
    pub fn compute_hash(&self) -> Result<String, StorageError> {
        let text = canonical_json_v2(&self.body())
            .map_err(|e| StorageError::SerializationError(e.to_string()))?;
        Ok(sha256_hex(text.as_bytes()))
    }
}

/// Proof that entry `seq` is leaf `leaf_index` of checkpoint `checkpoint_no`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct InclusionProof {
    pub seq: u64,
    pub entry_hash: String,
    pub checkpoint_no: u64,
    pub leaf_index: u64,
    pub tree_size: u64,
    pub audit_path: Vec<String>,
    pub merkle_root: String,
}

/// One finding of [`SqliteBackend::verify_chain`].
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Problem {
    /// Stable machine-readable kind, e.g. `broken_link`, `modified_entry`.
    pub kind: String,
    pub seq: Option<u64>,
    pub detail: String,
}

/// Result of [`SqliteBackend::verify_chain`].
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct VerifyReport {
    pub intact: bool,
    pub entries_checked: u64,
    pub first_seq: Option<u64>,
    pub last_seq: Option<u64>,
    pub head_hash: Option<String>,
    pub checkpoints_checked: u64,
    pub signed_checkpoints: u64,
    pub signatures_verified: u64,
    pub last_checkpoint_seq: Option<u64>,
    pub entries_after_last_checkpoint: u64,
    pub key_ids: Vec<String>,
    pub hash_schemes: Vec<String>,
    pub first_broken_seq: Option<u64>,
    pub missing_ranges: Vec<(u64, u64)>,
    pub modified_seqs: Vec<u64>,
    pub anchored: bool,
    pub problems: Vec<Problem>,
    pub notes: Vec<String>,
}

impl VerifyReport {
    fn problem(&mut self, kind: &str, seq: Option<u64>, detail: String) {
        if let Some(s) = seq {
            if matches!(
                kind,
                "broken_link"
                    | "modified_entry"
                    | "missing_entries"
                    | "snapshot_modified"
                    | "snapshot_missing"
                    | "unknown_hash_scheme"
            ) {
                self.first_broken_seq = Some(self.first_broken_seq.map_or(s, |f| f.min(s)));
            }
            if matches!(kind, "modified_entry" | "snapshot_modified")
                && !self.modified_seqs.contains(&s)
            {
                self.modified_seqs.push(s);
            }
        }
        self.problems.push(Problem {
            kind: kind.to_string(),
            seq,
            detail,
        });
    }
}

/// Options for [`SqliteBackend::verify_chain`].
#[derive(Debug, Clone, Default)]
pub struct VerifyOptions {
    /// A checkpoint held outside the database (printed, emailed, stored in a
    /// WORM bucket). The log must still contain everything it covers,
    /// unchanged; this is the only way to detect truncation after the last
    /// checkpoint in the database.
    pub against_checkpoint: Option<Checkpoint>,
    /// Require every checkpoint to be signed by the key with this thumbprint.
    /// Without it a verifier trusts whatever key the database names.
    pub expected_key_id: Option<String>,
}

fn now_rfc3339() -> String {
    chrono::Utc::now()
        .format("%Y-%m-%dT%H:%M:%S%.6fZ")
        .to_string()
}

fn conn_err(context: &str) -> impl Fn(rusqlite::Error) -> StorageError + '_ {
    move |e| StorageError::ConnectionError(format!("{}: {}", context, e))
}

fn ser_err(e: impl std::fmt::Display) -> StorageError {
    StorageError::SerializationError(e.to_string())
}

/// `entry_hash` of a chain entry (see the module docs).
#[allow(clippy::too_many_arguments)]
pub fn compute_entry_hash(
    hash_scheme: &str,
    seq: u64,
    record_id: &str,
    kind: &str,
    prev_hash: &str,
    record_hash: &str,
    recorded_at: &str,
) -> Result<String, StorageError> {
    if HashScheme::parse(hash_scheme) != Some(HashScheme::Jcs1) {
        return Err(StorageError::SerializationError(format!(
            "chain entries use hash scheme {}; {:?} is not supported here",
            HASH_SCHEME_JCS1, hash_scheme
        )));
    }
    let body = json!({
        "v": 1,
        "hash_scheme": hash_scheme,
        "seq": seq,
        "record_id": record_id,
        "kind": kind,
        "prev_hash": prev_hash,
        "record_hash": record_hash,
        "recorded_at": recorded_at,
    });
    Ok(sha256_hex(
        canonical_json_v2(&body).map_err(ser_err)?.as_bytes(),
    ))
}

fn read_entry(row: &rusqlite::Row<'_>) -> rusqlite::Result<(ChainEntry, String)> {
    Ok((
        ChainEntry {
            seq: row.get::<_, i64>(0)? as u64,
            record_id: row.get(1)?,
            kind: row.get(2)?,
            hash_scheme: row.get(3)?,
            prev_hash: row.get(5)?,
            record_hash: row.get(6)?,
            entry_hash: row.get(7)?,
            recorded_at: row.get(8)?,
        },
        row.get(4)?,
    ))
}

const ENTRY_COLUMNS: &str =
    "seq, record_id, kind, hash_scheme, record_json, prev_hash, record_hash, entry_hash, recorded_at";
const CHECKPOINT_COLUMNS: &str = "checkpoint_no, from_seq, seq, head_hash, merkle_root, \
     prev_checkpoint_hash, created_at, hash_scheme, key_id, public_key, signature, checkpoint_hash";

fn read_checkpoint(row: &rusqlite::Row<'_>) -> rusqlite::Result<Checkpoint> {
    Ok(Checkpoint {
        checkpoint_no: row.get::<_, i64>(0)? as u64,
        from_seq: row.get::<_, i64>(1)? as u64,
        seq: row.get::<_, i64>(2)? as u64,
        head_hash: row.get(3)?,
        merkle_root: row.get(4)?,
        prev_checkpoint_hash: row.get(5)?,
        created_at: row.get(6)?,
        hash_scheme: row.get(7)?,
        key_id: row.get(8)?,
        public_key: row.get(9)?,
        signature: row.get(10)?,
        checkpoint_hash: row.get(11)?,
    })
}

fn last_checkpoint(conn: &Connection) -> Result<Option<Checkpoint>, StorageError> {
    conn.query_row(
        &format!(
            "SELECT {} FROM chain_checkpoints ORDER BY checkpoint_no DESC LIMIT 1",
            CHECKPOINT_COLUMNS
        ),
        [],
        read_checkpoint,
    )
    .optional()
    .map_err(conn_err("Failed to read the last checkpoint"))
}

fn tail(conn: &Connection) -> Result<(u64, String), StorageError> {
    Ok(conn
        .query_row(
            "SELECT seq, entry_hash FROM chain_entries ORDER BY seq DESC LIMIT 1",
            [],
            |row| Ok((row.get::<_, i64>(0)? as u64, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(conn_err("Failed to read the chain tail"))?
        .unwrap_or((0, GENESIS_HASH.to_string())))
}

fn leaves_between(
    conn: &Connection,
    from: u64,
    to: u64,
) -> Result<Vec<(u64, String)>, StorageError> {
    let mut stmt = conn
        .prepare("SELECT seq, entry_hash FROM chain_entries WHERE seq BETWEEN ? AND ? ORDER BY seq")
        .map_err(conn_err("Failed to read entries"))?;
    let rows = stmt
        .query_map(params![from as i64, to as i64], |row| {
            Ok((row.get::<_, i64>(0)? as u64, row.get::<_, String>(1)?))
        })
        .map_err(conn_err("Failed to read entries"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(conn_err("Failed to read entries"))
}

fn to_leaves(hashes: &[(u64, String)]) -> Option<Vec<Hash>> {
    hashes
        .iter()
        .map(|(_, h)| merkle::hash_from_hex(h))
        .collect()
}

/// Append one entry inside an open transaction.
fn append_in_tx(
    conn: &Connection,
    record_id: &str,
    kind: &str,
    record: &Value,
) -> Result<ChainEntry, StorageError> {
    let record_json = canonical_json_v2(record).map_err(ser_err)?;
    let record_hash = sha256_hex(record_json.as_bytes());
    let (last_seq, prev_hash) = tail(conn)?;
    let seq = last_seq + 1;
    let recorded_at = now_rfc3339();
    let entry_hash = compute_entry_hash(
        HASH_SCHEME_JCS1,
        seq,
        record_id,
        kind,
        &prev_hash,
        &record_hash,
        &recorded_at,
    )?;
    conn.execute(
        &format!(
            "INSERT INTO chain_entries ({}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ENTRY_COLUMNS
        ),
        params![
            seq as i64,
            record_id,
            kind,
            HASH_SCHEME_JCS1,
            record_json,
            prev_hash,
            record_hash,
            entry_hash,
            recorded_at
        ],
    )
    .map_err(|e| match e {
        rusqlite::Error::SqliteFailure(ref code, _)
            if code.extended_code == rusqlite::ffi::SQLITE_CONSTRAINT_UNIQUE =>
        {
            StorageError::AlreadyExists(format!(
                "record {} is already in the chain (chained stores are append-only)",
                record_id
            ))
        }
        other => StorageError::ConnectionError(format!("Failed to append chain entry: {}", other)),
    })?;
    Ok(ChainEntry {
        seq,
        record_id: record_id.to_string(),
        kind: kind.to_string(),
        hash_scheme: HASH_SCHEME_JCS1.to_string(),
        prev_hash,
        record_hash,
        entry_hash,
        recorded_at,
    })
}

/// Write a checkpoint over everything since the last one, inside an open
/// transaction. Returns the last checkpoint unchanged when nothing is new.
fn checkpoint_in_tx(
    conn: &Connection,
    key: Option<&SigningKey>,
) -> Result<Option<Checkpoint>, StorageError> {
    let previous = last_checkpoint(conn)?;
    let from_seq = previous.as_ref().map_or(1, |c| c.seq + 1);
    let (to_seq, head_hash) = tail(conn)?;
    if to_seq < from_seq {
        return Ok(previous);
    }
    let hashes = leaves_between(conn, from_seq, to_seq)?;
    if hashes.len() as u64 != to_seq - from_seq + 1 {
        return Err(StorageError::ConnectionError(format!(
            "cannot checkpoint: entries {}..{} are not contiguous (run verify)",
            from_seq, to_seq
        )));
    }
    let leaves = to_leaves(&hashes)
        .ok_or_else(|| StorageError::SerializationError("malformed entry hash".into()))?;
    let mut checkpoint = Checkpoint {
        checkpoint_no: previous.as_ref().map_or(1, |c| c.checkpoint_no + 1),
        from_seq,
        seq: to_seq,
        head_hash,
        merkle_root: merkle::hash_to_hex(&merkle::merkle_root(&leaves)),
        prev_checkpoint_hash: previous
            .as_ref()
            .map_or(GENESIS_HASH.to_string(), |c| c.checkpoint_hash.clone()),
        created_at: now_rfc3339(),
        hash_scheme: HASH_SCHEME_JCS1.to_string(),
        key_id: key.map(|k| k.key_id()),
        public_key: key.map(|k| k.public_x()),
        signature: None,
        checkpoint_hash: String::new(),
    };
    checkpoint.checkpoint_hash = checkpoint.compute_hash()?;
    if let Some(key) = key {
        checkpoint.signature = Some(
            key.sign_digest(&checkpoint.checkpoint_hash)
                .map_err(|e| StorageError::SerializationError(e.to_string()))?,
        );
    }
    conn.execute(
        &format!(
            "INSERT INTO chain_checkpoints ({}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            CHECKPOINT_COLUMNS
        ),
        params![
            checkpoint.checkpoint_no as i64,
            checkpoint.from_seq as i64,
            checkpoint.seq as i64,
            checkpoint.head_hash,
            checkpoint.merkle_root,
            checkpoint.prev_checkpoint_hash,
            checkpoint.created_at,
            checkpoint.hash_scheme,
            checkpoint.key_id,
            checkpoint.public_key,
            checkpoint.signature,
            checkpoint.checkpoint_hash,
        ],
    )
    .map_err(conn_err("Failed to write checkpoint"))?;
    Ok(Some(checkpoint))
}

fn kind_of(snapshot: &Snapshot) -> &'static str {
    match snapshot.snapshot_type {
        SnapshotType::Decision => "decision",
        _ => "snapshot",
    }
}

impl SqliteBackend {
    /// Turn on the tamper-evident mode for this handle (and implies
    /// append-only: overwrites and deletes are refused).
    ///
    /// On a database that already holds snapshots but no chain, those rows
    /// are adopted into the chain first, oldest first; they are provably
    /// unaltered only from that moment on. Plain handles opened without
    /// `with_chain` still work on the same file, but anything they write is
    /// outside the chain and `verify_chain` reports it.
    pub fn with_chain(mut self, config: ChainConfig) -> Result<Self, StorageError> {
        {
            let mut guard = self.lock_conn();
            let tx = guard
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(conn_err("Failed to begin"))?;
            let started: Option<String> = tx
                .query_row(
                    "SELECT value FROM chain_meta WHERE key = 'created_at'",
                    [],
                    |row| row.get(0),
                )
                .optional()
                .map_err(conn_err("Failed to read chain metadata"))?;
            if started.is_none() {
                let created_at = now_rfc3339();
                let mut stmt = tx
                    .prepare(
                        "SELECT id, data_json FROM snapshots \
                         WHERE id NOT IN (SELECT record_id FROM chain_entries) \
                         ORDER BY created_at, id",
                    )
                    .map_err(conn_err("Failed to read snapshots"))?;
                let existing: Vec<(String, String)> = stmt
                    .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
                    .map_err(conn_err("Failed to read snapshots"))?
                    .collect::<Result<_, _>>()
                    .map_err(conn_err("Failed to read snapshots"))?;
                drop(stmt);
                for (id, data_json) in &existing {
                    let value: Value = serde_json::from_str(data_json).map_err(ser_err)?;
                    let kind = serde_json::from_value::<Snapshot>(value.clone())
                        .map(|s| kind_of(&s))
                        .unwrap_or("snapshot");
                    append_in_tx(&tx, id, kind, &value)?;
                }
                for (key, value) in [
                    ("created_at", created_at),
                    ("hash_scheme", HASH_SCHEME_JCS1.to_string()),
                    ("adopted_rows", existing.len().to_string()),
                ] {
                    tx.execute(
                        "INSERT OR IGNORE INTO chain_meta (key, value) VALUES (?, ?)",
                        params![key, value],
                    )
                    .map_err(conn_err("Failed to write chain metadata"))?;
                }
            }
            tx.commit().map_err(conn_err("Failed to commit"))?;
        }
        self.append_only = true;
        self.chain = Some(Arc::new(config));
        Ok(self)
    }

    /// Whether this handle writes through the hash chain.
    pub fn is_chained(&self) -> bool {
        self.chain.is_some()
    }

    fn chain_config(&self) -> Result<Arc<ChainConfig>, StorageError> {
        self.chain.clone().ok_or_else(|| {
            StorageError::InvalidQuery(
                "this backend is not chained; open it with with_chain(...)".into(),
            )
        })
    }

    /// Run `f` in one immediate transaction, then the checkpoint cadence.
    fn in_chain_tx<T>(
        &self,
        f: impl FnOnce(&Connection) -> Result<T, StorageError>,
    ) -> Result<T, StorageError> {
        let config = self.chain_config()?;
        let mut guard = self.lock_conn();
        let tx = guard
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(conn_err("Failed to begin"))?;
        let out = f(&tx)?;
        if config.checkpoint_every > 0 {
            let last = last_checkpoint(&tx)?.map_or(0, |c| c.seq);
            let (head, _) = tail(&tx)?;
            if head - last >= config.checkpoint_every {
                checkpoint_in_tx(&tx, config.signing_key.as_ref())?;
            }
        }
        tx.commit().map_err(conn_err("Failed to commit"))?;
        Ok(out)
    }

    pub(crate) fn save_snapshots_chained(
        &self,
        snapshots: &[Snapshot],
    ) -> Result<Vec<String>, StorageError> {
        self.in_chain_tx(|tx| {
            let mut ids = Vec::with_capacity(snapshots.len());
            for snapshot in snapshots {
                let id = snapshot.metadata.snapshot_id.to_string();
                let data_json = serde_json::to_string(snapshot).map_err(ser_err)?;
                tx.execute(
                    INSERT_ONLY_SQL,
                    params![
                        id,
                        format!("{:?}", snapshot.snapshot_type),
                        data_json,
                        format_created_at(&snapshot.metadata.timestamp),
                        snapshot.metadata.created_by,
                        snapshot.metadata.checksum,
                    ],
                )
                .map_err(|e| insert_error(&id, e))?;
                let value: Value = serde_json::to_value(snapshot).map_err(ser_err)?;
                append_in_tx(tx, &id, kind_of(snapshot), &value)?;
                ids.push(id);
            }
            Ok(ids)
        })
    }

    /// Append any JSON record (for example a Python `@capture` record) under
    /// `record_id`. The record is stored only in the chain, canonicalized.
    pub fn append_record(
        &self,
        record_id: &str,
        record: &Value,
    ) -> Result<ChainEntry, StorageError> {
        self.in_chain_tx(|tx| append_in_tx(tx, record_id, "record", record))
    }

    /// Append several records in one transaction (all or none).
    pub fn append_records(
        &self,
        records: &[(String, Value)],
    ) -> Result<Vec<ChainEntry>, StorageError> {
        self.in_chain_tx(|tx| {
            records
                .iter()
                .map(|(id, record)| append_in_tx(tx, id, "record", record))
                .collect()
        })
    }

    /// Write a checkpoint covering every entry since the previous one now.
    /// Returns the latest checkpoint (unchanged when nothing is new), or
    /// `None` for an empty chain.
    pub fn checkpoint(&self) -> Result<Option<Checkpoint>, StorageError> {
        let config = self.chain_config()?;
        let mut guard = self.lock_conn();
        let tx = guard
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(conn_err("Failed to begin"))?;
        let out = checkpoint_in_tx(&tx, config.signing_key.as_ref())?;
        tx.commit().map_err(conn_err("Failed to commit"))?;
        Ok(out)
    }

    /// The newest entry, if any.
    pub fn chain_head(&self) -> Result<Option<ChainEntry>, StorageError> {
        let conn = self.lock_conn();
        conn.query_row(
            &format!(
                "SELECT {} FROM chain_entries ORDER BY seq DESC LIMIT 1",
                ENTRY_COLUMNS
            ),
            [],
            read_entry,
        )
        .optional()
        .map(|o| o.map(|(e, _)| e))
        .map_err(conn_err("Failed to read the chain head"))
    }

    /// Entries with `from <= seq <= to`, each with its canonical record text.
    pub fn chain_entries(
        &self,
        from: u64,
        to: u64,
    ) -> Result<Vec<(ChainEntry, String)>, StorageError> {
        let conn = self.lock_conn();
        let mut stmt = conn
            .prepare(&format!(
                "SELECT {} FROM chain_entries WHERE seq BETWEEN ? AND ? ORDER BY seq",
                ENTRY_COLUMNS
            ))
            .map_err(conn_err("Failed to read entries"))?;
        let rows = stmt
            .query_map(
                params![from as i64, to.min(i64::MAX as u64) as i64],
                read_entry,
            )
            .map_err(conn_err("Failed to read entries"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(conn_err("Failed to read entries"))
    }

    /// Every checkpoint, oldest first.
    pub fn checkpoints(&self) -> Result<Vec<Checkpoint>, StorageError> {
        let conn = self.lock_conn();
        let mut stmt = conn
            .prepare(&format!(
                "SELECT {} FROM chain_checkpoints ORDER BY checkpoint_no",
                CHECKPOINT_COLUMNS
            ))
            .map_err(conn_err("Failed to read checkpoints"))?;
        let rows = stmt
            .query_map([], read_checkpoint)
            .map_err(conn_err("Failed to read checkpoints"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(conn_err("Failed to read checkpoints"))
    }

    /// Merkle inclusion proof of entry `seq` against the checkpoint that
    /// covers it; `None` while no checkpoint covers it yet.
    pub fn inclusion_proof(&self, seq: u64) -> Result<Option<InclusionProof>, StorageError> {
        let checkpoint = self
            .checkpoints()?
            .into_iter()
            .find(|c| c.from_seq <= seq && seq <= c.seq);
        let Some(checkpoint) = checkpoint else {
            return Ok(None);
        };
        let hashes = {
            let conn = self.lock_conn();
            leaves_between(&conn, checkpoint.from_seq, checkpoint.seq)?
        };
        let leaves = to_leaves(&hashes)
            .ok_or_else(|| StorageError::SerializationError("malformed entry hash".into()))?;
        let index = (seq - checkpoint.from_seq) as usize;
        let path = merkle::inclusion_proof(&leaves, index)
            .ok_or_else(|| StorageError::NotFound(format!("entry {} is missing", seq)))?;
        Ok(Some(InclusionProof {
            seq,
            entry_hash: hashes[index].1.clone(),
            checkpoint_no: checkpoint.checkpoint_no,
            leaf_index: index as u64,
            tree_size: leaves.len() as u64,
            audit_path: path.iter().map(merkle::hash_to_hex).collect(),
            merkle_root: checkpoint.merkle_root,
        }))
    }

    /// Walk the whole chain and its checkpoints and report every problem.
    /// Works on any handle (chained or not), read-only.
    pub fn verify_chain(&self, options: &VerifyOptions) -> Result<VerifyReport, StorageError> {
        let mut report = VerifyReport::default();
        let entries = self.chain_entries(1, u64::MAX)?;
        let checkpoints = self.checkpoints()?;

        // Snapshot rows, to catch edits that bypass the chain table.
        let snapshots: BTreeMap<String, String> = {
            let conn = self.lock_conn();
            let mut stmt = conn
                .prepare("SELECT id, data_json FROM snapshots")
                .map_err(conn_err("Failed to read snapshots"))?;
            let rows = stmt
                .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
                .map_err(conn_err("Failed to read snapshots"))?;
            rows.collect::<Result<_, _>>()
                .map_err(conn_err("Failed to read snapshots"))?
        };

        let mut expected_seq = 1u64;
        let mut prev_hash = GENESIS_HASH.to_string();
        let mut by_seq: BTreeMap<u64, String> = BTreeMap::new();
        let mut chained_ids = std::collections::BTreeSet::new();
        for (entry, record_json) in &entries {
            report.entries_checked += 1;
            report.first_seq.get_or_insert(entry.seq);
            report.last_seq = Some(entry.seq);
            report.head_hash = Some(entry.entry_hash.clone());
            chained_ids.insert(entry.record_id.clone());
            by_seq.insert(entry.seq, entry.entry_hash.clone());
            if !report.hash_schemes.contains(&entry.hash_scheme) {
                report.hash_schemes.push(entry.hash_scheme.clone());
            }

            if entry.seq > expected_seq {
                report.missing_ranges.push((expected_seq, entry.seq - 1));
                report.problem(
                    "missing_entries",
                    Some(expected_seq),
                    format!(
                        "{} (deleted, or the log was cut)",
                        entries_span(expected_seq, entry.seq - 1, "is missing", "are missing")
                    ),
                );
            } else if entry.prev_hash != prev_hash {
                report.problem(
                    "broken_link",
                    Some(entry.seq),
                    format!(
                        "entry {} does not follow the entry before it (prev_hash {} expected {}): \
                         entries were reordered, inserted or replaced",
                        entry.seq,
                        short(&entry.prev_hash),
                        short(&prev_hash)
                    ),
                );
            }
            expected_seq = entry.seq + 1;
            prev_hash = entry.entry_hash.clone();

            if HashScheme::parse(&entry.hash_scheme) != Some(HashScheme::Jcs1) {
                report.problem(
                    "unknown_hash_scheme",
                    Some(entry.seq),
                    format!(
                        "entry {} names hash scheme {:?}",
                        entry.seq, entry.hash_scheme
                    ),
                );
                continue;
            }
            let record_ok = parse_canonical_text(record_json)
                .ok()
                .and_then(|v| canonical_json_v2(&v).ok())
                .is_some_and(|canonical| &canonical == record_json)
                && sha256_hex(record_json.as_bytes()) == entry.record_hash;
            let recomputed = compute_entry_hash(
                &entry.hash_scheme,
                entry.seq,
                &entry.record_id,
                &entry.kind,
                &entry.prev_hash,
                &entry.record_hash,
                &entry.recorded_at,
            )?;
            if !record_ok || recomputed != entry.entry_hash {
                report.problem(
                    "modified_entry",
                    Some(entry.seq),
                    format!(
                        "entry {} (record {}) was changed after it was written",
                        entry.seq, entry.record_id
                    ),
                );
            }
            if entry.kind == "decision" || entry.kind == "snapshot" {
                match snapshots.get(&entry.record_id) {
                    None => report.problem(
                        "snapshot_missing",
                        Some(entry.seq),
                        format!(
                            "snapshot {} (entry {}) was deleted",
                            entry.record_id, entry.seq
                        ),
                    ),
                    Some(data_json) => {
                        let same = parse_canonical_text(data_json)
                            .ok()
                            .and_then(|v| canonical_json_v2(&v).ok())
                            .is_some_and(|c| &c == record_json);
                        if !same {
                            report.problem(
                                "snapshot_modified",
                                Some(entry.seq),
                                format!(
                                    "snapshot {} (entry {}) differs from what was recorded",
                                    entry.record_id, entry.seq
                                ),
                            );
                        }
                    }
                }
            }
        }
        for id in snapshots.keys() {
            if !chained_ids.contains(id) {
                report.problem(
                    "unchained_record",
                    None,
                    format!(
                        "snapshot {} is not in the chain (written by a handle without the chain, or inserted directly)",
                        id
                    ),
                );
            }
        }

        // Checkpoints.
        let mut prev_cp_hash = GENESIS_HASH.to_string();
        let mut prev_cp_seq = 0u64;
        for (i, cp) in checkpoints.iter().enumerate() {
            report.checkpoints_checked += 1;
            report.last_checkpoint_seq = Some(cp.seq);
            if cp.checkpoint_no != i as u64 + 1 {
                report.problem(
                    "checkpoint_chain_broken",
                    None,
                    format!(
                        "checkpoint {} is out of sequence (expected {})",
                        cp.checkpoint_no,
                        i + 1
                    ),
                );
            }
            if cp.prev_checkpoint_hash != prev_cp_hash || cp.from_seq != prev_cp_seq + 1 {
                report.problem(
                    "checkpoint_chain_broken",
                    None,
                    format!(
                        "checkpoint {} does not follow the previous checkpoint (a checkpoint was removed or replaced)",
                        cp.checkpoint_no
                    ),
                );
            }
            prev_cp_hash = cp.checkpoint_hash.clone();
            prev_cp_seq = cp.seq;
            self.check_checkpoint(cp, &by_seq, options, &mut report, "checkpoint")?;
        }
        let head = report.last_seq.unwrap_or(0);
        if let Some(last_cp) = report.last_checkpoint_seq {
            if last_cp > head {
                report.problem(
                    "truncated",
                    Some(head + 1),
                    format!(
                        "the log ends at entry {} but a checkpoint covers up to entry {}: {}",
                        head,
                        last_cp,
                        entries_span(head + 1, last_cp, "was removed", "were removed")
                    ),
                );
            }
        }
        report.entries_after_last_checkpoint =
            head.saturating_sub(report.last_checkpoint_seq.unwrap_or(0));

        if let Some(anchor) = &options.against_checkpoint {
            report.anchored = true;
            if anchor.compute_hash()? != anchor.checkpoint_hash {
                report.problem(
                    "anchor_invalid",
                    None,
                    "the checkpoint given to verify against does not match its own hash".into(),
                );
            } else {
                if head < anchor.seq {
                    report.problem(
                        "truncated",
                        Some(head + 1),
                        format!(
                            "the log ends at entry {} but the anchored checkpoint {} covers up to entry {}: {}",
                            head,
                            anchor.checkpoint_no,
                            anchor.seq,
                            entries_span(head + 1, anchor.seq, "was removed", "were removed")
                        ),
                    );
                }
                if let Some(stored) = checkpoints
                    .iter()
                    .find(|c| c.checkpoint_no == anchor.checkpoint_no)
                {
                    if stored.checkpoint_hash != anchor.checkpoint_hash {
                        report.problem(
                            "checkpoint_replaced",
                            None,
                            format!(
                                "checkpoint {} in the database differs from the anchored copy",
                                anchor.checkpoint_no
                            ),
                        );
                    }
                }
                self.check_checkpoint(
                    anchor,
                    &by_seq,
                    options,
                    &mut report,
                    "anchored checkpoint",
                )?;
            }
        } else {
            report.notes.push(
                "Removal of the newest entries after the last checkpoint cannot be detected from the database alone; verify against a checkpoint kept outside it (against_checkpoint).".into(),
            );
        }
        if report.signed_checkpoints == 0 && report.checkpoints_checked > 0 {
            report.notes.push(
                "No checkpoint is signed: anyone able to write the database could have rebuilt it consistently.".into(),
            );
        }
        report.missing_ranges.sort();
        report.modified_seqs.sort();
        report.intact = report.problems.is_empty();
        Ok(report)
    }

    fn check_checkpoint(
        &self,
        cp: &Checkpoint,
        by_seq: &BTreeMap<u64, String>,
        options: &VerifyOptions,
        report: &mut VerifyReport,
        label: &str,
    ) -> Result<(), StorageError> {
        let no = cp.checkpoint_no;
        if cp.compute_hash()? != cp.checkpoint_hash {
            report.problem(
                "checkpoint_modified",
                None,
                format!("{} {} was changed after it was written", label, no),
            );
        }
        match (&cp.signature, &cp.public_key, &cp.key_id) {
            (Some(sig), Some(x), Some(key_id)) => {
                report.signed_checkpoints += 1;
                if !report.key_ids.contains(key_id) {
                    report.key_ids.push(key_id.clone());
                }
                if &signing::jwk_thumbprint(x) != key_id {
                    report.problem(
                        "checkpoint_key_mismatch",
                        None,
                        format!(
                            "{} {} names key {} but carries a different public key",
                            label,
                            no,
                            short(key_id)
                        ),
                    );
                } else if signing::verify_digest(&cp.checkpoint_hash, sig, x) {
                    report.signatures_verified += 1;
                } else {
                    report.problem(
                        "checkpoint_signature_invalid",
                        None,
                        format!("{} {} has a signature that does not verify", label, no),
                    );
                }
                if let Some(expected) = &options.expected_key_id {
                    if expected != key_id {
                        report.problem(
                            "unexpected_key",
                            None,
                            format!(
                                "{} {} is signed by key {}, not the expected key {}",
                                label,
                                no,
                                short(key_id),
                                short(expected)
                            ),
                        );
                    }
                }
            }
            (None, None, None) => {
                if options.expected_key_id.is_some() {
                    report.problem(
                        "unsigned_checkpoint",
                        None,
                        format!(
                            "{} {} is not signed but a signing key is required",
                            label, no
                        ),
                    );
                }
            }
            _ => report.problem(
                "checkpoint_signature_invalid",
                None,
                format!(
                    "{} {} has an incomplete signature (signature, key or key id missing)",
                    label, no
                ),
            ),
        }
        // Recompute the head and Merkle root from the entries as stored now.
        let mut leaves = Vec::new();
        let mut complete = true;
        for seq in cp.from_seq..=cp.seq {
            match by_seq.get(&seq).and_then(|h| merkle::hash_from_hex(h)) {
                Some(h) => leaves.push(h),
                None => {
                    complete = false;
                    break;
                }
            }
        }
        if !complete {
            // Missing entries are reported by the chain walk / truncation check.
            return Ok(());
        }
        if by_seq.get(&cp.seq) != Some(&cp.head_hash) {
            report.problem(
                "checkpoint_mismatch",
                Some(cp.seq),
                format!(
                    "{} {} recorded head {} at entry {}, the log now has a different entry there (rewritten after the checkpoint)",
                    label, no, short(&cp.head_hash), cp.seq
                ),
            );
        } else if merkle::hash_to_hex(&merkle::merkle_root(&leaves)) != cp.merkle_root {
            report.problem(
                "checkpoint_mismatch",
                Some(cp.from_seq),
                format!(
                    "{} {}: {} its Merkle root (rewritten after the checkpoint)",
                    label,
                    no,
                    entries_span(cp.from_seq, cp.seq, "no longer matches", "no longer match")
                ),
            );
        }
        Ok(())
    }
}

/// `entry 4 was removed` / `entries 3-5 were removed`.
fn entries_span(first: u64, last: u64, one: &str, many: &str) -> String {
    if first == last {
        format!("entry {} {}", first, one)
    } else {
        format!("entries {}-{} {}", first, last, many)
    }
}

fn short(hash: &str) -> &str {
    &hash[..hash.len().min(12)]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::{DecisionSnapshot, Output};

    fn decision(answer: &str) -> DecisionSnapshot {
        DecisionSnapshot::new("review_claim")
            .add_output(Output::new("verdict", json!(answer), "string").with_confidence(0.93))
            .with_execution_time(12.5)
    }

    fn key() -> SigningKey {
        SigningKey::from_seed(&[9u8; 32]).unwrap()
    }

    fn chained(path: &std::path::Path, every: u64) -> SqliteBackend {
        SqliteBackend::new(path)
            .unwrap()
            .with_chain(
                ChainConfig::new()
                    .with_checkpoint_every(every)
                    .with_signing_key(key()),
            )
            .unwrap()
    }

    fn raw(path: &std::path::Path) -> Connection {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            "DROP TRIGGER chain_entries_no_update; DROP TRIGGER chain_entries_no_delete; \
             DROP TRIGGER chain_checkpoints_no_update; DROP TRIGGER chain_checkpoints_no_delete;",
        )
        .unwrap();
        conn
    }

    fn kinds(report: &VerifyReport) -> Vec<&str> {
        report.problems.iter().map(|p| p.kind.as_str()).collect()
    }

    fn fill(backend: &SqliteBackend, n: usize) -> Vec<String> {
        (0..n)
            .map(|i| {
                backend
                    .save_decision_internal(&decision(&format!("a{}", i)))
                    .unwrap()
            })
            .collect()
    }

    #[test]
    fn an_untouched_chain_verifies_and_links_every_entry() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.db");
        let backend = chained(&path, 4);
        fill(&backend, 10);
        backend
            .append_record("rec-1", &json!({"x": 1.5, "y": "é"}))
            .unwrap();
        let report = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert!(report.intact, "{:?}", report.problems);
        assert_eq!(report.entries_checked, 11);
        assert_eq!(report.checkpoints_checked, 2);
        assert_eq!(report.signatures_verified, 2);
        assert_eq!(report.entries_after_last_checkpoint, 3);
        assert_eq!(report.key_ids, vec![key().key_id()]);
        let entries = backend.chain_entries(1, 100).unwrap();
        for pair in entries.windows(2) {
            assert_eq!(pair[1].0.prev_hash, pair[0].0.entry_hash);
        }
        assert_eq!(entries[0].0.prev_hash, GENESIS_HASH);
    }

    #[test]
    fn saves_of_existing_ids_and_deletes_are_refused() {
        let dir = tempfile::tempdir().unwrap();
        let backend = chained(&dir.path().join("e.db"), 0);
        let d = decision("yes");
        backend.save_decision_internal(&d).unwrap();
        assert!(matches!(
            backend.save_decision_internal(&d),
            Err(StorageError::AlreadyExists(_))
        ));
        assert!(matches!(
            backend.delete_internal(&d.metadata.snapshot_id.to_string()),
            Err(StorageError::PermissionDenied(_))
        ));
        backend.append_record("r", &json!({})).unwrap();
        assert!(matches!(
            backend.append_record("r", &json!({})),
            Err(StorageError::AlreadyExists(_))
        ));
        assert_eq!(backend.chain_head().unwrap().unwrap().seq, 2);
    }

    #[test]
    fn inexact_integers_are_refused_not_rounded() {
        let backend = SqliteBackend::in_memory()
            .unwrap()
            .with_chain(ChainConfig::new())
            .unwrap();
        let err = backend
            .append_record("big", &json!({"id": 9007199254740993u64}))
            .unwrap_err();
        assert!(err.to_string().contains("encode it as a string"), "{}", err);
        assert!(backend.chain_head().unwrap().is_none());
    }

    #[test]
    fn in_place_edit_of_a_record_is_reported_with_its_seq() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.db");
        let backend = chained(&path, 0);
        let ids = fill(&backend, 5);
        raw(&path)
            .execute(
                "UPDATE snapshots SET data_json = replace(data_json, '\"a2\"', '\"a9\"') WHERE id = ?",
                params![ids[2]],
            )
            .unwrap();
        let report = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert!(!report.intact);
        assert_eq!(kinds(&report), vec!["snapshot_modified"]);
        assert_eq!(report.modified_seqs, vec![3]);
        assert_eq!(report.first_broken_seq, Some(3));
    }

    #[test]
    fn edit_of_the_chain_row_itself_is_reported() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.db");
        let backend = chained(&path, 0);
        backend.append_record("a", &json!({"v": 1})).unwrap();
        backend.append_record("b", &json!({"v": 2})).unwrap();
        raw(&path)
            .execute(
                "UPDATE chain_entries SET record_json = '{\"v\":3}' WHERE seq = 2",
                [],
            )
            .unwrap();
        let report = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert_eq!(kinds(&report), vec!["modified_entry"]);
        assert_eq!(report.modified_seqs, vec![2]);
    }

    #[test]
    fn deletion_reports_the_missing_range_and_the_checkpoint_mismatch() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.db");
        let backend = chained(&path, 0);
        fill(&backend, 6);
        backend.checkpoint().unwrap();
        raw(&path)
            .execute("DELETE FROM chain_entries WHERE seq IN (3, 4)", [])
            .unwrap();
        let report = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert!(!report.intact);
        assert_eq!(report.missing_ranges, vec![(3, 4)]);
        assert_eq!(report.first_broken_seq, Some(3));
        assert!(kinds(&report).contains(&"missing_entries"));
        // The snapshots for 3 and 4 are now outside the chain.
        assert_eq!(
            kinds(&report)
                .iter()
                .filter(|k| **k == "unchained_record")
                .count(),
            2
        );
    }

    #[test]
    fn reordering_breaks_the_links() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.db");
        let backend = chained(&path, 0);
        for i in 0..4 {
            backend
                .append_record(&format!("r{}", i), &json!({"i": i}))
                .unwrap();
        }
        let conn = raw(&path);
        // Swap entries 2 and 3 (renumbering them), as a reordering attack would.
        conn.execute_batch(
            "UPDATE chain_entries SET seq = -2 WHERE seq = 2; \
             UPDATE chain_entries SET seq = 2 WHERE seq = 3; \
             UPDATE chain_entries SET seq = 3 WHERE seq = -2;",
        )
        .unwrap();
        let report = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert!(kinds(&report).contains(&"broken_link"));
        assert_eq!(report.first_broken_seq, Some(2));
    }

    #[test]
    fn a_consistently_rewritten_tail_is_caught_by_the_signed_checkpoint() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.db");
        let backend = chained(&path, 0);
        for i in 0..4 {
            backend
                .append_record(&format!("r{}", i), &json!({"i": i}))
                .unwrap();
        }
        backend.checkpoint().unwrap();
        // Attacker without the key deletes entry 4 and appends a forged one
        // with correct hashes.
        raw(&path)
            .execute("DELETE FROM chain_entries WHERE seq = 4", [])
            .unwrap();
        let forger = SqliteBackend::new(&path)
            .unwrap()
            .with_chain(ChainConfig::new().with_checkpoint_every(0))
            .unwrap();
        forger.append_record("forged", &json!({"i": 99})).unwrap();
        let report = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert!(
            kinds(&report).contains(&"checkpoint_mismatch"),
            "{:?}",
            report.problems
        );
    }

    #[test]
    fn truncation_is_only_visible_against_an_anchored_checkpoint() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.db");
        let backend = chained(&path, 0);
        for i in 0..5 {
            backend
                .append_record(&format!("r{}", i), &json!({"i": i}))
                .unwrap();
        }
        let anchor = backend.checkpoint().unwrap().unwrap();
        // The attacker drops the newest two entries AND the checkpoint row.
        let conn = raw(&path);
        conn.execute("DELETE FROM chain_entries WHERE seq > 3", [])
            .unwrap();
        conn.execute("DELETE FROM chain_checkpoints", []).unwrap();
        let alone = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert!(
            alone.intact,
            "undetectable without an anchor: {:?}",
            alone.problems
        );
        assert!(alone.notes.iter().any(|n| n.contains("cannot be detected")));
        let anchored = backend
            .verify_chain(&VerifyOptions {
                against_checkpoint: Some(anchor),
                expected_key_id: Some(key().key_id()),
            })
            .unwrap();
        assert!(!anchored.intact);
        assert!(kinds(&anchored).contains(&"truncated"));
        assert!(anchored.anchored);
    }

    #[test]
    fn a_tampered_or_resigned_checkpoint_is_reported() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.db");
        let backend = chained(&path, 0);
        backend.append_record("a", &json!({})).unwrap();
        backend.checkpoint().unwrap();
        raw(&path)
            .execute(
                "UPDATE chain_checkpoints SET created_at = '2000-01-01T00:00:00Z'",
                [],
            )
            .unwrap();
        let report = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert!(kinds(&report).contains(&"checkpoint_modified"));

        // Key substitution: re-sign everything with the attacker's key. It
        // verifies internally; only a pinned key id exposes it.
        let dir2 = tempfile::tempdir().unwrap();
        let path2 = dir2.path().join("e.db");
        let attacker = SigningKey::from_seed(&[66u8; 32]).unwrap();
        let forged = SqliteBackend::new(&path2)
            .unwrap()
            .with_chain(
                ChainConfig::new()
                    .with_checkpoint_every(0)
                    .with_signing_key(attacker),
            )
            .unwrap();
        forged.append_record("a", &json!({})).unwrap();
        forged.checkpoint().unwrap();
        assert!(
            forged
                .verify_chain(&VerifyOptions::default())
                .unwrap()
                .intact
        );
        let pinned = forged
            .verify_chain(&VerifyOptions {
                against_checkpoint: None,
                expected_key_id: Some(key().key_id()),
            })
            .unwrap();
        assert_eq!(kinds(&pinned), vec!["unexpected_key"]);
    }

    #[test]
    fn inclusion_proofs_verify_against_the_checkpoint_root() {
        let dir = tempfile::tempdir().unwrap();
        let backend = chained(&dir.path().join("e.db"), 7);
        for i in 0..20 {
            backend
                .append_record(&format!("r{}", i), &json!({"i": i}))
                .unwrap();
        }
        assert!(
            backend.inclusion_proof(20).unwrap().is_none(),
            "not checkpointed yet"
        );
        for seq in 1..=14 {
            let proof = backend.inclusion_proof(seq).unwrap().unwrap();
            let leaf = merkle::hash_from_hex(&proof.entry_hash).unwrap();
            let path: Vec<Hash> = proof
                .audit_path
                .iter()
                .map(|h| merkle::hash_from_hex(h).unwrap())
                .collect();
            let root = merkle::hash_from_hex(&proof.merkle_root).unwrap();
            assert!(merkle::verify_inclusion(
                &leaf,
                proof.leaf_index as usize,
                proof.tree_size as usize,
                &path,
                &root
            ));
        }
    }

    #[test]
    fn opting_in_adopts_existing_rows_and_flags_later_unchained_writes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.db");
        let plain = SqliteBackend::new(&path).unwrap();
        plain.save_decision_internal(&decision("before")).unwrap();
        let backend = chained(&path, 0);
        let report = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert!(report.intact);
        assert_eq!(report.entries_checked, 1);
        // A handle without the chain writes around it.
        plain.save_decision_internal(&decision("sneaky")).unwrap();
        let report = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert_eq!(kinds(&report), vec!["unchained_record"]);
    }

    #[test]
    fn a_plain_database_is_unchanged_by_the_migration() {
        let backend = SqliteBackend::in_memory().unwrap();
        let d = decision("x");
        let id = backend.save_decision_internal(&d).unwrap();
        // Default handles still replace by id and delete.
        backend.save_decision_internal(&d).unwrap();
        assert!(backend.delete_internal(&id).unwrap());
        assert!(!backend.is_chained());
        assert!(backend.chain_head().unwrap().is_none());
    }

    /// Two real processes append to one database at the same time; the chain
    /// must come out linear with no gaps.
    #[test]
    fn two_processes_appending_do_not_fork_the_chain() {
        const CHILD: &str = "BRIEFCASE_CHAIN_CHILD_DB";
        if let Ok(path) = std::env::var(CHILD) {
            let tag = std::env::var("BRIEFCASE_CHAIN_CHILD_TAG").unwrap();
            let backend = SqliteBackend::new(&path)
                .unwrap()
                .with_chain(
                    ChainConfig::new()
                        .with_checkpoint_every(25)
                        .with_signing_key(key()),
                )
                .unwrap();
            for i in 0..100 {
                backend
                    .append_record(
                        &format!("{}-{}", tag, i),
                        &json!({"tag": tag, "i": i, "f": 0.1 * i as f64}),
                    )
                    .unwrap();
            }
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("shared.db");
        // Create the schema first so both children open an existing file.
        drop(chained(&path, 25));
        let exe = std::env::current_exe().unwrap();
        let spawn = |tag: &str| {
            std::process::Command::new(&exe)
                .args([
                    "--exact",
                    "storage::chain::tests::two_processes_appending_do_not_fork_the_chain",
                    "--nocapture",
                ])
                .env(CHILD, &path)
                .env("BRIEFCASE_CHAIN_CHILD_TAG", tag)
                .spawn()
                .unwrap()
        };
        let (mut a, mut b) = (spawn("a"), spawn("b"));
        assert!(a.wait().unwrap().success());
        assert!(b.wait().unwrap().success());
        let backend = chained(&path, 25);
        let report = backend.verify_chain(&VerifyOptions::default()).unwrap();
        assert!(report.intact, "{:?}", report.problems);
        assert_eq!(report.entries_checked, 200);
        assert_eq!(report.checkpoints_checked, 8);
        let entries = backend.chain_entries(1, 200).unwrap();
        let from_a = entries
            .iter()
            .filter(|(e, _)| e.record_id.starts_with("a-"))
            .count();
        assert_eq!(from_a, 100);
    }
}
