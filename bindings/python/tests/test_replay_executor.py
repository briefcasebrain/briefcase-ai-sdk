"""Replay compares a fresh answer against the recorded one.

Covers the Python surface: an engine with no executor reports that nothing was
verified, and an engine with one detects a changed answer.
"""

import pytest

from briefcase._native import (
    DecisionSnapshot,
    Input,
    Output,
    ReplayEngine,
    ReplayPolicy,
    SqliteBackend,
    init_with_config,
    is_initialized,
)


@pytest.fixture
def stored():
    if not is_initialized():
        init_with_config(2)
    backend = SqliteBackend(None)
    decision = DecisionSnapshot("classify_ticket")
    decision.add_input(Input("text", "reset my password", "string"))
    decision.add_output(Output("category", "account_access", "string"))
    return backend, backend.save_decision(decision)


def test_without_an_executor_the_replay_reports_nothing_verified(stored):
    backend, decision_id = stored
    result = ReplayEngine(backend).replay(decision_id, "strict")

    assert result.status == "pending"
    assert result.outputs_match is False
    assert result.replay_output is None


def test_a_changed_answer_is_a_mismatch(stored):
    backend, decision_id = stored
    engine = ReplayEngine(backend)
    engine.with_executor(lambda inputs: {"category": "billing"})

    result = engine.replay(decision_id, "strict")

    assert result.outputs_match is False
    assert result.status == "failed"


def test_an_unchanged_answer_still_matches(stored):
    backend, decision_id = stored
    engine = ReplayEngine(backend)
    engine.with_executor(lambda inputs: {"category": "account_access"})

    result = engine.replay(decision_id, "strict")

    assert result.outputs_match is True
    assert result.status == "success"


def test_the_executor_receives_the_recorded_inputs(stored):
    backend, decision_id = stored
    seen = {}

    def executor(inputs):
        seen.update(inputs)
        return {"category": "account_access"}

    ReplayEngine(backend).with_executor(executor)
    engine = ReplayEngine(backend)
    engine.with_executor(executor)
    engine.replay(decision_id, "strict")

    assert seen == {"text": "reset my password"}


def test_a_bare_return_value_becomes_the_result_output(stored):
    backend, decision_id = stored
    engine = ReplayEngine(backend)
    engine.with_executor(lambda inputs: "account_access")

    result = engine.replay(decision_id, "strict")

    assert result.replay_output[0]["name"] == "result"
    assert result.replay_output[0]["value"] == "account_access"


def test_a_raising_executor_surfaces_rather_than_reporting_success(stored):
    backend, decision_id = stored
    engine = ReplayEngine(backend)

    def boom(inputs):
        raise RuntimeError("model unavailable")

    engine.with_executor(boom)

    with pytest.raises(Exception) as excinfo:
        engine.replay(decision_id, "strict")
    assert "model unavailable" in str(excinfo.value)


def test_a_non_callable_executor_is_rejected(stored):
    backend, _ = stored
    with pytest.raises(TypeError):
        ReplayEngine(backend).with_executor("not callable")


def test_an_exact_match_policy_fails_on_a_changed_field(stored):
    backend, decision_id = stored
    engine = ReplayEngine(backend)
    engine.with_executor(lambda inputs: {"category": "billing"})
    policy = ReplayPolicy("output-consistency")
    policy.with_exact_match("category")

    result = engine.replay_with_policy(decision_id, policy, "strict")

    assert len(result.policy_violations) == 1
    violation = result.policy_violations[0]
    assert violation["field"] == "category"
    assert violation["expected"] == "account_access"
    assert violation["actual"] == "billing"


def test_an_exact_match_policy_passes_when_the_field_held(stored):
    backend, decision_id = stored
    engine = ReplayEngine(backend)
    engine.with_executor(lambda inputs: {"category": "account_access"})
    policy = ReplayPolicy("output-consistency")
    policy.with_exact_match("category")

    result = engine.replay_with_policy(decision_id, policy, "strict")

    assert result.policy_violations == []


def test_a_policy_cannot_pass_without_an_executor(stored):
    backend, decision_id = stored
    policy = ReplayPolicy("output-consistency")
    policy.with_exact_match("category")

    result = ReplayEngine(backend).replay_with_policy(decision_id, policy, "strict")

    assert len(result.policy_violations) == 1
    assert result.policy_violations[0]["actual"] == "not replayed"


def test_a_similarity_rule_tolerates_wording_but_not_meaning(stored):
    backend, decision_id = stored
    policy = ReplayPolicy("output-consistency")
    policy.with_similarity_threshold("category", 0.8)

    close = ReplayEngine(backend)
    close.with_executor(lambda inputs: {"category": "account_acces"})
    assert close.replay_with_policy(decision_id, policy, "strict").policy_violations == []

    far = ReplayEngine(backend)
    far.with_executor(lambda inputs: {"category": "billing"})
    assert far.replay_with_policy(decision_id, policy, "strict").policy_violations != []
