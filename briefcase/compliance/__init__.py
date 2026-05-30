"""
Compliance artifact packaging.

The :class:`ExaminerBundle` is a reproducible artifact joining the three
things an examiner needs to reconstruct an agentic decision:

* the decision record itself (what was decided),
* the bitemporal evidence that informed it (what was known at the time),
* the policy version that was in effect (which rule fired, which version).

Export to JSON for transport; re-import and verify integrity via a single
content hash. The bundle is the operational output of the "examiner
replay" pattern described in the Steve Cannon and Bridge.xyz notes.
"""

from briefcase.compliance.examiner import ExaminerBundle, BundleIntegrityError

__all__ = ["ExaminerBundle", "BundleIntegrityError"]
