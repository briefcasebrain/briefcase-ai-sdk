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

:mod:`briefcase.compliance.audit_bundle` follows the same conventions for
many records at once: records cut from a hash-chained store with Merkle
proofs against Ed25519-signed checkpoints (``briefcase bundle create``).
"""

from briefcase.compliance.examiner import ExaminerBundle, BundleIntegrityError
from briefcase.compliance.signed_bundle import SignedExaminerBundle
from briefcase.compliance.audit_bundle import (
    BundleError,
    Selection,
    create_bundle,
    verify_bundle,
)

__all__ = [
    "BundleError",
    "BundleIntegrityError",
    "ExaminerBundle",
    "Selection",
    "SignedExaminerBundle",
    "create_bundle",
    "verify_bundle",
]
