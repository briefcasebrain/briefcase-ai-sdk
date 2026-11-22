# Tamper-evident decision store

Record decisions into a SQLite file where every record is hash-chained to the one before it,
so nothing can be edited, reordered, inserted or deleted afterwards without `briefcase verify`
saying so. Signed checkpoints let you prove one record was in the log without handing over the
whole log.

Flow: **Verify**. Feature id: `chained-store`. Status: experimental.

## The one command

```python
import briefcase
briefcase.observe("chain:evidence.db")       # every @capture record goes into the chain
```

```bash
briefcase verify evidence.db
```

Sign checkpoints (recommended): create a key once and point `BRIEFCASE_SIGNING_KEY` at it.

```bash
briefcase bundle keygen ~/.briefcase/signing-key.jwk
export BRIEFCASE_SIGNING_KEY=~/.briefcase/signing-key.jwk
```

From Rust: `SqliteBackend::new(path)?.with_chain(ChainConfig::new().with_signing_key(key))?`
(feature `chain`). From Python directly:
`briefcase._native.SqliteBackend(path, chained=True, signing_key="key.jwk")`.

### Real output

Captured on 2026-11-22 in a temporary `HOME` (its path is shown as `$HOME`). `capture.py`
calls `briefcase.observe("chain:evidence.db")`, captures four synthetic `score_claim` decisions
in two `briefcase.correlation` workflows, and calls `briefcase.shutdown()`. `tamper.py` plays
the attacker: it opens the file with Python's `sqlite3`, drops the guard triggers and runs the
SQL shown. The `briefcase ...` commands were run through the command modules' `register()`
(what the CLI's command discovery does; discovery itself belongs to another work package).

```text
$ briefcase bundle keygen $HOME/.briefcase/signing-key.jwk
Wrote private key $HOME/.briefcase/signing-key.jwk (mode 0600). Keep it private; back it up.
  Key id (publish this so verifiers can pin it): 88ed9d51e8791dd0d572d469320ead053a94b03ef3cbfed86b21b86cc2f773a5
  Public key: {"kty": "OKP", "crv": "Ed25519", "x": "ImilAvK2BUicqFU_kxSj9N7MNVusxYtUE8QO75KRjQk"}
Next: export BRIEFCASE_SIGNING_KEY=$HOME/.briefcase/signing-key.jwk   (then briefcase.observe('chain:evidence.db'))
[exit 0]

$ BRIEFCASE_SIGNING_KEY=~/.briefcase/signing-key.jwk python capture.py
[exit 0]

$ briefcase verify evidence.db
INTACT: evidence.db (chained store)
  Checked 4 entries (seq 1-4) and 1 checkpoint.
  Signatures: 1 verified (key 88ed9d51e879).
  Every entry links to the one before it and matches its record.
  Note: Removal of the newest entries after the last checkpoint cannot be detected from the database alone; verify against a checkpoint kept outside it (--against FILE).
Next: briefcase verify evidence.db --save-checkpoint anchor.json   (keep it off this machine)
[exit 0]

$ briefcase verify evidence.db --save-checkpoint anchor.json
INTACT: evidence.db (chained store)
  Checked 4 entries (seq 1-4) and 1 checkpoint.
  Signatures: 1 verified (key 88ed9d51e879).
  Every entry links to the one before it and matches its record.
  Note: Removal of the newest entries after the last checkpoint cannot be detected from the database alone; verify against a checkpoint kept outside it (--against FILE).
  Saved the latest checkpoint to anchor.json. Keep a copy where the writer of evidence.db cannot change it.
Next: copy anchor.json off this machine; later run: briefcase verify evidence.db --against anchor.json
[exit 0]

$ python tamper.py evidence.db "UPDATE chain_entries SET record_json = replace(record_json, 'False', 'True') WHERE seq = 4"

$ briefcase verify evidence.db
TAMPERED: evidence.db (chained store)
  Checked 4 entries (seq 1-4) and 1 checkpoint.
  Signatures: 1 verified (key 88ed9d51e879).
  1 problem:
    - entry 4 (record 9a2fb3f7-d856-47d1-8c8b-789b94ffe669) was changed after it was written
  First broken entry: 4; changed: 4.
  Note: Removal of the newest entries after the last checkpoint cannot be detected from the database alone; verify against a checkpoint kept outside it (--against FILE).
Next: briefcase verify evidence.db --json > verify-report.json   (keep the full report)
[exit 1]

$ python tamper.py cut.db "DELETE FROM chain_entries WHERE seq = 4" "DELETE FROM chain_checkpoints"

$ briefcase verify cut.db
INTACT: cut.db (chained store)
  Checked 3 entries (seq 1-3) and 0 checkpoints.
  Every entry links to the one before it and matches its record.
  3 entries are chained but not yet covered by a checkpoint.
  Note: Removal of the newest entries after the last checkpoint cannot be detected from the database alone; verify against a checkpoint kept outside it (--against FILE).
Next: briefcase verify cut.db --save-checkpoint anchor.json   (keep it off this machine)
[exit 0]

$ briefcase verify cut.db --against anchor.json
INCOMPLETE: cut.db (chained store)
  Checked 3 entries (seq 1-3) and 0 checkpoints.
  Signatures: 1 verified (key 88ed9d51e879).
  1 problem:
    - the log ends at entry 3 but the anchored checkpoint 1 covers up to entry 4: entry 4 was removed
Next: briefcase verify cut.db --json > verify-report.json   (keep the full report)
[exit 1]
```

`cut.db` is a copy of the untouched file with its newest entry and its checkpoint deleted. On
its own it verifies as intact: that is the truncation limit described below, shown for real.

## What it writes, and where

Only the SQLite file you name (created owner-only, 0600). Tables, added by schema migration 2
(`PRAGMA user_version`) to every database the SDK opens, empty and unused unless a handle opts
in:

- `chain_entries`: `seq`, `record_id`, `kind`, `hash_scheme` (`jcs-1`), `record_json` (the
  record's RFC 8785 text), `record_hash`, `prev_hash`, `entry_hash`, `recorded_at`.
  `entry_hash = sha256(jcs({v:1, hash_scheme, seq, record_id, kind, prev_hash, record_hash,
  recorded_at}))`; seq 1 links to 64 zeros.
- `chain_checkpoints`: `checkpoint_no`, `from_seq`..`seq`, `head_hash`, `merkle_root` (RFC 6962
  layout over the entry hashes since the previous checkpoint), `prev_checkpoint_hash`,
  `created_at`, `key_id`, `public_key`, `signature` (Ed25519 over the 32 bytes of
  `checkpoint_hash`).
- `chain_meta`: when the chain started and how many pre-existing rows it adopted.
- Triggers that refuse plain `UPDATE`/`DELETE` on the two chain tables (an accident guard; an
  attacker can drop them, which is what verification is for).

Decisions saved through `save_decision` also land in the usual `snapshots` table, so `load`
and `query` work as before; verification compares each snapshot row with its chain entry.
Records are stored exactly as captured: redaction or hashing applied by `@capture` is what the
chain holds. Nothing is sent anywhere.

Keys use the format of `briefcase.integrity.signing`: a 32-byte Ed25519 seed, stored by
`briefcase bundle keygen` as a private RFC 8037 JWK; public keys are OKP JWKs named by their
RFC 7638 thumbprint (the "key id"). A key made in Python signs in Rust and the other way round
(tested both ways; Ed25519 is deterministic, so both produce the same signature bytes).

## Defaults and how to change them

| Setting | Default | Change with |
|---|---|---|
| Checkpoint cadence | every 100 entries, and when the exporter closes (`briefcase.shutdown()` / exit) | `checkpoint_every=N` (0 = only on demand), `BRIEFCASE_CHECKPOINT_EVERY`, `backend.checkpoint()` |
| Signing key | none (checkpoints carry hashes only) | `BRIEFCASE_SIGNING_KEY=path`, `signing_key=` |
| Hash scheme | `jcs-1` | not configurable |
| Overwrites and deletes | refused (`FileExistsError` / `PermissionError`) | not configurable in chained mode |

Why 100: a checkpoint costs one Merkle root over at most 100 hashes and one signature, so the
overhead per record is tiny, and at most 100 records are ever uncovered by a signed checkpoint
in a long-running process; short scripts get a checkpoint at shutdown.

Concurrency: each append runs in one `BEGIN IMMEDIATE` transaction, which takes SQLite's write
lock before the chain tail is read, so writers in different processes queue instead of forking
the chain (a UNIQUE index on `prev_hash` backs this up). Tested with two OS processes appending
to one file, from Rust and from Python.

Opting in on an existing database adopts its existing rows into the chain first (oldest
first); they are provably unaltered only from that moment on.

## How to turn it off or undo

Stop using `chain:` (or `chained=True`). The file stays a normal SQLite database that plain
handles can read; they can also write around the chain, and `briefcase verify` reports those
rows as `unchained_record`. There is no way to "unchain" entries: that would defeat the point.
Delete the file to discard it.

## Threat model

Detected by `briefcase verify` (or `SqliteBackend.verify()`, which runs the same checks in Rust):

| Attack | How it shows |
|---|---|
| In-place edit of a record or its decision row | `modified_entry` / `snapshot_modified`, with the seq |
| Deletion from the middle | `missing_entries` with the missing range |
| Reordering, or inserting a row between two others | `broken_link` at the first affected seq |
| Writing records around the chain (plain handle, raw SQL) | `unchained_record` |
| Rewriting the newest entries consistently, after a checkpoint | `checkpoint_mismatch` (head or Merkle root differs) |
| Editing, removing or re-ordering checkpoints | `checkpoint_modified`, `checkpoint_chain_broken`, `checkpoint_signature_invalid` |
| Truncation (newest entries and their checkpoints removed) | **only** with `--against` a checkpoint kept elsewhere: `truncated` |
| Key substitution (re-signing everything with another key) | **only** with a pinned key, `--key KEYID`: `unexpected_key` |

Not detected:

- A writer who holds the signing key and rewrites the whole log (and re-signs every
  checkpoint) before any checkpoint was anchored outside their reach. Checkpoints prove
  consistency and who signed; only an external anchor (`--save-checkpoint` copied to a WORM
  bucket, a ticket, an email to the auditor) fixes the history in time.
- Truncation after the last checkpoint, when you have no anchored checkpoint newer than the cut.
- Records that were false when written. The chain proves "unchanged since recorded", not
  "true".

## Limits and failure modes

- Values RFC 8785 cannot represent exactly (NaN, Infinity, integers beyond 2^53 a double cannot
  hold) are refused: the exporter logs a warning naming the record and counts it as failed in
  `briefcase.export_stats()`. Encode such values as strings.
- Needs the compiled core to write (`briefcase._native`, part of the wheel). `briefcase verify`
  reads the file with Python's `sqlite3` and works without it.
- One writer lock per database: very high write rates from many processes serialize.
- Not tested on network filesystems (SQLite locking is unreliable there) or Windows.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `briefcase verify` says "SQLite database without a hash chain" | The file was written without the chain. Use `briefcase.observe("chain:evidence.db")` or `SqliteBackend(path, chained=True)`. |
| `Signatures: none (checkpoints are not signed)` | `briefcase bundle keygen ~/.briefcase/signing-key.jwk` and `export BRIEFCASE_SIGNING_KEY=...` before capturing. |
| Records never appear in the chain | Run `briefcase status`; check `briefcase.export_stats()` for failures and the log for "not added to the evidence chain". |
| `ValueError: ... encode it as a string` | A record holds NaN or a huge integer; convert it to a string before capture. |
| `FileExistsError` on save | Chained stores never overwrite: save under a new id. |
| `unchained_record` after mixing old code | Some process writes without `chained=True`; switch it to the chained store. |
| "N entries ... not yet covered by a checkpoint" | Normal between checkpoints; call `briefcase.shutdown()` or `backend.checkpoint()` to cover them. |
| `database is locked` | Another process held the write lock for more than 5 s; retry, or reduce concurrent writers. |
