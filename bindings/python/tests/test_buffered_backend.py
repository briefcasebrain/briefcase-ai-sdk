"""BufferedBackend groups decision writes and can be read before it flushes."""

import pytest

from briefcase._native import (
    DecisionSnapshot,
    BufferedBackend,
    Input,
    SnapshotQuery,
    SqliteBackend,
    init_with_config,
    is_initialized,
)


@pytest.fixture
def backends(tmp_path):
    if not is_initialized():
        init_with_config(2)
    inner = SqliteBackend(str(tmp_path / "decisions.db"))
    return inner, BufferedBackend(inner, 4)


def decision(n=0):
    d = DecisionSnapshot("classify_ticket")
    d.add_input(Input("text", f"ticket-{n}", "string"))
    return d


def test_decisions_are_held_until_the_batch_fills(backends):
    inner, buffered = backends

    for n in range(3):
        buffered.save_decision(decision(n))

    assert buffered.pending() == 3
    assert inner.query(SnapshotQuery().with_function_name("classify_ticket")) == []

    buffered.save_decision(decision(3))

    assert buffered.pending() == 0
    assert len(inner.query(SnapshotQuery().with_function_name("classify_ticket"))) == 4


def test_a_returned_id_resolves_before_the_write_lands(backends):
    _, buffered = backends
    decision_id = buffered.save_decision(decision())

    assert buffered.pending() == 1
    assert buffered.load_decision(decision_id).function_name == "classify_ticket"


def test_flush_writes_a_partial_batch_and_reports_its_own_count(backends):
    inner, buffered = backends
    buffered.save_decision(decision(0))
    buffered.save_decision(decision(1))

    written = buffered.flush()

    assert written == 2, "flush reports what it wrote, not the table's row count"
    assert buffered.pending() == 0
    assert len(inner.query(SnapshotQuery().with_function_name("classify_ticket"))) == 2


def test_the_context_manager_flushes_on_exit(tmp_path):
    if not is_initialized():
        init_with_config(2)
    inner = SqliteBackend(str(tmp_path / "ctx.db"))

    with BufferedBackend(inner, 100) as buffered:
        buffered.save_decision(decision())
        assert buffered.pending() == 1

    assert len(inner.query(SnapshotQuery().with_function_name("classify_ticket"))) == 1


def test_a_batch_size_of_one_writes_through(tmp_path):
    if not is_initialized():
        init_with_config(2)
    inner = SqliteBackend(str(tmp_path / "through.db"))
    buffered = BufferedBackend(inner, 1)

    buffered.save_decision(decision())

    assert buffered.pending() == 0
    assert len(inner.query(SnapshotQuery().with_function_name("classify_ticket"))) == 1
