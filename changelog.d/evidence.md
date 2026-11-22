### Added

- Hash scheme `jcs-1`: RFC 8785 canonical JSON in Python (`briefcase.integrity.canonical_json_v2`,
  `canonical_bytes`, `parse_canonical_text`), TypeScript (`canonicalJsonV2`, `canonicalBytes` in
  `@briefcase-ai/runtime/integrity`) and Rust (`briefcase_core::integrity::canonical_json_v2`),
  byte-identical across the three, floats included. Integers a double cannot hold exactly are
  rejected, never rounded. Golden vectors `tests/vectors/canonical_json_v2.json` (RFC 8785
  examples, published JCS test data, floats, large integers, astral keys, escapes) are asserted
  by all three languages and in the `canonical-vectors` CI job.
- `hash_scheme` on every hash-carrying structure: `HashChainEntry` (Python and TypeScript; absent
  means `json-sorted-1`), chained-store entries and checkpoints, bundle manifests. Verifiers
  dispatch on it. `HashChainAppender(..., hash_scheme="jcs-1")` / `{ hashScheme: "jcs-1" }` opts
  a chain in; the default stays `json-sorted-1`.
- Tamper-evident store (Rust feature `chain`): `SqliteBackend::with_chain(ChainConfig)` /
  `SqliteBackend(path, chained=True, signing_key=..., checkpoint_every=100)` in Python. Each
  saved decision or `append_record` gets `seq`, `prev_hash` and `entry_hash` in one immediate
  transaction (no forks across processes); overwrites and deletes are refused. Merkle
  checkpoints every N entries or on demand, Ed25519-signed with keys interchangeable with
  `briefcase.integrity.signing`. `verify` / `verify_chain` report broken links, missing ranges,
  modified entries, checkpoint and signature failures, unexpected keys, and truncation against an
  externally kept checkpoint (`against_checkpoint`). `inclusion_proof(seq)`.
- `briefcase.observe("chain:evidence.db")` and `briefcase.exporters.ChainedSqliteExporter`
  (`BRIEFCASE_SIGNING_KEY`, `BRIEFCASE_CHECKPOINT_EVERY`); a checkpoint is written on shutdown.
- `briefcase verify PATH` for a chained store, chain JSONL or bundle: plain-language verdict,
  `--json`, `--against CHECKPOINT`, `--key`, `--save-checkpoint`; exit 0 intact, 1 tampered or
  incomplete, 2 usage. Pure Python, read-only; works without the compiled core.
- `briefcase bundle create|verify|keygen` and `briefcase.compliance.audit_bundle`: a
  self-verifying ZIP (records, chain entries, Merkle proofs, signed checkpoints, public keys,
  manifest, `README.txt` on what it does and does not prove), selectable by session, workflow,
  trace or time. Verification needs only the SDK (PyNaCl, the core, or a pure-Python RFC 8032
  fallback).
- `briefcase.integrity.merkle`, `briefcase.integrity.keys`, `briefcase.integrity.evidence`.
- `status()` hook for `briefcase status` (`chained-store`).

### Changed

- SQLite schema migration 2 adds the empty `chain_entries`, `chain_checkpoints` and `chain_meta`
  tables (with UPDATE/DELETE guard triggers) to every database the core opens. Plain databases
  behave exactly as before; releases before this one ignore the tables.
- `briefcase-core` enables `serde_json`'s `float_roundtrip` feature so stored floats parse back
  to the exact double (needed to re-verify hashes). Float parsing is slightly slower.
- `JsonlHashChainStore` writes a `hash_scheme` field only for non-default schemes, so
  `json-sorted-1` lines are byte-identical to earlier releases.
- Not breaking: existing hashes, chains and defaults are unchanged. Data written with `jcs-1`
  (including any chained store) cannot be verified by releases older than this one.
