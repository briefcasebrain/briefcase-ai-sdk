# Evidence bundles and `briefcase verify`

One file that proves which decisions were recorded and that they have not changed since,
checkable offline with only the SDK installed. The same format carries a set of records cut
from a tamper-evident store (with Merkle proofs) or a single sealed routing decision with its
policy and evidence (what `ExaminerBundle` builds); `briefcase verify` answers "is this evidence
intact?" for a bundle of any version, a chained store or a chain JSONL file.

Flow: **Verify**. Feature id: `audit-bundle`. Status: experimental.

## The one command

```bash
briefcase bundle create --from evidence.db --out evidence-bundle.zip --sign-key ~/.briefcase/signing-key.jwk
```

The auditor runs:

```bash
briefcase bundle verify evidence-bundle.zip --key KEYID
```

| Command | What it does |
|---|---|
| `briefcase bundle create --from evidence.db --out FILE` | Records from a chained store (or its JSONL export); select with `--session`, `--workflow`, `--trace` (matched on the record's `session_id` / `workflow_id` / `trace_id`, its `tags` or `attributes`, or a decision's tags) and `--since` / `--until` (ISO 8601, chain time). |
| `briefcase bundle create --from examiner.json --out FILE` | Re-packages a format-1 examiner bundle as format 2, keeping its KMS signature. |
| `--sign-key KEY` (repeatable) | An Ed25519 key file (`briefcase bundle keygen`), or `kms:KEY_ID` / a KMS key ARN to sign with AWS KMS (`--kms-algorithm`, default `RSASSA_PSS_SHA_256`). |
| `briefcase bundle verify FILE [--key KEY ...] [--kms]` | Checks any format version. `--key` pins signers (64-hex Ed25519 key id, key file, or `kms:KEY_ID`). `--kms` allows calls to AWS KMS to check KMS signatures. |
| `briefcase bundle inspect FILE` | Shows format version, hash scheme, signers and contents without verifying. |
| `briefcase verify PATH` | The same verdict for a bundle (either version), a chained store or a chain JSONL. |

Exit codes: `0` intact, `1` tampered or incomplete, `2` usage (missing file, unknown format or
version). `--json` prints the full report.

From Python: `briefcase.compliance.create_bundle(...)`, `write_bundle(...)`,
`verify_bundle(path, expected_key_ids=[...], kms_client=...)`, `inspect_bundle(path)`,
`migrate_legacy(old_json, new_zip, signers=[...])`, and `ExaminerBundle.to_bundle(path,
signers=[...])` / `ExaminerBundle.from_bundle(path)`. From TypeScript:
`verifyBundle(zipBytes, { expectedKeyIds })` in `@briefcase-ai/runtime/integrity` (format 2,
Ed25519 only).

### Real output

Captured on 2026-11-22 in a temporary `HOME` (its path shown as `$HOME`), continuing the
session in [chained-store.md](chained-store.md): four synthetic `score_claim` decisions, three
in workflow `wf-claims-42`, captured with `briefcase.observe("chain:evidence.db")` and a
signing key. `edited.zip` is a copy of the bundle with one number in `records.jsonl` changed by
a short Python script. `examiner-bundle.json` is the test fixture
`tests/fixtures/compliance/signed_examiner_bundle_v1.json`: a bundle written by the released
`SignedExaminerBundle` code, signed with an **offline fake KMS** (no AWS call was made anywhere
in this session, so its KMS signature shows as not checked). Commands were run through the
command modules' `register()` (what the CLI's command discovery does).

Records from a chained store:

```text
$ briefcase bundle create --from evidence.db --workflow wf-claims-42 --out wf-claims-42.zip --sign-key $HOME/.briefcase/signing-key.jwk
Wrote wf-claims-42.zip
  3 record(s) of 4 in the source chain, 1 checkpoint(s).
  Checkpoints signed by key 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2.
  Bundle signed by 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2; content hash sha256:e1c7d94d06f5d37498f36e4992b64c71a7cb1d4d99e09db38ad2e17be5249e0f.
Next: briefcase bundle verify wf-claims-42.zip --key 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2
[exit 0]

$ briefcase bundle verify wf-claims-42.zip
INTACT: wf-claims-42.zip (evidence bundle, format 2)
  Checked 3 records (seq 1-3) against 1 checkpoint.
  Signatures: ed25519 9807a955e02e on the bundle: valid; ed25519 9807a955e02e on the checkpoint 1: valid.
  Every file matches the sealed manifest.
  Every chained record matches its chain entry and its Merkle proof leads to a checkpoint.
  Note: The bundle names its own signing key(s). Confirm that 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2 is what the record keeper published, or re-run with --key to pin it.
  Note: A bundle proves its records are unaltered since they were sealed (and, for chained records, that they were in the log at checkpoint time). It does not prove the log or the evidence is complete unless a checkpoint was anchored outside the record keeper's control.
Next: briefcase bundle verify wf-claims-42.zip --key 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2   (pin the keys the record keeper published)
[exit 0]

$ briefcase bundle verify wf-claims-42.zip --key 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2
INTACT: wf-claims-42.zip (evidence bundle, format 2)
  Checked 3 records (seq 1-3) against 1 checkpoint.
  Signatures: ed25519 9807a955e02e on the bundle: valid; ed25519 9807a955e02e on the checkpoint 1: valid.
  Every file matches the sealed manifest.
  Every chained record matches its chain entry and its Merkle proof leads to a checkpoint.
  Note: A bundle proves its records are unaltered since they were sealed (and, for chained records, that they were in the log at checkpoint time). It does not prove the log or the evidence is complete unless a checkpoint was anchored outside the record keeper's control.
Next: keep this bundle with the audit file; anyone can re-run this check offline.
[exit 0]

$ briefcase bundle verify edited.zip
TAMPERED: edited.zip (evidence bundle, format 2)
  Checked 3 records (seq 1-3) against 1 checkpoint.
  Signatures: ed25519 9807a955e02e on the bundle: valid; ed25519 9807a955e02e on the checkpoint 1: valid.
  2 problems:
    - records.jsonl was changed after the bundle was made
    - record d8827aa9-26c6-411d-9997-1ee8728fe56a (entry 2) does not match its chain entry: it was changed
  First broken entry: 2; changed: 2.
  Note: The bundle names its own signing key(s). Confirm that 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2 is what the record keeper published, or re-run with --key to pin it.
  Note: A bundle proves its records are unaltered since they were sealed (and, for chained records, that they were in the log at checkpoint time). It does not prove the log or the evidence is complete unless a checkpoint was anchored outside the record keeper's control.
Next: ask the record keeper for a fresh bundle; do not rely on this one.
[exit 1]

$ briefcase bundle inspect wf-claims-42.zip
wf-claims-42.zip: briefcase-bundle format 2, hash scheme jcs-1, subject records
  Created 2026-11-22T05:41:14.004999Z by briefcase-ai 4.4.0
  Records: 3 records; checkpoints: 1
  Selection: workflow=wf-claims-42
  Signed: ed25519 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2 (Ed25519) at 2026-11-22T05:41:14.007546+00:00
  Content hash: sha256:e1c7d94d06f5d37498f36e4992b64c71a7cb1d4d99e09db38ad2e17be5249e0f
Next: briefcase bundle verify wf-claims-42.zip
[exit 0]
```

A format-1 examiner bundle, checked as is and re-packaged as format 2:

```text
$ briefcase bundle inspect examiner-bundle.json
examiner-bundle.json: briefcase-bundle format 1, hash scheme examiner-compat-1, subject examiner
  Records: 1 decision, 1 evidence, 1 policy; checkpoints: 0
  Policy as of: 2026-04-18T00:00:00+00:00
  Signed: aws-kms arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000001 (RSASSA_PSS_SHA_256) at 2026-11-22T05:31:45.061260+00:00
  Content hash: sha256:452b9636ddeea0e68ba9538e88f490ef9e9b98edff223776a6fa0d42cbd24c50
Next: briefcase bundle verify examiner-bundle.json
[exit 0]

$ briefcase verify examiner-bundle.json
INTACT: examiner-bundle.json (evidence bundle, format 1)
  Checked 3 records against 0 checkpoints.
  Signatures: aws-kms arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000001 on the bundle: not checked.
  The content hash matches the decision, policy and evidence.
  Note: AWS KMS signature by arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000001 not checked: pass the expected KMS key id (--key) and allow KMS calls (--kms)
  Note: The bundle names its own signing key(s). Confirm that arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000001 is what the record keeper published, or re-run with --key to pin it.
  Note: A bundle proves its records are unaltered since they were sealed (and, for chained records, that they were in the log at checkpoint time). It does not prove the log or the evidence is complete unless a checkpoint was anchored outside the record keeper's control.
Next: briefcase bundle verify examiner-bundle.json --key kms:KEY_ID --kms   (check the KMS signature)
[exit 0]

$ briefcase bundle create --from examiner-bundle.json --out decision.zip --sign-key $HOME/.briefcase/signing-key.jwk
Wrote decision.zip (format 2) from the format-1 examiner bundle examiner-bundle.json
  3 record(s): the decision, its policy and the evidence it cited.
  Kept the original AWS KMS signature by arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000001 (it still covers the same content).
  Bundle signed by 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2; content hash sha256:520c2f00e47a34434c3f1c8ba9a7697837a1b8a6d320be56a5ba3e7f9a603592.
Next: briefcase bundle verify decision.zip --key 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2 --key kms:arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000001 --kms
[exit 0]

$ briefcase bundle inspect decision.zip
decision.zip: briefcase-bundle format 2, hash scheme jcs-1, subject examiner
  Created 2026-11-22T05:41:15.577149Z by briefcase-ai 4.4.0
  Records: 1 decision, 1 evidence, 1 policy; checkpoints: 0
  Policy as of: 2026-04-18T00:00:00+00:00
  Original format-1 signature: aws-kms arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000001 (RSASSA_PSS_SHA_256)
  Signed: ed25519 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2 (Ed25519) at 2026-11-22T05:41:15.579927+00:00
  Content hash: sha256:520c2f00e47a34434c3f1c8ba9a7697837a1b8a6d320be56a5ba3e7f9a603592
Next: briefcase bundle verify decision.zip
[exit 0]

$ briefcase bundle verify decision.zip --key 9807a955e02e27829dce8bb6f4fa98cce0b73c0b12c83469fc3be5aeaf58f2c2
INTACT: decision.zip (evidence bundle, format 2)
  Checked 3 records against 0 checkpoints.
  Signatures: ed25519 9807a955e02e on the bundle: valid; aws-kms arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000001 on the original format-1 content: not trusted (key not pinned).
  Every file matches the sealed manifest.
  Note: Signature on the original format-1 content: not checked: KMS key arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000001 is not one you pinned
  Note: A bundle proves its records are unaltered since they were sealed (and, for chained records, that they were in the log at checkpoint time). It does not prove the log or the evidence is complete unless a checkpoint was anchored outside the record keeper's control.
Next: keep this bundle with the audit file; anyone can re-run this check offline.
[exit 0]
```

Usage errors:

```text
$ briefcase verify missing.db
error: missing.db does not exist
fix: check the path, e.g. briefcase verify ./evidence.db
[exit 2]

$ briefcase verify runs.jsonl
error: runs.jsonl has no chain fields (seq/prev_hash/entry_hash, or prior_hash/payload_hash). Plain record files (briefcase.observe('runs.jsonl')) carry no hashes; capture into a chained store with briefcase.observe('chain:evidence.db') instead.
[exit 2]
```

## The format (version 2)

A bundle is a ZIP archive with exactly these members (any other member fails verification):

| File | Content |
|---|---|
| `manifest.json` | The seal, below. |
| `records.jsonl` | One JSON object per line: `role` (`record`, `decision`, `policy` or `evidence`), `record` (the record exactly as stored or built), and for chained records `entry` and `proof`. |
| `checkpoints.json` | Array of the chain checkpoints the proofs point at (`[]` when there are none). |
| `keys.json` | Object: Ed25519 key id → public JWK `{"kty":"OKP","crv":"Ed25519","x":...}`. |
| `README.txt` | What the bundle proves and does not prove, in plain language. |

`manifest.json` fields:

| Field | Meaning |
|---|---|
| `format` | `"briefcase-bundle"` |
| `format_version` | `2` (verifiers refuse versions they do not know) |
| `hash_scheme` | `"jcs-1"`: RFC 8785 canonical JSON, see [hashing-v2.md](hashing-v2.md) |
| `subject` | `"records"` (cut from a chained store) or `"examiner"` (one sealed decision) |
| `created_at`, `created_by` | When and by which SDK version |
| `source`, `selection` | Where the records came from and how they were selected (`null` when not applicable) |
| `context` | Subject-specific facts. Examiner bundles: `as_of_transaction_time`, `examiner_schema_version`, `examiner_content_hash`, `metadata`, and `legacy_signatures` when re-packaged from a signed format-1 bundle |
| `counts` | `{"records": n, "checkpoints": m}` |
| `keys` | Sorted key ids present in `keys.json` |
| `signers` | `[{type, key_id, algorithm}]` of the signatures below, in order. Sealed, so a signature cannot be removed or swapped for another key's without detection |
| `files` | Member name → lowercase hex SHA-256 of its bytes, for every member except `manifest.json` |
| `content_hash` | `"sha256:" + hex(SHA-256(JCS(manifest without content_hash and signatures)))` |
| `signatures` | `[{type, key_id, algorithm, value, signed_at, ...}]`; each signs the **32 raw bytes** of the `content_hash` digest |

Signature types:

| `type` | Fields | Verification |
|---|---|---|
| `ed25519` | `public_key` (JWK `x`), `value` (base64url, no padding) | Ed25519 (RFC 8032) over the 32 digest bytes with `public_key`; `key_id` must equal the RFC 7638 thumbprint of the key (hex SHA-256 of `{"crv":"Ed25519","kty":"OKP","x":"..."}`). Works offline. |
| `aws-kms` | `key_id` (KMS key id or ARN), `algorithm` (e.g. `RSASSA_PSS_SHA_256`), `value` (base64) | AWS KMS `Verify` with `MessageType=RAW`, called with the **pinned** key id and algorithm, never the ones in the bundle. Needs AWS access; reported `unchecked` otherwise. |
| anything else | as the signer defines | A `briefcase.compliance.signers.Signer` (`type` plus `sign(message) -> dict`) and a verifier registered with `register_verifier(type, fn)`; unknown types are reported `unchecked`. |

Chained records (`subject: "records"`): `entry` holds `seq`, `record_id`, `kind`, `hash_scheme`,
`prev_hash`, `record_hash`, `entry_hash`, `recorded_at`; `proof` holds `checkpoint_no`,
`leaf_index`, `tree_size`, `audit_path` (hex). A verifier checks
`record_hash = SHA-256(JCS(record))`,
`entry_hash = SHA-256(JCS({v:1, hash_scheme, seq, record_id, kind, prev_hash, record_hash, recorded_at}))`,
folds `audit_path` with the RFC 6962 rules (leaf `SHA-256(0x00‖entry_hash bytes)`, node
`SHA-256(0x01‖left‖right)`, split at the largest power of two below the size) to the
checkpoint's `merkle_root`, with `leaf_index = seq - from_seq` and
`tree_size = seq - from_seq + 1` of the checkpoint. Each checkpoint's `checkpoint_hash` is
`SHA-256(JCS({v:1, hash_scheme, checkpoint_no, from_seq, seq, head_hash, merkle_root,
prev_checkpoint_hash, created_at, key_id}))`; its Ed25519 `signature` covers the 32 bytes of
that hash; consecutive checkpoints must chain (`prev_checkpoint_hash`, `from_seq`). Checkpoints
marked `origin: "bundle"` were made by `bundle create` for records the store had not
checkpointed yet; they attest what the bundle creator saw.

Examiner bundles (`subject: "examiner"`): one `decision` line, at most one `policy` line, and
the `evidence` lines. A `legacy_signatures` entry is a format-1 KMS signature; it covers the
UTF-8 bytes of `examiner_content_hash`, which the verifier recomputes from the records with the
format-1 rule below.

### Format version 1 (the released examiner JSON)

The JSON object `ExaminerBundle.to_json()` / `SignedExaminerBundle.to_json()` have always
written: `decision`, `policy`, `evidence`, `as_of_transaction_time`, `schema_version`,
`metadata`, `content_hash`, and for signed bundles `kms_key_id`, `signature` (base64),
`signing_algorithm`, `signed_at`. `content_hash` is `"sha256:" + SHA-256` over
`json.dumps({schema_version, decision, policy, evidence, as_of_transaction_time},
sort_keys=True, default=str, separators=(",", ":"))` (hash scheme `examiner-compat-1`; ASCII
escapes, Python float repr); the KMS signature covers the UTF-8 bytes of `content_hash`.
`metadata` and `signed_at` are not covered. `briefcase verify` and `verify_bundle` accept it
unchanged.

## What a passing check proves

- No member of the bundle changed after it was sealed (file hashes, `content_hash`).
- Who sealed it: each signature by a pinned key verifies. Signatures by keys you did not pin
  are reported "not trusted", and a bundle signed only by unpinned keys fails
  (`unexpected_key`). Without `--key`, the report names the keys so a person can compare them
  with the ones the record keeper published.
- For chained records: each record is byte for byte what was written to the hash chain, and
  was in the log when its checkpoint was made (Merkle proof to a signed checkpoint root).
- For a re-packaged signed examiner bundle: the decision, policy and evidence are exactly what
  the original KMS signature covered.

It does **not** prove that the log or the evidence is complete (records outside the
selection, or evidence a decision relied on but did not cite, are not there; a log can be cut
or rebuilt before a checkpoint unless that checkpoint was anchored outside the record keeper's
reach), that the records are true, anything about content redacted or hashed at capture time,
or who controls a signing key. A bundle carries whatever the records carry; nothing new is
captured.

## Migrating from the old bundle

- Existing JSON bundles (format 1, signed or not) keep verifying: `briefcase verify old.json`,
  `verify_bundle("old.json")`, `ExaminerBundle.from_json(...).verify()` and
  `SignedExaminerBundle.verify_signature(expected_key_id=...)` all work as before.
- `ExaminerBundle` and `SignedExaminerBundle` keep their API and still write format 1 byte for
  byte (`to_json`); signing and verification now go through the shared signer layer
  (`KmsSigner`). New: `to_bundle(path, signers=[...])`, `from_bundle(path)`, and a `kms_client=`
  argument next to `boto_session=`. No deprecation.
- To move a file to format 2: `briefcase bundle create --from old.json --out new.zip
  [--sign-key KEY]` (or `migrate_legacy`). The original KMS signature is kept and still
  checked; no KMS access is needed to migrate. A source whose content hash does not match is
  refused.

## What it writes, and where

`bundle create` writes one new ZIP at `--out` (never overwrites) and reads the source
read-only. `bundle keygen` writes one new private key file, mode 0600. `verify
--save-checkpoint FILE` writes the store's latest checkpoint to a new file. `kms:` signers and
`--kms` call AWS KMS with your AWS credentials; nothing else leaves the machine.

## Defaults and how to change them

- Verification uses PyNaCl if installed, else the compiled core, else a pure-Python RFC 8032
  verifier, so Ed25519 checks work on a bare `pip install briefcase-ai`. KMS checks need
  `briefcase-ai[compliance-kms]` (boto3) and `--kms`.
- Accepted KMS algorithms: `RSASSA_PSS_SHA_256` unless `--kms-algorithm` (repeatable) says
  otherwise.
- `bundle create` refuses a source that does not verify (exit 1): a bundle from a broken log
  would prove nothing.
- Every checkpoint in a bundle must be signed by a pinned key when you pin keys: pass the store
  key and the bundle key if they differ.

## How to turn it off or undo

Nothing runs in the background. Delete the bundle file. A key file can be deleted, but bundles
signed with it stay verifiable from the public key they carry.

## Limits and failure modes

- KMS signatures can only be checked online, by someone allowed to call `kms:Verify` on that
  key. The TypeScript verifier reports them `unchecked` and does not read format 1.
- The bundle names its own Ed25519 keys; without `--key`, a forger can produce a consistent
  bundle with their own key. Always pin the key ids the record keeper published.
- `signed_at` is informational (not covered by the signature), as in format 1.
- Selection reads correlation ids where `@capture`, the receiver and decision tags put them.
- Large selections produce one JSON line per record; there is no size cap.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: ... is not intact (...)` from `bundle create` | Run `briefcase verify evidence.db` to see what is wrong with the store. |
| `no record in evidence.db matches the selection` | Check the id (`--workflow wf-...`), or select by time with `--since`. |
| `aws-kms ... not checked` | Pin the key and allow the call: `--key kms:KEY_ID --kms` (needs AWS credentials and boto3). |
| `signed only by key(s) you did not pin` | The bundle was sealed by a different key than you expected; ask the record keeper which key they use. Do not rely on it until that matches. |
| `checkpoint N is signed by key X, not an expected key` | The store key differs from the bundle key; pass each with its own `--key`. |
| `bundle format version N is newer than this SDK understands` | `pip install -U briefcase-ai`. |
| `pre-release audit bundle` | Made by an unreleased build; re-create it with `briefcase bundle create`. |
| `already exists` | Bundles and keys are never overwritten; choose a new `--out` / key path. |
| Exit code 2 in CI | The path is wrong or not evidence (`briefcase verify --json PATH` prints the reason). |
