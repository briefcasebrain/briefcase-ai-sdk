"""Tests for briefcase.guardrails.envs RBAC and ABAC environments."""

from __future__ import annotations

import pytest

from briefcase.guardrails.envs import ABACGuardrailEnv, ABACRule, RBACGuardrailEnv
from briefcase.guardrails.framework import (
    Effect,
    EvalRequest,
    GuardrailEnv,
    _default_registry,
    make,
)


def _rbac_env() -> RBACGuardrailEnv:
    return RBACGuardrailEnv(
        agents=["agent-a", "agent-b"],
        allowed_actions=["read", "write"],
        allowed_resources=["/data/*", "/reports/q1.csv"],
    )


def _request(agent="agent-a", action="read", resource="/data/file.txt", context=None):
    return EvalRequest(
        agent=agent, action=action, resource=resource, context=context or {},
    )


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

def test_rbac_allow_when_all_match():
    result = _rbac_env().evaluate(_request())
    assert result.effect == Effect.ALLOW
    assert result.reason == "RBAC check passed"
    assert result.eval_time_ms >= 0


def test_rbac_deny_unknown_agent():
    result = _rbac_env().evaluate(_request(agent="intruder"))
    assert result.effect == Effect.DENY
    assert "agent 'intruder'" in result.reason


def test_rbac_deny_unknown_action():
    result = _rbac_env().evaluate(_request(action="delete"))
    assert result.effect == Effect.DENY
    assert "action 'delete'" in result.reason


def test_rbac_deny_unmatched_resource():
    result = _rbac_env().evaluate(_request(resource="/secrets/key.pem"))
    assert result.effect == Effect.DENY
    assert "does not match any allowed pattern" in result.reason


def test_rbac_glob_matches_literal_resource():
    result = _rbac_env().evaluate(_request(resource="/reports/q1.csv"))
    assert result.effect == Effect.ALLOW


def test_rbac_metadata_and_explain():
    env = _rbac_env()
    result = env.evaluate(_request(agent="intruder"))
    rbac = result.metadata["rbac_result"]
    assert rbac["agent"] == "intruder"
    assert rbac["agent_permitted"] is False

    explanation = env.explain(result)
    assert explanation.effect == Effect.DENY
    assert explanation.rbac_result == rbac


def test_rbac_request_space():
    space = _rbac_env().request_space
    assert space.agents == ["agent-a", "agent-b"]
    assert space.actions == ["read", "write"]
    assert space.resources == ["/data/*", "/reports/q1.csv"]


def test_rbac_default_config_denies_everything():
    result = RBACGuardrailEnv().evaluate(_request())
    assert result.effect == Effect.DENY


def test_rbac_satisfies_guardrail_env_protocol():
    assert isinstance(_rbac_env(), GuardrailEnv)


# ---------------------------------------------------------------------------
# ABAC
# ---------------------------------------------------------------------------

def test_abac_allow_when_all_rules_pass():
    env = ABACGuardrailEnv(rules=[
        ABACRule(attribute="risk_score", operator="<=", value=0.5),
        ABACRule(attribute="region", operator="in", value=["us", "eu"]),
    ])
    result = env.evaluate(_request(context={"risk_score": 0.2, "region": "eu"}))
    assert result.effect == Effect.ALLOW
    assert result.reason == "All ABAC rules passed"


def test_abac_deny_on_violated_rule():
    env = ABACGuardrailEnv(rules=[
        ABACRule(attribute="risk_score", operator="<=", value=0.5),
    ])
    result = env.evaluate(_request(context={"risk_score": 0.9}))
    assert result.effect == Effect.DENY
    assert "Rule violated" in result.reason
    assert result.metadata["violated_rule"]["actual"] == 0.9


def test_abac_missing_attribute_returns_rule_effect():
    env = ABACGuardrailEnv(rules=[
        ABACRule(attribute="clearance", operator="==", value="high"),
    ])
    result = env.evaluate(_request(context={}))
    assert result.effect == Effect.DENY
    assert "Missing attribute 'clearance'" in result.reason
    assert result.metadata["violated_rule"]["actual"] is None


def test_abac_rule_effect_allow_on_violation():
    env = ABACGuardrailEnv(rules=[
        ABACRule(attribute="score", operator=">=", value=10, effect=Effect.ALLOW),
    ])
    result = env.evaluate(_request(context={"score": 1}))
    assert result.effect == Effect.ALLOW
    assert "Rule violated" in result.reason


@pytest.mark.parametrize(
    "operator,value,actual,satisfied",
    [
        (">=", 5, 5, True),
        (">=", 5, 4, False),
        ("<=", 5, 5, True),
        ("<=", 5, 6, False),
        (">", 5, 6, True),
        (">", 5, 5, False),
        ("<", 5, 4, True),
        ("<", 5, 5, False),
        ("==", "x", "x", True),
        ("==", "x", "y", False),
        ("!=", "x", "y", True),
        ("!=", "x", "x", False),
        ("in", ["a", "b"], "a", True),
        ("in", ["a", "b"], "c", False),
        ("not_in", ["a", "b"], "c", True),
        ("not_in", ["a", "b"], "a", False),
    ],
)
def test_abac_operators(operator, value, actual, satisfied):
    env = ABACGuardrailEnv(rules=[
        ABACRule(attribute="attr", operator=operator, value=value),
    ])
    result = env.evaluate(_request(context={"attr": actual}))
    expected = Effect.ALLOW if satisfied else Effect.DENY
    assert result.effect == expected


def test_abac_unknown_operator_fails_closed():
    env = ABACGuardrailEnv(rules=[
        ABACRule(attribute="attr", operator="matches", value="x"),
    ])
    result = env.evaluate(_request(context={"attr": "x"}))
    assert result.effect == Effect.DENY


def test_abac_explain_includes_extraction():
    env = ABACGuardrailEnv(rules=[
        ABACRule(attribute="risk_score", operator="<=", value=0.5),
    ])
    result = env.evaluate(_request(context={"risk_score": 0.9}))
    explanation = env.explain(result)
    assert explanation.extraction["attribute"] == "risk_score"
    assert explanation.extraction["value"] == 0.9
    assert explanation.abac_result == result.metadata["violated_rule"]


def test_abac_explain_allow_has_no_extraction():
    env = ABACGuardrailEnv(rules=[])
    result = env.evaluate(_request())
    explanation = env.explain(result)
    assert explanation.effect == Effect.ALLOW
    assert explanation.extraction is None


def test_abac_request_space_bounds():
    env = ABACGuardrailEnv(rules=[
        ABACRule(attribute="age", operator=">=", value=18),
        ABACRule(attribute="risk", operator="<=", value=0.5),
        ABACRule(attribute="region", operator="in", value=["us"]),
    ])
    schema = env.request_space.context_schema
    assert schema["age"].low == 18.0
    assert schema["age"].high == float("inf")
    assert schema["risk"].low == float("-inf")
    assert schema["risk"].high == 0.5
    assert schema["region"].low == float("-inf")
    assert schema["region"].high == float("inf")


def test_abac_satisfies_guardrail_env_protocol():
    assert isinstance(ABACGuardrailEnv(), GuardrailEnv)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------

def test_envs_registered_on_import():
    """Importing briefcase.guardrails.envs registers both env ids."""
    assert "rbac-env-v1" in _default_registry._specs
    assert "abac-env-v1" in _default_registry._specs


def test_make_rbac_env_from_registry():
    env = make(
        "rbac-env-v1",
        agents=["agent-a"],
        allowed_actions=["read"],
        allowed_resources=["/data/*"],
    )
    assert isinstance(env, RBACGuardrailEnv)
    result = env.evaluate(_request())
    assert result.effect == Effect.ALLOW


def test_make_abac_env_by_unversioned_id():
    """make() resolves 'abac-env' to the highest registered version."""
    env = make("abac-env", rules=[
        ABACRule(attribute="risk", operator="<=", value=0.5),
    ])
    assert isinstance(env, ABACGuardrailEnv)
    result = env.evaluate(_request(context={"risk": 0.9}))
    assert result.effect == Effect.DENY
