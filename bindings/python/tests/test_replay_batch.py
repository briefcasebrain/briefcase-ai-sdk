"""Tests for ReplayEngine.replay_batch when some items fail.

A batch that raises must not throw away the items the runtime already replayed:
a nightly audit job over thousands of snapshots should not lose every result
because one snapshot was pruned.
"""

from __future__ import annotations

import pytest
from briefcase._native import (
    DecisionSnapshot,
    ReplayEngine,
    Snapshot,
    SqliteBackend,
    init_with_config,
    is_initialized,
)


@pytest.fixture
def engine_with_one_snapshot(tmp_path):
    if not is_initialized():
        init_with_config(2)
    backend = SqliteBackend(str(tmp_path / "replay.db"))
    snapshot = Snapshot("session")
    snapshot.add_decision(DecisionSnapshot("score_loan"))
    snapshot_id = backend.save(snapshot)
    return ReplayEngine(backend), snapshot_id


def test_batch_of_valid_ids_returns_results(engine_with_one_snapshot):
    engine, snapshot_id = engine_with_one_snapshot
    results = engine.replay_batch([snapshot_id])
    assert len(results) == 1


def test_partial_failure_raises(engine_with_one_snapshot):
    engine, snapshot_id = engine_with_one_snapshot
    with pytest.raises(Exception) as excinfo:
        engine.replay_batch([snapshot_id, "does-not-exist"])
    assert "1 of 2" in str(excinfo.value)


def test_partial_failure_carries_the_successful_results(engine_with_one_snapshot):
    engine, snapshot_id = engine_with_one_snapshot
    with pytest.raises(Exception) as excinfo:
        engine.replay_batch([snapshot_id, "does-not-exist"])

    error = excinfo.value
    assert error.succeeded == 1
    assert error.total == 2
    assert error.failed_indices == [1]
    assert len(error.results) == 1
    # The recovered item is a real ReplayResult, not a placeholder.
    assert error.results[0].status
    assert error.results[0].to_dict()


def test_total_failure_carries_an_empty_result_list(engine_with_one_snapshot):
    engine, _ = engine_with_one_snapshot
    with pytest.raises(Exception) as excinfo:
        engine.replay_batch(["nope-1", "nope-2"])

    error = excinfo.value
    assert error.succeeded == 0
    assert error.failed_indices == [0, 1]
    assert list(error.results) == []
