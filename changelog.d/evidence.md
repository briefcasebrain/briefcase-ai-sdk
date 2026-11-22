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
- `briefcase verify PATH` for a chained store, chain JSONL or evidence bundle of any version:
  plain-language verdict, `--json`, `--against CHECKPOINT`, `--key`, `--kms`,
  `--save-checkpoint`; exit 0 intact, 1 tampered or incomplete, 2 usage. Pure Python, read-only.
- One evidence bundle format (`briefcase.compliance.bundle`, `briefcase-bundle` format 2): a ZIP
  with manifest (file hashes, RFC 8785 `content_hash`, sealed signer list, signatures), records
  (one or many; a sealed routing decision is a bundle whose records are the decision, policy and
  evidence), chain entries with Merkle proofs, checkpoints, public keys and a `README.txt` on what
  it does and does not prove. One verifier, `verify_bundle`, dispatching on format version, hash
  scheme and signer type, also accepts the released format-1 `ExaminerBundle` JSON.
- Pluggable signers (`briefcase.compliance.signers`): `Ed25519Signer` (key files / JWK),
  `KmsSigner` (AWS KMS, same key-id and signing-algorithm metadata as `SignedExaminerBundle`),
  a `Signer` protocol and `register_verifier` for others. Verifiers pin keys; KMS is only called
  when the caller passes a client (`--kms`).
- `briefcase bundle create|verify|inspect|keygen`: create from a chained store (select by
  session, workflow, trace or time) or re-package a format-1 examiner JSON (its KMS signature is
  kept and still checked); `--sign-key` accepts key files and `kms:KEY_ID`.
- `ExaminerBundle.to_bundle(path, signers=...)` / `from_bundle(path)`;
  `SignedExaminerBundle.build_and_sign` / `verify_signature` accept `kms_client=`;
  `briefcase.compliance.migrate_legacy`, `inspect_bundle`, `write_bundle`.
- TypeScript: `verifyBundle`, `readZip` in `@briefcase-ai/runtime/integrity`: format-2 verifier
  (files, seal, Ed25519 via `node:crypto`, checkpoints, Merkle proofs); KMS signatures are
  reported unchecked and format 1 is not read.
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
- `SignedExaminerBundle` signs and verifies through `KmsSigner` / `kms_verify_raw`; behaviour and
  the format-1 JSON are unchanged (no deprecation).
- Not breaking: existing hashes, chains, bundles and defaults are unchanged. Data written with `jcs-1`
  (including any chained store) cannot be verified by releases older than this one.
