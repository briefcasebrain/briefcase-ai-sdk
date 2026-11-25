"""Regenerate the format-2 bundle fixtures the TypeScript verifier is tested on.

    python -m tests.fixtures.bundles.generate      # from the repository root

Writes records-v2.zip (chained records with Merkle proofs, Ed25519 seal),
examiner-v2.zip (the released format-1 KMS fixture re-packaged, original KMS
signature kept, Ed25519 seal) and kms-v2.zip (sealed by the offline fake KMS with a key generated on the
spot, so its signature can no longer be checked: it exercises "unchecked").
The Ed25519 key is the test seed bytes(range(32)); it protects nothing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from briefcase.compliance import BundleRecord, Ed25519Signer, KmsSigner, create_bundle, migrate_legacy, write_bundle
from tests.compliance.fake_kms import SIGNING_KEY_ID, FakeKms
from tests.integrity.chain_db import ChainWriter

HERE = Path(__file__).resolve().parent
SEED = bytes(range(32))


def main() -> None:
    for name in ("records-v2.zip", "examiner-v2.zip", "kms-v2.zip"):
        (HERE / name).unlink(missing_ok=True)
    signer = Ed25519Signer(SEED)
    with tempfile.TemporaryDirectory() as tmp:
        writer = ChainWriter(Path(tmp) / "evidence.db", SEED)
        for i in range(5):
            writer.append("d-%d" % i, {"decision_id": "d-%d" % i, "score": 0.1 * i, "text": "Grüße 😀",
                                       "workflow_id": "wf-1"})
        writer.checkpoint()
        writer.append("d-5", {"decision_id": "d-5", "score": 1e21, "workflow_id": "wf-1"})
        writer.close()
        create_bundle(writer.path, HERE / "records-v2.zip", signers=[signer])
    migrate_legacy(HERE.parent / "compliance" / "signed_examiner_bundle_v1.json", HERE / "examiner-v2.zip",
                   signers=[signer])
    write_bundle(HERE / "kms-v2.zip", [BundleRecord({"decision_id": "k-1", "confidence": 0.5}, role="decision")],
                 subject="examiner", signers=[KmsSigner(SIGNING_KEY_ID, kms_client=FakeKms.with_fixture_key())])
    print("key id:", signer.key_id)


if __name__ == "__main__":
    main()
