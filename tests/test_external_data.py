"""
Tests for external data versioning: tracker, snapshot policies, drift detection.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from briefcase.external_data.tracker import (
    DriftReport,
    ExternalDataTracker,
    Snapshot,
    SnapshotFrequency,
    SnapshotPolicy,
)


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def tracker():
    """Tracker with no lakeFS client (in-memory only)."""
    return ExternalDataTracker()


@pytest.fixture
def tracker_with_lakefs():
    """Tracker wired to a mock lakeFS client."""
    mock = MagicMock()
    return ExternalDataTracker(lakefs_client=mock, repository="test-repo", branch="main")


@pytest.fixture
def sample_response():
    return {"users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]}


@pytest.fixture
def sample_response_v2():
    return {"users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}, {"id": 3, "name": "Charlie"}]}


def _hash(data):
    """Helper: SHA-256 of JSON-serialized data."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


class VersionedClientDouble:
    """Fake with VersionedClient's exact method signatures."""

    def __init__(self):
        self.uploads = {}

    def upload_object(self, path, data, content_type="application/octet-stream"):
        self.uploads[path] = data

    def get_commit(self):
        return "c0ffee0000000000000000000000000000000000"


# =========================================================================
# 1. Dataclass tests
# =========================================================================

class TestSnapshotDataclass:
    def test_to_dict_excludes_none(self):
        snap = Snapshot(
            snapshot_id="s1",
            source_name="api_x",
            source_type="api",
            data_hash="abc123",
            timestamp="2025-01-01T00:00:00",
            size_bytes=100,
        )
        d = snap.to_dict()
        assert "record_count" not in d
        assert "lakefs_path" not in d
        assert d["snapshot_id"] == "s1"

    def test_to_dict_includes_present_values(self):
        snap = Snapshot(
            snapshot_id="s1",
            source_name="api_x",
            source_type="api",
            data_hash="abc123",
            timestamp="2025-01-01T00:00:00",
            size_bytes=100,
            record_count=42,
            lakefs_path="snapshots/api_x/s1.json",
        )
        d = snap.to_dict()
        assert d["record_count"] == 42
        assert d["lakefs_path"] == "snapshots/api_x/s1.json"

    def test_from_dict_round_trip(self):
        snap = Snapshot(
            snapshot_id="s1",
            source_name="api_x",
            source_type="api",
            data_hash="abc123",
            timestamp="2025-01-01T00:00:00",
            size_bytes=100,
            metadata={"k": "v"},
        )
        d = snap.to_dict()
        restored = Snapshot.from_dict(d)
        assert restored.snapshot_id == snap.snapshot_id
        assert restored.metadata == {"k": "v"}

    def test_from_dict_ignores_extra_keys(self):
        d = {
            "snapshot_id": "s1",
            "source_name": "x",
            "source_type": "api",
            "data_hash": "abc",
            "timestamp": "t",
            "size_bytes": 10,
            "unknown_field": "ignored",
        }
        snap = Snapshot.from_dict(d)
        assert snap.snapshot_id == "s1"


class TestSnapshotPolicyDefaults:
    def test_defaults(self):
        p = SnapshotPolicy()
        assert p.frequency == SnapshotFrequency.ON_CHANGE
        assert p.retention_days == 90
        assert p.change_threshold == 0.0
        assert p.max_snapshots == 0
        assert p.compress is False


class TestDriftReportDataclass:
    def test_to_dict(self):
        r = DriftReport(
            source_name="src",
            baseline_snapshot_id="a",
            current_snapshot_id="b",
            baseline_hash="h1",
            current_hash="h2",
            has_changed=True,
            size_delta=50,
            drift_score=1.0,
        )
        d = r.to_dict()
        assert d["has_changed"] is True
        assert d["drift_score"] == 1.0
        assert "timestamp" in d


class TestSnapshotFrequencyEnum:
    def test_values(self):
        assert SnapshotFrequency.EVERY_CALL.value == "every_call"
        assert SnapshotFrequency.ON_CHANGE.value == "on_change"
        assert SnapshotFrequency.HOURLY.value == "hourly"
        assert SnapshotFrequency.DAILY.value == "daily"
        assert SnapshotFrequency.WEEKLY.value == "weekly"


# =========================================================================
# 2. Policy management
# =========================================================================

class TestPolicyManagement:
    def test_default_policy(self, tracker):
        p = tracker.get_policy("unknown_source")
        assert p.frequency == SnapshotFrequency.ON_CHANGE

    def test_set_and_get_policy(self, tracker):
        policy = SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL, retention_days=30)
        tracker.set_policy("my_api", policy)
        assert tracker.get_policy("my_api").frequency == SnapshotFrequency.EVERY_CALL
        assert tracker.get_policy("my_api").retention_days == 30

    def test_custom_default_policy(self):
        custom = SnapshotPolicy(frequency=SnapshotFrequency.DAILY, retention_days=7)
        t = ExternalDataTracker(default_policy=custom)
        assert t.get_policy("anything").frequency == SnapshotFrequency.DAILY

    def test_set_change_detector(self, tracker):
        def detector(old, new):
            return 0.5
        tracker.set_change_detector("src", detector)
        assert "src" in tracker._change_detectors


# =========================================================================
# 3. track_api_call
# =========================================================================

class TestTrackApiCall:
    def test_basic_call_returns_hash_and_timestamp(self, tracker, sample_response):
        result = tracker.track_api_call(
            api_name="ofac",
            endpoint="https://api.treasury.gov/sdn",
            method="GET",
            response_data=sample_response,
        )
        assert result["data_hash"] == _hash(sample_response)
        assert result["timestamp"] is not None
        assert result["snapshot_stored"] is True
        assert result["snapshot_id"] is not None

    def test_snapshot_not_stored_when_disabled(self, tracker, sample_response):
        result = tracker.track_api_call(
            api_name="ofac",
            endpoint="/sdn",
            method="GET",
            response_data=sample_response,
            store_snapshot=False,
        )
        assert result["snapshot_stored"] is False
        assert result["snapshot_id"] is None

    def test_duplicate_data_not_stored_on_change_policy(self, tracker, sample_response):
        """ON_CHANGE policy: second identical call should NOT store a new snapshot."""
        tracker.track_api_call("ofac", "/sdn", "GET", sample_response)
        r2 = tracker.track_api_call("ofac", "/sdn", "GET", sample_response)
        assert r2["snapshot_stored"] is False
        assert r2["drift_detected"] is False
        assert tracker.get_snapshot_count("ofac") == 1

    def test_changed_data_stores_new_snapshot(self, tracker, sample_response, sample_response_v2):
        tracker.track_api_call("ofac", "/sdn", "GET", sample_response)
        r2 = tracker.track_api_call("ofac", "/sdn", "GET", sample_response_v2)
        assert r2["snapshot_stored"] is True
        assert r2["drift_detected"] is True
        assert tracker.get_snapshot_count("ofac") == 2

    def test_every_call_policy_always_stores(self, tracker, sample_response):
        tracker.set_policy("ofac", SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL))
        r1 = tracker.track_api_call("ofac", "/sdn", "GET", sample_response)
        r2 = tracker.track_api_call("ofac", "/sdn", "GET", sample_response)
        assert r1["snapshot_stored"] is True
        assert r2["snapshot_stored"] is True
        assert tracker.get_snapshot_count("ofac") == 2

    def test_version_included_in_metadata(self, tracker, sample_response):
        tracker.track_api_call("ofac", "/sdn", "GET", sample_response, version="v2")
        snap = tracker.get_latest_snapshot("ofac")
        assert snap.metadata["api_version"] == "v2"

    def test_record_count_stored(self, tracker, sample_response):
        tracker.track_api_call("ofac", "/sdn", "GET", sample_response, record_count=2)
        snap = tracker.get_latest_snapshot("ofac")
        assert snap.record_count == 2

    def test_status_code_in_metadata(self, tracker, sample_response):
        tracker.track_api_call("ofac", "/sdn", "GET", sample_response, status_code=201)
        snap = tracker.get_latest_snapshot("ofac")
        assert snap.metadata["status_code"] == 201

    def test_lakefs_upload_called(self, tracker_with_lakefs, sample_response):
        tracker_with_lakefs.track_api_call("ofac", "/sdn", "GET", sample_response)
        tracker_with_lakefs.lakefs.upload_object.assert_called_once()
        call_args = tracker_with_lakefs.lakefs.upload_object.call_args
        assert "snapshots/ofac/" in call_args[0][0]
        assert isinstance(call_args[0][1], bytes)

    def test_lakefs_upload_failure_doesnt_crash(self, tracker_with_lakefs, sample_response):
        tracker_with_lakefs.lakefs.upload_object.side_effect = Exception("connection refused")
        result = tracker_with_lakefs.track_api_call("ofac", "/sdn", "GET", sample_response)
        # Snapshot is still created in memory, just no lakefs_path
        assert result["snapshot_stored"] is True
        snap = tracker_with_lakefs.get_latest_snapshot("ofac")
        assert snap.lakefs_path is None

    def test_upload_matches_versioned_client_signature(self, sample_response):
        """The tracker calls upload_object(path, data) as defined by
        VersionedClient, so uploads reach a real client."""
        lakefs = VersionedClientDouble()
        tracker = ExternalDataTracker(
            lakefs_client=lakefs, repository="test-repo", branch="main"
        )
        tracker.track_api_call("ofac", "/sdn", "GET", sample_response)
        snap = tracker.get_latest_snapshot("ofac")
        assert snap.lakefs_path is not None
        assert snap.lakefs_path in lakefs.uploads
        assert isinstance(lakefs.uploads[snap.lakefs_path], bytes)

    def test_parent_snapshot_linked(self, tracker, sample_response, sample_response_v2):
        tracker.track_api_call("ofac", "/sdn", "GET", sample_response)
        first_id = tracker.get_latest_snapshot("ofac").snapshot_id
        tracker.track_api_call("ofac", "/sdn", "GET", sample_response_v2)
        second = tracker.get_latest_snapshot("ofac")
        assert second.parent_snapshot_id == first_id


# =========================================================================
# 4. track_db_query
# =========================================================================

class TestTrackDbQuery:
    def test_basic_query_no_data(self, tracker):
        result = tracker.track_db_query("postgresql", "analytics", "SELECT 1", result_count=1)
        assert result["query_hash"] is not None
        assert result["result_count"] == 1
        assert result["data_hash"] is None
        assert result["snapshot_stored"] is False

    def test_query_with_result_data_no_snapshot(self, tracker):
        data = [{"id": 1, "val": "foo"}]
        result = tracker.track_db_query("postgresql", "analytics", "SELECT *", result_data=data, result_count=1)
        assert result["data_hash"] == _hash(data)
        assert result["snapshot_stored"] is False

    def test_query_with_snapshot(self, tracker):
        data = [{"id": 1}]
        result = tracker.track_db_query("postgresql", "analytics", "SELECT *",
                                        result_data=data, result_count=1, store_snapshot=True)
        assert result["snapshot_stored"] is True
        assert result["snapshot_id"] is not None
        assert tracker.get_snapshot_count("postgresql.analytics") == 1

    def test_db_source_name_format(self, tracker):
        data = [{"id": 1}]
        tracker.track_db_query("mysql", "orders", "SELECT *",
                               result_data=data, result_count=1, store_snapshot=True)
        assert "mysql.orders" in tracker.get_all_sources()

    def test_query_hash_deterministic(self, tracker):
        r1 = tracker.track_db_query("pg", "db", "SELECT * FROM t WHERE id = ?")
        r2 = tracker.track_db_query("pg", "db", "SELECT * FROM t WHERE id = ?")
        assert r1["query_hash"] == r2["query_hash"]

    def test_different_queries_different_hashes(self, tracker):
        r1 = tracker.track_db_query("pg", "db", "SELECT * FROM t")
        r2 = tracker.track_db_query("pg", "db", "SELECT * FROM t WHERE id = 1")
        assert r1["query_hash"] != r2["query_hash"]

    def test_db_snapshot_carries_valid_time_and_trust_level(self, tracker):
        vt = datetime(2025, 6, 1, tzinfo=timezone.utc)
        result = tracker.track_db_query(
            "postgresql", "analytics", "SELECT *",
            result_data=[{"id": 1}], result_count=1, store_snapshot=True,
            valid_time=vt, source_trust_level="primary",
        )
        assert result["snapshot_stored"] is True
        snap = tracker.get_latest_snapshot("postgresql.analytics")
        assert snap.valid_time == vt.isoformat()
        assert snap.source_trust_level == "primary"


# =========================================================================
# 5. track_file_fetch
# =========================================================================

class TestTrackFileFetch:
    def test_basic_file_fetch(self, tracker):
        data = b"hello world"
        result = tracker.track_file_fetch("s3_export", data)
        assert result["data_hash"] == hashlib.sha256(data).hexdigest()
        assert result["size_bytes"] == 11
        assert result["snapshot_stored"] is True

    def test_file_path_in_metadata(self, tracker):
        tracker.track_file_fetch("s3_export", b"data", file_path="s3://bucket/file.csv")
        snap = tracker.get_latest_snapshot("s3_export")
        assert snap.metadata["file_path"] == "s3://bucket/file.csv"

    def test_file_record_count(self, tracker):
        tracker.track_file_fetch("s3_export", b"data", record_count=1000)
        snap = tracker.get_latest_snapshot("s3_export")
        assert snap.record_count == 1000

    def test_empty_file(self, tracker):
        result = tracker.track_file_fetch("empty", b"")
        assert result["size_bytes"] == 0
        assert result["snapshot_stored"] is True

    def test_on_change_no_duplicate(self, tracker):
        tracker.track_file_fetch("src", b"data_v1")
        r2 = tracker.track_file_fetch("src", b"data_v1")
        assert r2["snapshot_stored"] is False
        assert tracker.get_snapshot_count("src") == 1

    def test_on_change_stores_when_changed(self, tracker):
        tracker.track_file_fetch("src", b"data_v1")
        r2 = tracker.track_file_fetch("src", b"data_v2")
        assert r2["snapshot_stored"] is True
        assert r2["drift_detected"] is True
        assert tracker.get_snapshot_count("src") == 2

    def test_file_snapshot_carries_valid_time_and_trust_level(self, tracker):
        vt = datetime(2025, 6, 1, tzinfo=timezone.utc)
        tracker.track_file_fetch(
            "s3_export", b"data",
            valid_time=vt, source_trust_level="derived",
        )
        snap = tracker.get_latest_snapshot("s3_export")
        assert snap.valid_time == vt.isoformat()
        assert snap.source_trust_level == "derived"


# =========================================================================
# 6. Snapshot policy evaluation
# =========================================================================

class TestSnapshotPolicyEvaluation:
    def test_first_call_always_stores(self, tracker, sample_response):
        """No previous snapshot  always store."""
        result = tracker.track_api_call("new_api", "/ep", "GET", sample_response)
        assert result["snapshot_stored"] is True

    def test_on_change_identical_data_skips(self, tracker, sample_response):
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        r2 = tracker.track_api_call("src", "/ep", "GET", sample_response)
        assert r2["snapshot_stored"] is False
        assert r2["drift_detected"] is False

    def test_on_change_with_threshold_skips_minor_change(self, tracker):
        """Custom detector returns score below threshold  skip snapshot."""
        tracker.set_policy("src", SnapshotPolicy(
            frequency=SnapshotFrequency.ON_CHANGE,
            change_threshold=0.5,
        ))
        tracker.set_change_detector("src", lambda old, new: 0.2)

        tracker.track_api_call("src", "/ep", "GET", {"v": 1})
        r2 = tracker.track_api_call("src", "/ep", "GET", {"v": 2})
        # Data changed but score 0.2 < threshold 0.5
        assert r2["drift_detected"] is True
        assert r2["snapshot_stored"] is False

    def test_on_change_with_threshold_stores_major_change(self, tracker):
        tracker.set_policy("src", SnapshotPolicy(
            frequency=SnapshotFrequency.ON_CHANGE,
            change_threshold=0.5,
        ))
        tracker.set_change_detector("src", lambda old, new: 0.8)

        tracker.track_api_call("src", "/ep", "GET", {"v": 1})
        r2 = tracker.track_api_call("src", "/ep", "GET", {"v": 2})
        assert r2["drift_detected"] is True
        assert r2["snapshot_stored"] is True

    def test_hourly_frequency_gates_by_time(self, tracker, sample_response, sample_response_v2):
        tracker.set_policy("src", SnapshotPolicy(frequency=SnapshotFrequency.HOURLY))
        tracker.track_api_call("src", "/ep", "GET", sample_response)

        # Second call within the hour  should NOT store
        r2 = tracker.track_api_call("src", "/ep", "GET", sample_response_v2)
        assert r2["snapshot_stored"] is False

    def test_hourly_frequency_stores_after_interval(self, tracker, sample_response, sample_response_v2):
        tracker.set_policy("src", SnapshotPolicy(frequency=SnapshotFrequency.HOURLY))
        tracker.track_api_call("src", "/ep", "GET", sample_response)

        # Manually set last snapshot time to >1 hour ago
        tracker._last_snapshot_times["src"] = datetime.now(timezone.utc) - timedelta(hours=2)

        r2 = tracker.track_api_call("src", "/ep", "GET", sample_response_v2)
        assert r2["snapshot_stored"] is True

    def test_daily_frequency(self, tracker, sample_response, sample_response_v2):
        tracker.set_policy("src", SnapshotPolicy(frequency=SnapshotFrequency.DAILY))
        tracker.track_api_call("src", "/ep", "GET", sample_response)

        # Within same day
        r2 = tracker.track_api_call("src", "/ep", "GET", sample_response_v2)
        assert r2["snapshot_stored"] is False

        # Force time past interval
        tracker._last_snapshot_times["src"] = datetime.now(timezone.utc) - timedelta(days=2)
        r3 = tracker.track_api_call("src", "/ep", "GET", sample_response_v2)
        assert r3["snapshot_stored"] is True

    def test_weekly_frequency(self, tracker, sample_response, sample_response_v2):
        tracker.set_policy("src", SnapshotPolicy(frequency=SnapshotFrequency.WEEKLY))
        tracker.track_api_call("src", "/ep", "GET", sample_response)

        # Within same week
        r2 = tracker.track_api_call("src", "/ep", "GET", sample_response_v2)
        assert r2["snapshot_stored"] is False

        tracker._last_snapshot_times["src"] = datetime.now(timezone.utc) - timedelta(weeks=2)
        r3 = tracker.track_api_call("src", "/ep", "GET", sample_response_v2)
        assert r3["snapshot_stored"] is True

    def test_change_detector_exception_falls_through(self, tracker):
        """If custom detector raises, we still store the snapshot."""
        tracker.set_policy("src", SnapshotPolicy(
            frequency=SnapshotFrequency.ON_CHANGE,
            change_threshold=0.5,
        ))
        tracker.set_change_detector("src", lambda old, new: 1/0)

        tracker.track_api_call("src", "/ep", "GET", {"v": 1})
        r2 = tracker.track_api_call("src", "/ep", "GET", {"v": 2})
        # Detector crashed  fall through to store
        assert r2["snapshot_stored"] is True


# =========================================================================
# 7. Drift detection
# =========================================================================

class TestDriftDetection:
    def test_no_previous_snapshot_returns_none(self, tracker):
        report = tracker.detect_drift("unknown", current_data={"a": 1})
        assert report is None

    def test_drift_detected_with_data(self, tracker, sample_response, sample_response_v2):
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        report = tracker.detect_drift("src", current_data=sample_response_v2)
        assert report is not None
        assert report.has_changed is True
        assert report.drift_score == 1.0
        assert report.current_hash == _hash(sample_response_v2)

    def test_no_drift_with_same_data(self, tracker, sample_response):
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        report = tracker.detect_drift("src", current_data=sample_response)
        assert report.has_changed is False
        assert report.drift_score == 0.0

    def test_drift_with_hash_only(self, tracker, sample_response):
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        report = tracker.detect_drift("src", current_hash="different_hash_value", current_size=50)
        assert report.has_changed is True
        assert report.size_delta == 50 - tracker.get_latest_snapshot("src").size_bytes

    def test_drift_with_bytes_data(self, tracker):
        tracker.track_file_fetch("file_src", b"original data")
        report = tracker.detect_drift("file_src", current_data=b"modified data")
        assert report.has_changed is True

    def test_drift_record_count_delta(self, tracker, sample_response):
        tracker.track_api_call("src", "/ep", "GET", sample_response, record_count=100)
        report = tracker.detect_drift("src", current_data={"diff": True}, current_record_count=150)
        assert report.record_count_delta == 50

    def test_drift_record_count_delta_none_when_baseline_missing(self, tracker, sample_response):
        tracker.track_api_call("src", "/ep", "GET", sample_response)  # no record_count
        report = tracker.detect_drift("src", current_data={"diff": True}, current_record_count=150)
        assert report.record_count_delta is None

    def test_drift_requires_data_or_hash(self, tracker, sample_response):
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        with pytest.raises(ValueError, match="Must provide"):
            tracker.detect_drift("src")

    def test_custom_detector_used_in_drift(self, tracker, sample_response, sample_response_v2):
        tracker.set_change_detector("src", lambda old, new: 0.42)
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        report = tracker.detect_drift("src", current_data=sample_response_v2)
        assert report.drift_score == 0.42

    def test_custom_detector_failure_defaults_to_1(self, tracker, sample_response, sample_response_v2):
        tracker.set_change_detector("src", lambda old, new: 1/0)
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        report = tracker.detect_drift("src", current_data=sample_response_v2)
        assert report.drift_score == 1.0


# =========================================================================
# 8. compare_snapshots
# =========================================================================

class TestCompareSnapshots:
    def test_compare_identical(self, tracker, sample_response):
        # Two calls with EVERY_CALL policy  two snapshots with same hash
        tracker.set_policy("src", SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL))
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        tracker.track_api_call("src", "/ep", "GET", sample_response)

        snaps = tracker.get_snapshots("src")
        report = tracker.compare_snapshots(snaps[0].snapshot_id, snaps[1].snapshot_id)
        assert report.has_changed is False
        assert report.drift_score == 0.0

    def test_compare_different(self, tracker, sample_response, sample_response_v2):
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        tracker.track_api_call("src", "/ep", "GET", sample_response_v2)

        snaps = tracker.get_snapshots("src")
        report = tracker.compare_snapshots(snaps[0].snapshot_id, snaps[1].snapshot_id)
        assert report.has_changed is True
        assert report.drift_score == 1.0
        assert report.size_delta != 0

    def test_compare_missing_snapshot_returns_none(self, tracker, sample_response):
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        snap = tracker.get_latest_snapshot("src")
        assert tracker.compare_snapshots(snap.snapshot_id, "nonexistent") is None
        assert tracker.compare_snapshots("nonexistent", snap.snapshot_id) is None

    def test_compare_record_count_delta(self, tracker):
        tracker.set_policy("src", SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL))
        tracker.track_api_call("src", "/ep", "GET", {"v": 1}, record_count=10)
        tracker.track_api_call("src", "/ep", "GET", {"v": 2}, record_count=25)
        snaps = tracker.get_snapshots("src")
        report = tracker.compare_snapshots(snaps[0].snapshot_id, snaps[1].snapshot_id)
        assert report.record_count_delta == 15


# =========================================================================
# 9. Snapshot queries
# =========================================================================

class TestSnapshotQueries:
    def test_get_latest_snapshot_empty(self, tracker):
        assert tracker.get_latest_snapshot("nothing") is None

    def test_get_latest_snapshot(self, tracker, sample_response, sample_response_v2):
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        tracker.track_api_call("src", "/ep", "GET", sample_response_v2)
        latest = tracker.get_latest_snapshot("src")
        assert latest.data_hash == _hash(sample_response_v2)

    def test_get_snapshots_returns_all(self, tracker):
        tracker.set_policy("src", SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL))
        for i in range(5):
            tracker.track_api_call("src", "/ep", "GET", {"i": i})
        assert len(tracker.get_snapshots("src")) == 5

    def test_get_snapshots_with_limit(self, tracker):
        tracker.set_policy("src", SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL))
        for i in range(5):
            tracker.track_api_call("src", "/ep", "GET", {"i": i})
        snaps = tracker.get_snapshots("src", limit=3)
        assert len(snaps) == 3
        # Should be the 3 most recent
        assert snaps[-1].data_hash == _hash({"i": 4})

    def test_get_all_sources(self, tracker, sample_response):
        tracker.track_api_call("api_a", "/ep", "GET", sample_response)
        tracker.track_api_call("api_b", "/ep", "GET", sample_response)
        tracker.track_file_fetch("file_c", b"data")
        sources = tracker.get_all_sources()
        assert set(sources) == {"api_a", "api_b", "file_c"}

    def test_get_snapshot_count_total(self, tracker, sample_response, sample_response_v2):
        tracker.track_api_call("a", "/ep", "GET", sample_response)
        tracker.track_api_call("b", "/ep", "GET", sample_response)
        tracker.track_api_call("a", "/ep", "GET", sample_response_v2)
        assert tracker.get_snapshot_count() == 3
        assert tracker.get_snapshot_count("a") == 2
        assert tracker.get_snapshot_count("b") == 1

    def test_get_snapshot_count_unknown_source(self, tracker):
        assert tracker.get_snapshot_count("nope") == 0


# =========================================================================
# 10. Retention enforcement
# =========================================================================

class TestRetention:
    def test_retention_by_days(self, tracker):
        tracker.set_policy("src", SnapshotPolicy(
            frequency=SnapshotFrequency.EVERY_CALL,
            retention_days=30,
        ))

        # Create snapshots manually with old timestamps
        # days_ago: 60, 50, 40, 25, 10
        days_ago = [60, 50, 40, 25, 10]
        for i, d in enumerate(days_ago):
            tracker._snapshots.setdefault("src", []).append(Snapshot(
                snapshot_id=f"s_{i}",
                source_name="src",
                source_type="api",
                data_hash=f"hash_{i}",
                timestamp=(datetime.now(timezone.utc) - timedelta(days=d)).isoformat(),
                size_bytes=100,
            ))

        # With 30-day retention: keep s_3 (25 days) and s_4 (10 days)
        removed = tracker.enforce_retention("src")
        assert removed == 3
        assert tracker.get_snapshot_count("src") == 2

    def test_retention_by_max_snapshots(self, tracker):
        tracker.set_policy("src", SnapshotPolicy(
            frequency=SnapshotFrequency.EVERY_CALL,
            max_snapshots=3,
            retention_days=0,  # No time-based retention
        ))

        for i in range(10):
            tracker._snapshots.setdefault("src", []).append(Snapshot(
                snapshot_id=f"s_{i}",
                source_name="src",
                source_type="api",
                data_hash=f"hash_{i}",
                timestamp=datetime.now(timezone.utc).isoformat(),
                size_bytes=100,
            ))

        removed = tracker.enforce_retention("src")
        assert removed == 7
        remaining = tracker.get_snapshots("src")
        assert len(remaining) == 3
        # Should keep most recent
        assert remaining[-1].snapshot_id == "s_9"

    def test_retention_all_sources(self, tracker):
        tracker.set_policy("a", SnapshotPolicy(
            frequency=SnapshotFrequency.EVERY_CALL, max_snapshots=1, retention_days=0
        ))
        tracker.set_policy("b", SnapshotPolicy(
            frequency=SnapshotFrequency.EVERY_CALL, max_snapshots=2, retention_days=0
        ))

        for i in range(5):
            for src in ["a", "b"]:
                tracker._snapshots.setdefault(src, []).append(Snapshot(
                    snapshot_id=f"{src}_{i}",
                    source_name=src,
                    source_type="api",
                    data_hash=f"hash_{i}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    size_bytes=100,
                ))

        removed = tracker.enforce_retention()
        assert tracker.get_snapshot_count("a") == 1
        assert tracker.get_snapshot_count("b") == 2
        assert removed == 7

    def test_retention_no_snapshots(self, tracker):
        removed = tracker.enforce_retention("empty")
        assert removed == 0

    def test_retention_zero_days_means_forever(self, tracker):
        tracker.set_policy("src", SnapshotPolicy(retention_days=0, max_snapshots=0))
        for i in range(5):
            tracker._snapshots.setdefault("src", []).append(Snapshot(
                snapshot_id=f"s_{i}",
                source_name="src",
                source_type="api",
                data_hash=f"hash_{i}",
                timestamp=(datetime.now(timezone.utc) - timedelta(days=1000)).isoformat(),
                size_bytes=100,
            ))
        removed = tracker.enforce_retention("src")
        assert removed == 0
        assert tracker.get_snapshot_count("src") == 5


# =========================================================================
# 11. Snapshot chain (parent linking)
# =========================================================================

class TestSnapshotChain:
    def test_first_snapshot_has_no_parent(self, tracker, sample_response):
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        snap = tracker.get_latest_snapshot("src")
        assert snap.parent_snapshot_id is None

    def test_subsequent_snapshots_link_to_parent(self, tracker):
        tracker.set_policy("src", SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL))
        ids = []
        for i in range(4):
            tracker.track_api_call("src", "/ep", "GET", {"i": i})
            ids.append(tracker.get_latest_snapshot("src").snapshot_id)

        snaps = tracker.get_snapshots("src")
        assert snaps[0].parent_snapshot_id is None
        assert snaps[1].parent_snapshot_id == ids[0]
        assert snaps[2].parent_snapshot_id == ids[1]
        assert snaps[3].parent_snapshot_id == ids[2]


# =========================================================================
# 12. Edge cases
# =========================================================================

class TestEdgeCases:
    def test_non_serializable_data_uses_default_str(self, tracker):
        """datetime objects in response data should be serialized via default=str."""
        data = {"ts": datetime(2025, 1, 1, 12, 0, 0), "val": 42}
        result = tracker.track_api_call("src", "/ep", "GET", data)
        assert result["data_hash"] is not None
        assert result["snapshot_stored"] is True

    def test_large_response_data(self, tracker):
        """Large data should still hash and store correctly."""
        data = {"items": list(range(100000))}
        result = tracker.track_api_call("src", "/ep", "GET", data)
        assert result["snapshot_stored"] is True
        assert result["size_bytes"] > 0

    def test_empty_response_data(self, tracker):
        result = tracker.track_api_call("src", "/ep", "GET", {})
        assert result["data_hash"] == _hash({})
        assert result["snapshot_stored"] is True

    def test_null_response_data(self, tracker):
        result = tracker.track_api_call("src", "/ep", "GET", None)
        assert result["data_hash"] is not None

    def test_multiple_sources_independent(self, tracker):
        """Snapshots for different sources are completely independent."""
        tracker.track_api_call("api_a", "/ep", "GET", {"v": 1})
        tracker.track_api_call("api_b", "/ep", "GET", {"v": 1})

        # Same data but different sources  separate snapshots
        assert tracker.get_snapshot_count("api_a") == 1
        assert tracker.get_snapshot_count("api_b") == 1

        # Second call with same data  ON_CHANGE skips for both
        tracker.track_api_call("api_a", "/ep", "GET", {"v": 1})
        tracker.track_api_call("api_b", "/ep", "GET", {"v": 1})
        assert tracker.get_snapshot_count("api_a") == 1
        assert tracker.get_snapshot_count("api_b") == 1

    def test_snapshot_id_format(self, tracker, sample_response):
        tracker.track_api_call("my_api", "/ep", "GET", sample_response)
        snap = tracker.get_latest_snapshot("my_api")
        # Format: source_name + hash_prefix + timestamp
        assert snap.snapshot_id.startswith("my_api_")
        parts = snap.snapshot_id.split("_", 2)  # my, api, hash_timestamp
        assert len(parts) >= 3

    def test_source_type_api(self, tracker, sample_response):
        tracker.track_api_call("src", "/ep", "GET", sample_response)
        assert tracker.get_latest_snapshot("src").source_type == "api"

    def test_source_type_db(self, tracker):
        data = [{"id": 1}]
        tracker.track_db_query("pg", "db", "SELECT 1", result_data=data, store_snapshot=True)
        assert tracker.get_latest_snapshot("pg.db").source_type == "db"

    def test_source_type_file(self, tracker):
        tracker.track_file_fetch("src", b"data")
        assert tracker.get_latest_snapshot("src").source_type == "file"


# =========================================================================
# 13. OTel span helpers (no-op without OTel)
# =========================================================================

class TestOTelHelpers:
    def test_start_span_returns_none_without_otel(self, tracker):
        # Temporarily patch HAS_OTEL to False to simulate missing OTel
        import briefcase.external_data.tracker as tracker_mod
        original = tracker_mod.HAS_OTEL
        tracker_mod.HAS_OTEL = False
        try:
            span = tracker._start_span("test_span", {"key": "val"})
            # Should gracefully return None (no crash)
            assert span is None
        finally:
            tracker_mod.HAS_OTEL = original

    def test_set_span_attributes_no_op_on_none(self, tracker):
        tracker._set_span_attributes(None, {"key": "val"})  # Should not raise

    def test_record_span_exception_no_op_on_none(self, tracker):
        tracker._record_span_exception(None, Exception("test"))  # Should not raise

    def test_end_span_no_op_on_none(self, tracker):
        tracker._end_span(None)  # Should not raise
