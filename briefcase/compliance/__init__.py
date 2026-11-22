"""
Compliance artifact packaging.

The :class:`ExaminerBundle` is a reproducible artifact joining the three
things an examiner needs to reconstruct an agentic decision:

* the decision record itself (what was decided),
* the bitemporal evidence that informed it (what was known at the time),
* the policy version that was in effect (which rule fired, which version).

Export to JSON for transport; re-import and verify integrity via a single
content hash. The bundle supports examiner replay: reconstructing the
inputs a decision saw, as of the decision's transaction time.

Both are one format: :mod:`briefcase.compliance.bundle` defines the
versioned evidence bundle (format 2, a ZIP holding one or many records,
optional chain proofs, and signatures from pluggable signers: Ed25519 key
files, AWS KMS, or your own :class:`Signer`) and the single verifier,
:func:`verify_bundle`, which also accepts the format-1 JSON that
``ExaminerBundle.to_json()`` writes. ``ExaminerBundle.to_bundle(path)``
writes format 2; :func:`create_bundle` cuts one from a chained store.
"""

from briefcase.compliance.examiner import ExaminerBundle, BundleIntegrityError
from briefcase.compliance.signed_bundle import SignedExaminerBundle
from briefcase.compliance.audit_bundle import Selection, create_bundle
from briefcase.compliance.bundle import (
    BundleError,
    BundleRecord,
    inspect_bundle,
    migrate_legacy,
    verify_bundle,
    write_bundle,
)
from briefcase.compliance.signers import (
    Ed25519Signer,
    KmsSigner,
    Signer,
    register_verifier,
)

__all__ = [
    "BundleError",
    "BundleIntegrityError",
    "BundleRecord",
    "Ed25519Signer",
    "ExaminerBundle",
    "KmsSigner",
    "Selection",
    "SignedExaminerBundle",
    "Signer",
    "create_bundle",
    "inspect_bundle",
    "migrate_legacy",
    "register_verifier",
    "verify_bundle",
    "write_bundle",
]
