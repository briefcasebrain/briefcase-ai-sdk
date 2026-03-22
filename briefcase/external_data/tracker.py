"""
Tracker for external data sources with automatic versioning, snapshot policies,
drift detection, and lakeFS storage integration.
"""

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Callable
import logging

try:
    from opentelemetry import trace
    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False

from briefcase.semantic_conventions.external_data import *

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class SnapshotFrequency(Enum):
    """How often snapshots should be taken."""
    EVERY_CALL = "every_call"
    ON_CHANGE = "on_change"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass
class SnapshotPolicy:
    """
    Policy controlling when and how snapshots are stored.

    Attributes:
        frequency: How often to take snapshots.
        retention_days: How many days to retain snapshots (0 = forever).
        change_threshold: Minimum Jaccard distance (0.0-1.0) to consider data
            "changed" when frequency is ON_CHANGE. 0.0 means any byte-level
            change triggers a snapshot.
        max_snapshots: Maximum snapshots to retain per source (0 = unlimited).
        compress: Whether to compress snapshot data before storage.
    """
    frequency: SnapshotFrequency = SnapshotFrequency.ON_CHANGE
    retention_days: int = 90
    change_threshold: float = 0.0
    max_snapshots: int = 0
    compress: bool = False


@dataclass
class Snapshot:
    """
    Immutable record of an external data fetch.
    """
    snapshot_id: str
    source_name: str
    source_type: str  # "api" | "db" | "file"
    data_hash: str
    timestamp: str  # ISO-8601
    size_bytes: int
    record_count: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    lakefs_path: Optional[str] = None
    parent_snapshot_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Snapshot":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DriftReport:
    """
    Report comparing two snapshots of the same source.
    """
    source_name: str
    baseline_snapshot_id: str
    current_snapshot_id: str
    baseline_hash: str
    current_hash: str
    has_changed: bool
    size_delta: int  # bytes
    record_count_delta: Optional[int] = None
    drift_score: float = 0.0  # 0.0 = identical, 1.0 = completely different
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Main tracker
# ---------------------------------------------------------------------------

class ExternalDataTracker:
    """
    Tracks external data access with snapshot policies, drift detection,
    and optional lakeFS storage.

    Usage:
        tracker = ExternalDataTracker()
        policy = SnapshotPolicy(frequency=SnapshotFrequency.ON_CHANGE)
        tracker.set_policy("ofac_sdn", policy)

        result = tracker.track_api_call(
            api_name="ofac_sdn",
            endpoint="https://api.treasury.gov/sdn",
            method="GET",
            response_data=sdn_response,
        )
    """

    def __init__(
        self,
        lakefs_client: Optional[Any] = None,
        repository: Optional[str] = None,
        branch: str = "main",
        default_policy: Optional[SnapshotPolicy] = None,
    ):
        self.lakefs = lakefs_client
        self.repository = repository
        self.branch = branch
        self._default_policy = default_policy or SnapshotPolicy()

        # source_name -> SnapshotPolicy
        self._policies: Dict[str, SnapshotPolicy] = {}

        # source_name -> list of Snapshot (most recent last)
        self._snapshots: Dict[str, List[Snapshot]] = {}

        # source_name -> last snapshot timestamp (for frequency gating)
        self._last_snapshot_times: Dict[str, datetime] = {}

        # Optional custom change detector: (old_data, new_data) -> float
        self._change_detectors: Dict[str, Callable] = {}

    # ------------------------------------------------------------------
    # Policy management
    # ------------------------------------------------------------------

    def set_policy(self, source_name: str, policy: SnapshotPolicy) -> None:
        """Set snapshot policy for a specific source."""
        self._policies[source_name] = policy

    def get_policy(self, source_name: str) -> SnapshotPolicy:
        """Get policy for source, falling back to default."""
        return self._policies.get(source_name, self._default_policy)

    def set_change_detector(
        self, source_name: str, detector: Callable[[Any, Any], float]
    ) -> None:
        """
        Register a custom change detector for a source.

        The detector receives (old_data, new_data) and returns a float
        in [0.0, 1.0] representing how different the data is.
        """
        self._change_detectors[source_name] = detector

    # ------------------------------------------------------------------
    # Core tracking
    # ------------------------------------------------------------------

    def track_api_call(
        self,
        api_name: str,
        endpoint: str,
        method: str,
        response_data: Any,
        version: Optional[str] = None,
        status_code: int = 200,
        record_count: Optional[int] = None,
        store_snapshot: bool = True,
    ) -> Dict[str, Any]:
        """
        Track an API call and optionally store a versioned snapshot.

        Returns dict with: data_hash, timestamp, snapshot_id (if stored),
        snapshot_stored (bool), drift_detected (bool).
        """
        span = self._start_span("external_data.track_api_call", {
            EXTERNAL_API_NAME: api_name,
            EXTERNAL_API_ENDPOINT: endpoint,
            EXTERNAL_API_METHOD: method,
            EXTERNAL_API_STATUS_CODE: status_code,
        })

        try:
            response_str = json.dumps(response_data, sort_keys=True, default=str)
            data_hash = hashlib.sha256(response_str.encode()).hexdigest()
            size_bytes = len(response_str.encode())
            timestamp = datetime.now(timezone.utc)

            metadata = {
                "api_name": api_name,
                "endpoint": endpoint,
                "method": method,
                "status_code": status_code,
            }
            if version:
                metadata["api_version"] = version

            result = {
                "data_hash": data_hash,
                "timestamp": timestamp.isoformat(),
                "size_bytes": size_bytes,
                "snapshot_id": None,
                "snapshot_stored": False,
                "drift_detected": False,
            }

            if store_snapshot:
                should_store, drift = self._evaluate_snapshot_policy(
                    api_name, data_hash, size_bytes, response_data, timestamp
                )

                if should_store:
                    snapshot = self._create_snapshot(
                        source_name=api_name,
                        source_type="api",
                        data_hash=data_hash,
                        size_bytes=size_bytes,
                        record_count=record_count,
                        metadata=metadata,
                        timestamp=timestamp,
                        data=response_str if self.lakefs else None,
                    )
                    result["snapshot_id"] = snapshot.snapshot_id
                    result["snapshot_stored"] = True

                result["drift_detected"] = drift

            self._set_span_attributes(span, {
                EXTERNAL_DATA_HASH: data_hash,
                EXTERNAL_DATA_SIZE: size_bytes,
                EXTERNAL_DATA_SOURCE: api_name,
            })
            if result.get("snapshot_id"):
                self._set_span_attributes(span, {
                    EXTERNAL_SNAPSHOT_ID: result["snapshot_id"],
                })

            return result

        except Exception as e:
            self._record_span_exception(span, e)
            raise
        finally:
            self._end_span(span)

    def track_db_query(
        self,
        db_system: str,
        db_name: str,
        query: str,
        result_data: Any = None,
        result_count: int = 0,
        store_snapshot: bool = False,
    ) -> Dict[str, Any]:
        """
        Track a database query and optionally store result snapshot.

        Returns dict with: query_hash, data_hash (if result_data provided),
        result_count, timestamp, snapshot_id, snapshot_stored, drift_detected.
        """
        source_name = f"{db_system}.{db_name}"

        span = self._start_span("external_data.track_db_query", {
            EXTERNAL_DB_SYSTEM: db_system,
            EXTERNAL_DB_NAME: db_name,
            EXTERNAL_DB_QUERY_HASH: hashlib.sha256(query.encode()).hexdigest()[:16],
            EXTERNAL_DB_RESULT_COUNT: result_count,
        })

        try:
            query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
            timestamp = datetime.now(timezone.utc)

            result = {
                "query_hash": query_hash,
                "result_count": result_count,
                "timestamp": timestamp.isoformat(),
                "data_hash": None,
                "snapshot_id": None,
                "snapshot_stored": False,
                "drift_detected": False,
            }

            if result_data is not None:
                data_str = json.dumps(result_data, sort_keys=True, default=str)
                data_hash = hashlib.sha256(data_str.encode()).hexdigest()
                size_bytes = len(data_str.encode())
                result["data_hash"] = data_hash

                if store_snapshot:
                    should_store, drift = self._evaluate_snapshot_policy(
                        source_name, data_hash, size_bytes, result_data, timestamp
                    )

                    if should_store:
                        snapshot = self._create_snapshot(
                            source_name=source_name,
                            source_type="db",
                            data_hash=data_hash,
                            size_bytes=size_bytes,
                            record_count=result_count,
                            metadata={
                                "db_system": db_system,
                                "db_name": db_name,
                                "query_hash": query_hash,
                            },
                            timestamp=timestamp,
                            data=data_str if self.lakefs else None,
                        )
                        result["snapshot_id"] = snapshot.snapshot_id
                        result["snapshot_stored"] = True

                    result["drift_detected"] = drift

            self._set_span_attributes(span, {
                EXTERNAL_DATA_SOURCE: source_name,
            })
            if result.get("data_hash"):
                self._set_span_attributes(span, {
                    EXTERNAL_DATA_HASH: result["data_hash"],
                })

            return result

        except Exception as e:
            self._record_span_exception(span, e)
            raise
        finally:
            self._end_span(span)

    def track_file_fetch(
        self,
        source_name: str,
        file_data: bytes,
        file_path: Optional[str] = None,
        record_count: Optional[int] = None,
        store_snapshot: bool = True,
    ) -> Dict[str, Any]:
        """
        Track a file fetch (S3, SFTP, local) and optionally store snapshot.
        """
        span = self._start_span("external_data.track_file_fetch", {
            EXTERNAL_DATA_SOURCE: source_name,
        })

        try:
            data_hash = hashlib.sha256(file_data).hexdigest()
            size_bytes = len(file_data)
            timestamp = datetime.now(timezone.utc)

            metadata = {"source_name": source_name}
            if file_path:
                metadata["file_path"] = file_path

            result = {
                "data_hash": data_hash,
                "size_bytes": size_bytes,
                "timestamp": timestamp.isoformat(),
                "snapshot_id": None,
                "snapshot_stored": False,
                "drift_detected": False,
            }

            if store_snapshot:
                # For files we use the raw bytes hash, not JSON serialized
                should_store, drift = self._evaluate_snapshot_policy(
                    source_name, data_hash, size_bytes, file_data, timestamp
                )

                if should_store:
                    snapshot = self._create_snapshot(
                        source_name=source_name,
                        source_type="file",
                        data_hash=data_hash,
                        size_bytes=size_bytes,
                        record_count=record_count,
                        metadata=metadata,
                        timestamp=timestamp,
                        data=None,  # Files are stored as-is via lakefs
                    )
                    result["snapshot_id"] = snapshot.snapshot_id
                    result["snapshot_stored"] = True

                result["drift_detected"] = drift

            self._set_span_attributes(span, {
                EXTERNAL_DATA_HASH: data_hash,
                EXTERNAL_DATA_SIZE: size_bytes,
            })

            return result

        except Exception as e:
            self._record_span_exception(span, e)
            raise
        finally:
            self._end_span(span)

    # ------------------------------------------------------------------
    # Drift detection
    # ------------------------------------------------------------------

    def detect_drift(
        self,
        source_name: str,
        current_data: Any = None,
        current_hash: Optional[str] = None,
        current_size: Optional[int] = None,
        current_record_count: Optional[int] = None,
    ) -> Optional[DriftReport]:
        """
        Compare current data against the latest snapshot for a source.

        Provide either current_data (will be hashed) or current_hash directly.
        Returns None if no previous snapshot exists.
        """
        snapshots = self._snapshots.get(source_name)
        if not snapshots:
            return None

        baseline = snapshots[-1]  # most recent

        if current_hash is None and current_data is not None:
            if isinstance(current_data, bytes):
                current_hash = hashlib.sha256(current_data).hexdigest()
                current_size = current_size or len(current_data)
            else:
                data_str = json.dumps(current_data, sort_keys=True, default=str)
                current_hash = hashlib.sha256(data_str.encode()).hexdigest()
                current_size = current_size or len(data_str.encode())

        if current_hash is None:
            raise ValueError("Must provide either current_data or current_hash")

        has_changed = current_hash != baseline.data_hash
        size_delta = (current_size or 0) - baseline.size_bytes
        record_delta = None
        if current_record_count is not None and baseline.record_count is not None:
            record_delta = current_record_count - baseline.record_count

        # Compute drift score
        drift_score = 0.0
        if has_changed:
            # Check custom detector first
            if source_name in self._change_detectors and current_data is not None:
                try:
                    drift_score = self._change_detectors[source_name](
                        None, current_data  # We don't store old data in memory
                    )
                except Exception as e:
                    logger.warning(f"Custom change detector failed for {source_name}: {e}")
                    drift_score = 1.0
            else:
                drift_score = 1.0  # Binary: changed or not without custom detector

        return DriftReport(
            source_name=source_name,
            baseline_snapshot_id=baseline.snapshot_id,
            current_snapshot_id="pending",
            baseline_hash=baseline.data_hash,
            current_hash=current_hash,
            has_changed=has_changed,
            size_delta=size_delta,
            record_count_delta=record_delta,
            drift_score=drift_score,
        )

    def compare_snapshots(
        self, snapshot_a_id: str, snapshot_b_id: str
    ) -> Optional[DriftReport]:
        """
        Compare two specific snapshots by ID.

        Returns None if either snapshot is not found.
        """
        snap_a = self._find_snapshot(snapshot_a_id)
        snap_b = self._find_snapshot(snapshot_b_id)

        if snap_a is None or snap_b is None:
            return None

        has_changed = snap_a.data_hash != snap_b.data_hash
        size_delta = snap_b.size_bytes - snap_a.size_bytes
        record_delta = None
        if snap_a.record_count is not None and snap_b.record_count is not None:
            record_delta = snap_b.record_count - snap_a.record_count

        return DriftReport(
            source_name=snap_a.source_name,
            baseline_snapshot_id=snap_a.snapshot_id,
            current_snapshot_id=snap_b.snapshot_id,
            baseline_hash=snap_a.data_hash,
            current_hash=snap_b.data_hash,
            has_changed=has_changed,
            size_delta=size_delta,
            record_count_delta=record_delta,
            drift_score=1.0 if has_changed else 0.0,
        )

    # ------------------------------------------------------------------
    # Snapshot queries
    # ------------------------------------------------------------------

    def get_snapshots(
        self,
        source_name: str,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[Snapshot]:
        """Get snapshots for a source, optionally filtered by time range."""
        snaps = self._snapshots.get(source_name, [])

        if since:
            since_str = since.isoformat()
            snaps = [s for s in snaps if s.timestamp >= since_str]
        if until:
            until_str = until.isoformat()
            snaps = [s for s in snaps if s.timestamp <= until_str]
        if limit:
            snaps = snaps[-limit:]

        return snaps

    def get_latest_snapshot(self, source_name: str) -> Optional[Snapshot]:
        """Get the most recent snapshot for a source."""
        snaps = self._snapshots.get(source_name, [])
        return snaps[-1] if snaps else None

    def get_all_sources(self) -> List[str]:
        """List all tracked source names."""
        return list(self._snapshots.keys())

    def get_snapshot_count(self, source_name: Optional[str] = None) -> int:
        """Count snapshots, optionally for a specific source."""
        if source_name:
            return len(self._snapshots.get(source_name, []))
        return sum(len(v) for v in self._snapshots.values())

    # ------------------------------------------------------------------
    # Retention / cleanup
    # ------------------------------------------------------------------

    def enforce_retention(self, source_name: Optional[str] = None) -> int:
        """
        Remove snapshots that exceed their policy's retention period
        or max_snapshots limit.

        Returns the number of snapshots removed.
        """
        removed = 0
        sources = [source_name] if source_name else list(self._snapshots.keys())

        for src in sources:
            policy = self.get_policy(src)
            snaps = self._snapshots.get(src, [])
            if not snaps:
                continue

            original_count = len(snaps)

            # Remove by retention_days
            if policy.retention_days > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=policy.retention_days)).isoformat()
                snaps = [s for s in snaps if s.timestamp >= cutoff]

            # Remove by max_snapshots (keep most recent)
            if policy.max_snapshots > 0 and len(snaps) > policy.max_snapshots:
                snaps = snaps[-policy.max_snapshots:]

            self._snapshots[src] = snaps
            removed += original_count - len(snaps)

        return removed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evaluate_snapshot_policy(
        self,
        source_name: str,
        data_hash: str,
        size_bytes: int,
        data: Any,
        timestamp: datetime,
    ) -> tuple:
        """
        Decide whether to store a snapshot based on the source's policy.

        Returns (should_store: bool, drift_detected: bool).
        """
        policy = self.get_policy(source_name)
        last_snap = self.get_latest_snapshot(source_name)

        # No previous snapshot -> always store
        if last_snap is None:
            return True, False

        # Check drift
        drift_detected = data_hash != last_snap.data_hash

        if policy.frequency == SnapshotFrequency.EVERY_CALL:
            return True, drift_detected

        if policy.frequency == SnapshotFrequency.ON_CHANGE:
            if not drift_detected:
                return False, False

            # Check change threshold with custom detector
            if policy.change_threshold > 0.0 and source_name in self._change_detectors:
                try:
                    score = self._change_detectors[source_name](None, data)
                    if score < policy.change_threshold:
                        return False, True  # Drift detected but below threshold
                except Exception:
                    pass  # Fall through to store

            return True, drift_detected

        # Time-based frequencies
        last_time = self._last_snapshot_times.get(source_name)
        if last_time is None:
            return True, drift_detected

        interval_map = {
            SnapshotFrequency.HOURLY: timedelta(hours=1),
            SnapshotFrequency.DAILY: timedelta(days=1),
            SnapshotFrequency.WEEKLY: timedelta(weeks=1),
        }
        interval = interval_map.get(policy.frequency, timedelta(hours=1))

        if timestamp - last_time >= interval:
            return True, drift_detected

        return False, drift_detected

    def _create_snapshot(
        self,
        source_name: str,
        source_type: str,
        data_hash: str,
        size_bytes: int,
        record_count: Optional[int],
        metadata: Dict[str, Any],
        timestamp: datetime,
        data: Optional[str] = None,
    ) -> Snapshot:
        """Create and store a snapshot."""
        parent = self.get_latest_snapshot(source_name)
        snapshot_id = f"{source_name}_{data_hash[:12]}_{timestamp.strftime('%Y%m%d%H%M%S')}"

        lakefs_path = None
        if self.lakefs and self.repository:
            lakefs_path = f"snapshots/{source_name}/{snapshot_id}.json"
            try:
                self.lakefs.upload_object(
                    self.repository,
                    self.branch,
                    lakefs_path,
                    data or json.dumps(metadata),
                )
            except Exception as e:
                logger.warning(f"Failed to upload snapshot to lakeFS: {e}")
                lakefs_path = None

        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            source_name=source_name,
            source_type=source_type,
            data_hash=data_hash,
            timestamp=timestamp.isoformat(),
            size_bytes=size_bytes,
            record_count=record_count,
            metadata=metadata,
            lakefs_path=lakefs_path,
            parent_snapshot_id=parent.snapshot_id if parent else None,
        )

        self._snapshots.setdefault(source_name, []).append(snapshot)
        self._last_snapshot_times[source_name] = timestamp

        logger.info(
            f"Stored snapshot {snapshot_id} for {source_name} "
            f"(hash={data_hash[:12]}, size={size_bytes})"
        )

        return snapshot

    def _find_snapshot(self, snapshot_id: str) -> Optional[Snapshot]:
        """Find a snapshot by ID across all sources."""
        for snaps in self._snapshots.values():
            for snap in snaps:
                if snap.snapshot_id == snapshot_id:
                    return snap
        return None

    # ------------------------------------------------------------------
    # OpenTelemetry helpers
    # ------------------------------------------------------------------

    def _start_span(self, name: str, attributes: Dict[str, Any] = None):
        """Start an OTel span if available."""
        if not HAS_OTEL:
            return None
        try:
            tracer = trace.get_tracer(__name__)
            span = tracer.start_span(name, attributes=attributes or {})
            return span
        except Exception as e:
            logger.debug(f"Failed to start OTel span: {e}")
            return None

    def _set_span_attributes(self, span, attributes: Dict[str, Any]) -> None:
        """Set attributes on a span if it exists."""
        if span is None:
            return
        try:
            for k, v in attributes.items():
                span.set_attribute(k, v)
        except Exception:
            pass

    def _record_span_exception(self, span, exception: Exception) -> None:
        """Record an exception on a span."""
        if span is None:
            return
        try:
            span.set_status(trace.StatusCode.ERROR, str(exception))
            span.record_exception(exception)
        except Exception:
            pass

    def _end_span(self, span) -> None:
        """End a span if it exists."""
        if span is None:
            return
        try:
            span.end()
        except Exception:
            pass
