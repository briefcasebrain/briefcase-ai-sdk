# Audit bundles and `briefcase verify`

Give an auditor one ZIP file that proves which decisions were recorded and that they have not
changed since, checkable offline with only the SDK installed. `briefcase verify` answers "is
this evidence intact?" for a chained store, a chain JSONL file or a bundle, in plain language
and with exit codes CI can use.

Flow: **Verify**. Feature id: `audit-bundle`. Status: experimental.

## The one command

```bash
briefcase bundle create --from evidence.db --out evidence-bundle.zip --sign-key ~/.briefcase/signing-key.jwk
```

The auditor runs:

```bash
briefcase bundle verify evidence-bundle.zip --key KEYID
```

Select records with `--session ID`, `--workflow ID`, `--trace ID` (matched on the record's
`session_id` / `workflow_id` / `trace_id`, its `tags` or `attributes`, or a decision's tags) and
`--since` / `--until` (ISO 8601, compared with the time the record was chained).

`briefcase verify PATH` accepts a chained store (`.db`), a JSONL export of one
(`briefcase.integrity.evidence.export_jsonl`), a `JsonlHashChainStore` file, or a bundle, and
dispatches on what the file is. Exit codes: `0` intact, `1` tampered or incomplete, `2` usage
(missing file, unknown format). `--json` prints the full report.

### Real output

Captured on 2026-11-22 in a temporary `HOME` (its path shown as `$HOME`), continuing the
session in [chained-store.md](chained-store.md): four synthetic `score_claim` decisions, three
in workflow `wf-claims-42`, captured with `briefcase.observe("chain:evidence.db")` and a
signing key. `edited.zip` is a copy of the bundle with one number in `records.jsonl` changed
by a short Python script. Commands were run through the command modules' `register()` (what
the CLI's command discovery does).

```text
$ briefcase bundle create --from evidence.db --workflow wf-claims-42 --out wf-claims-42.zip --sign-key $HOME/.briefcase/signing-key.jwk
Wrote wf-claims-42.zip
  3 record(s) of 4 in the source chain, 1 checkpoint(s).
  Checkpoints signed by key 88ed9d51e8791dd0d572d469320ead053a94b03ef3cbfed86b21b86cc2f773a5.
  Bundle signed; content hash sha256:e8a0154e3d23edbfabb1a0f0327547a66ea95d5cf9f3202ee7d231932ce84c9a.
Next: briefcase bundle verify wf-claims-42.zip --key 88ed9d51e8791dd0d572d469320ead053a94b03ef3cbfed86b21b86cc2f773a5
[exit 0]

$ briefcase bundle verify wf-claims-42.zip
INTACT: wf-claims-42.zip (audit bundle)
  Checked 3 records (seq 1-3) against 1 checkpoint.
  Signatures: 2 verified (key 88ed9d51e879).
  Every record matches its chain entry and its Merkle proof leads to a checkpoint.
  Note: The bundle carries its own public key(s). Confirm that key id 88ed9d51e8791dd0d572d469320ead053a94b03ef3cbfed86b21b86cc2f773a5 is the one the record keeper published, or re-run with --key to pin it.
  Note: A bundle proves the included records were in the log when its checkpoint was made and are unaltered since. It does not prove the log is complete unless that checkpoint was anchored outside the record keeper's control.
Next: briefcase bundle verify wf-claims-42.zip --key 88ed9d51e8791dd0d572d469320ead053a94b03ef3cbfed86b21b86cc2f773a5   (pin the keys the record keeper published)
[exit 0]

$ briefcase bundle verify wf-claims-42.zip --key 88ed9d51e8791dd0d572d469320ead053a94b03ef3cbfed86b21b86cc2f773a5
INTACT: wf-claims-42.zip (audit bundle)
  Checked 3 records (seq 1-3) against 1 checkpoint.
  Signatures: 2 verified (key 88ed9d51e879).
  Every record matches its chain entry and its Merkle proof leads to a checkpoint.
  Note: A bundle proves the included records were in the log when its checkpoint was made and are unaltered since. It does not prove the log is complete unless that checkpoint was anchored outside the record keeper's control.
Next: keep this bundle with the audit file; anyone can re-run this check offline.
[exit 0]

$ briefcase bundle verify edited.zip
TAMPERED: edited.zip (audit bundle)
  Checked 3 records (seq 1-3) against 1 checkpoint.
  Signatures: 2 verified (key 88ed9d51e879).
  2 problems:
    - records.jsonl was changed after the bundle was made
    - record 152c7fd5-fac6-45f1-aa34-e45a6637d5e7 (entry 2) does not match its chain entry: it was changed
  First broken entry: 2; changed: 2.
  Note: The bundle carries its own public key(s). Confirm that key id 88ed9d51e8791dd0d572d469320ead053a94b03ef3cbfed86b21b86cc2f773a5 is the one the record keeper published, or re-run with --key to pin it.
  Note: A bundle proves the included records were in the log when its checkpoint was made and are unaltered since. It does not prove the log is complete unless that checkpoint was anchored outside the record keeper's control.
Next: ask the record keeper for a fresh bundle; do not rely on this one.
[exit 1]

$ briefcase verify evidence.db --json | head -20
{
  "intact": true,
  "entries_checked": 4,
  "first_seq": 1,
  "last_seq": 4,
  "head_hash": "ba53f24abc9d904f3aa5404e0b2551ba5d95cd7e5da714ba9e54604e66fac9e2",
  "checkpoints_checked": 1,
  "signed_checkpoints": 1,
  "signatures_verified": 1,
  "last_checkpoint_seq": 4,
  "entries_after_last_checkpoint": 0,
  "key_ids": [
    "88ed9d51e8791dd0d572d469320ead053a94b03ef3cbfed86b21b86cc2f773a5"
  ],
  "hash_schemes": [
    "jcs-1"
  ],
  "first_broken_seq": null,
  "missing_ranges": [],
  "modified_seqs": [],

$ briefcase verify missing.db
error: missing.db does not exist
fix: check the path, e.g. briefcase verify ./evidence.db
[exit 2]

$ briefcase verify runs.jsonl
error: runs.jsonl has no chain fields (seq/prev_hash/entry_hash, or prior_hash/payload_hash). Plain record files (briefcase.observe('runs.jsonl')) carry no hashes; capture into a chained store with briefcase.observe('chain:evidence.db') instead.
[exit 2]
```

"Signatures: 2 verified" counts the checkpoint signature and the bundle's own signature.

## What it writes, and where

`bundle create` writes one new ZIP at `--out` (never overwrites) and reads the source
read-only. `bundle keygen` writes one new private key file, mode 0600. `verify
--save-checkpoint FILE` writes the store's latest checkpoint to a new file. Nothing else is
written and nothing is sent anywhere.

The bundle holds:

| File | Content |
|---|---|
| `manifest.json` | format `briefcase-audit-bundle`, `schema_version` 1, selection, counts, SHA-256 of every other file, `content_hash` (`sha256:` over the RFC 8785 form of the manifest without `content_hash`/`signature`), optional Ed25519 `signature` |
| `records.jsonl` | per record: the record exactly as stored, its chain entry (`seq`, `prev_hash`, `record_hash`, `entry_hash`, `recorded_at`, ...), and a Merkle inclusion proof (`checkpoint_no`, `leaf_index`, `tree_size`, `audit_path`) |
| `checkpoints.json` | the checkpoints those proofs point at, with signatures |
| `keys.json` | public keys (RFC 8037 JWK) by key id (RFC 7638 thumbprint) |
| `README.txt` | what the bundle proves and does not prove, how to check it, and the manual recipe |

The README's "proves / does not prove" section, from the bundle above:

```text
WHAT A PASSING CHECK PROVES

- Each record in records.jsonl is byte-for-byte the record that was written to the
  hash chain (its hash, and the chain entry's hash over it, recompute exactly).
- Each record was in the log when the checkpoint named in its proof was made: the
  Merkle proof links its chain entry to that checkpoint's root.
- Those checkpoints were signed by the Ed25519 key(s) 88ed9d51e8791dd0, so they were not
  altered after signing, and only a holder of that key could have made them.

WHAT IT DOES NOT PROVE

- That the log is complete. Records outside the selection are not included, and the
  log could have been cut or rebuilt before the checkpoint was made unless that
  checkpoint was also stored somewhere the record keeper cannot change (an external
  anchor: a WORM bucket, an email to the auditor, a ticket).
- That the records are true. It proves they were not changed after they were
  recorded, not that what was recorded was correct.
- Anything about content that was redacted or hashed at capture time.
- Who holds the signing key. A record keeper holding the key could have signed a
  rewritten log before any checkpoint was anchored externally.
```

Content: a bundle carries whatever the selected records carry, nothing more. Records captured
with content off or hashed stay that way; there is no new capture.

The format follows the conventions of `ExaminerBundle` / `SignedExaminerBundle` in
`briefcase.compliance` (schema version, `sha256:` content hash over a canonical payload,
signature metadata next to it, a verifier that pins the expected key instead of trusting the
embedded one) but holds many records with chain proofs instead of one routing decision, and
signs locally with Ed25519 instead of AWS KMS.

## Defaults and how to change them

- Records newer than the store's last checkpoint get a **bundle checkpoint** (marked `origin:
  "bundle"` in `checkpoints.json`) that extends the store's checkpoint chain and is signed with
  `--sign-key`. It attests what the bundle creator saw, not the store's own checkpoint; to
  avoid it, checkpoint the store first (`briefcase.shutdown()`, or `backend.checkpoint()`).
- A source that does not verify is refused (exit 1): a bundle from a broken log would prove
  nothing. Run `briefcase verify` on it to see why.
- `--key` may be repeated when the store and the bundle were signed with different keys.
- Verification uses PyNaCl if installed, else the compiled core, else a pure-Python RFC 8032
  verifier, so it works on a bare `pip install briefcase-ai`.

## How to turn it off or undo

Nothing runs in the background. Delete the bundle file. A key file can be deleted, but
bundles and checkpoints signed with it stay verifiable from the public key they carry.

## Limits and failure modes

- A bundle proves inclusion at checkpoint time, not completeness; see the README excerpt above.
- The bundle names its own public keys. Without `--key`, a forger can sign a consistent fake
  bundle with their own key; always pin the key id the record keeper published.
- Selection reads correlation ids where `@capture`, the receiver and decision tags put them;
  records without those ids are matched only by `--since/--until`.
- Large selections produce one JSON line per record; there is no size cap.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: ... is not intact (...)` from `bundle create` | Run `briefcase verify evidence.db` to see what is wrong with the store. |
| `no record in evidence.db matches the selection` | Check the id (`--workflow wf-...`), or select by time with `--since`. |
| `... is signed by key X, not the expected key Y` | Ask the record keeper which key signs their store; pass each key with its own `--key`. |
| `Checkpoints are NOT signed` | Set `BRIEFCASE_SIGNING_KEY` before capturing and pass `--sign-key` to `bundle create`. |
| `already exists` | Bundles and keys are never overwritten; choose a new `--out` / key path. |
| `bundle schema N is newer than this SDK understands` | `pip install -U briefcase-ai`. |
| Exit code 2 in CI | The path is wrong or not evidence (`briefcase verify --json PATH` prints the reason). |
