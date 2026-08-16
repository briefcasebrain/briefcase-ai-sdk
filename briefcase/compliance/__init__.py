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
"""

from briefcase.compliance.examiner import ExaminerBundle, BundleIntegrityError
from briefcase.compliance.signed_bundle import SignedExaminerBundle

__all__ = ["ExaminerBundle", "BundleIntegrityError", "SignedExaminerBundle"]
