"""
External data source versioning components.
"""

try:
    from briefcase.external_data.tracker import (
        ExternalDataTracker,
        Snapshot,
        SnapshotPolicy,
        SnapshotFrequency,
        DriftReport,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.external_data requires the 'external' extra.\n"
        "Install it with: pip install briefcase-ai[external]"
    ) from exc

__all__ = [
    "ExternalDataTracker",
    "Snapshot",
    "SnapshotPolicy",
    "SnapshotFrequency",
    "DriftReport",
]
