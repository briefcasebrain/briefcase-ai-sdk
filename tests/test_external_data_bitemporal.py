"""Regression tests for the bitemporal extensions on ExternalDataTracker."""

from datetime import datetime, timezone, timedelta

import pytest

from briefcase.external_data.tracker import ExternalDataTracker, SnapshotPolicy, SnapshotFrequency


UTC = timezone.utc


def _ts(days: int = 0) -> datetime:
    return datetime(2026, 4, 17, tzinfo=UTC) + timedelta(days=days)


def test_snapshot_defaults_valid_time_to_timestamp():
    tracker = ExternalDataTracker(
        default_policy=SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL)
    )
    result = tracker.track_api_call(
        api_name="ofac_sdn",
        endpoint="https://example/sdn",
        method="GET",
        response_data={"records": []},
    )
    assert result["snapshot_stored"]
    snap = tracker.get_latest_snapshot("ofac_sdn")
    assert snap.valid_time is not None
    # When no valid_time was supplied, it defaults to the transaction time.
    assert snap.valid_time == snap.timestamp


def test_snapshot_carries_supplied_valid_time():
    tracker = ExternalDataTracker(
        default_policy=SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL)
    )
    vt = _ts(-30)
    tracker.track_api_call(
        api_name="bloomberg_prices",
        endpoint="https://example/px",
        method="GET",
        response_data={"px": 1.0001},
        valid_time=vt,
        source_trust_level="primary",
    )
    snap = tracker.get_latest_snapshot("bloomberg_prices")
    assert snap.valid_time == vt.isoformat()
    assert snap.source_trust_level == "primary"


def test_correct_snapshot_appends_with_parent_lineage():
    tracker = ExternalDataTracker(
        default_policy=SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL)
    )
    original = tracker.track_api_call(
        api_name="bloomberg",
        endpoint="/px",
        method="GET",
        response_data={"px": 1.0001},
        valid_time=_ts(-10),
    )
    parent_id = original["snapshot_id"]
    correction = tracker.correct_snapshot(parent_id, {"px": 1.0002})
    assert correction.parent_snapshot_id == parent_id
    assert correction.metadata.get("correction_of") == parent_id
    # Valid time preserved from parent.
    parent = tracker.get_latest_snapshot("bloomberg")  # now the correction
    assert correction.snapshot_id == parent.snapshot_id
    assert correction.valid_time == _ts(-10).isoformat()


def test_correct_snapshot_raises_for_unknown_parent():
    tracker = ExternalDataTracker()
    with pytest.raises(LookupError):
        tracker.correct_snapshot("nonexistent_id", {"v": 1})
