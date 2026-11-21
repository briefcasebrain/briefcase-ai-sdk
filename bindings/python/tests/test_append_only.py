"""SqliteBackend(append_only=True) refuses to rewrite a stored decision."""

import pytest

from briefcase._native import (
    BufferedBackend,
    DecisionSnapshot,
    Output,
    SqliteBackend,
    init_with_config,
    is_initialized,
)


@pytest.fixture(autouse=True)
def runtime():
    if not is_initialized():
        init_with_config(2)


def decision(answer="approved"):
    d = DecisionSnapshot("review_claim")
    d.add_output(Output("verdict", answer, "string"))
    return d


def test_the_default_backend_still_replaces_by_id():
    backend = SqliteBackend(None)
    assert backend.append_only is False

    d = decision("approved")
    decision_id = backend.save_decision(d)
    d.add_output(Output("note", "edited later", "string"))
    assert backend.save_decision(d) == decision_id

    assert len(backend.load_decision(decision_id).outputs) == 2
    assert backend.delete(decision_id) is True


def test_append_only_raises_on_an_existing_id_and_keeps_the_row(tmp_path):
    backend = SqliteBackend(str(tmp_path / "audit.db"), append_only=True)
    assert backend.append_only is True
    assert "append_only=True" in repr(backend)

    d = decision("approved")
    decision_id = backend.save_decision(d)
    d.add_output(Output("note", "edited later", "string"))

    with pytest.raises(FileExistsError) as excinfo:
        backend.save_decision(d)
    assert decision_id in str(excinfo.value)
    assert len(backend.load_decision(decision_id).outputs) == 1

    # A different decision still saves.
    assert backend.save_decision(decision("denied")) != decision_id


def test_append_only_refuses_delete():
    backend = SqliteBackend.in_memory(append_only=True)
    decision_id = backend.save_decision(decision())

    with pytest.raises(PermissionError):
        backend.delete(decision_id)
    assert backend.load_decision(decision_id) is not None


def test_append_only_applies_through_a_buffered_backend():
    inner = SqliteBackend(None, append_only=True)
    d = decision()
    inner.save_decision(d)

    buffered = BufferedBackend(inner, 8)
    buffered.save_decision(d)
    with pytest.raises(FileExistsError):
        buffered.flush()
