"""Tests for versioned routing policies."""

from datetime import datetime, timezone, timedelta

import pytest

from briefcase.routing import (
    AgentRouter,
    AgentRoutingDecision,
    PolicyRegistry,
    PolicyRule,
    PolicyVersion,
)


UTC = timezone.utc


def _ts(days: int = 0) -> datetime:
    return datetime(2026, 4, 17, tzinfo=UTC) + timedelta(days=days)


def _v1() -> PolicyVersion:
    return PolicyVersion(
        policy_id="stablecoin_router",
        version="1.0.0",
        rules=[
            PolicyRule(
                rule_id="r1",
                condition={"jurisdiction": "US"},
                choice="USDC",
                rationale="US regulatory alignment",
            ),
            PolicyRule(
                rule_id="r2",
                condition={"jurisdiction": {"in": ["LATAM", "SEA"]}},
                choice="USDT",
                rationale="EM liquidity",
            ),
        ],
        default_choice="USDC",
    )


def _v2() -> PolicyVersion:
    # Regulatory shift — LATAM now routes USDC.
    return PolicyVersion(
        policy_id="stablecoin_router",
        version="2.0.0",
        rules=[
            PolicyRule(
                rule_id="r1",
                condition={"jurisdiction": {"in": ["US", "LATAM"]}},
                choice="USDC",
                rationale="Compliant issuer across both corridors",
            ),
            PolicyRule(
                rule_id="r2",
                condition={"jurisdiction": "SEA"},
                choice="USDT",
                rationale="SEA still needs USDT liquidity",
            ),
        ],
        default_choice="USDC",
    )


# --------------------------------------------------------------------------
# Rule evaluation
# --------------------------------------------------------------------------

def test_rule_equality_match():
    r = PolicyRule(rule_id="r", condition={"jurisdiction": "US"}, choice="USDC")
    assert r.matches({"jurisdiction": "US"})
    assert not r.matches({"jurisdiction": "EU"})


def test_rule_in_operator():
    r = PolicyRule(
        rule_id="r",
        condition={"jurisdiction": {"in": ["US", "EU"]}},
        choice="USDC",
    )
    assert r.matches({"jurisdiction": "US"})
    assert not r.matches({"jurisdiction": "LATAM"})


def test_rule_ne_operator():
    r = PolicyRule(
        rule_id="r", condition={"tier": {"ne": "blocked"}}, choice="proceed"
    )
    assert r.matches({"tier": "ok"})
    assert not r.matches({"tier": "blocked"})


def test_rule_unknown_operator_is_loud():
    r = PolicyRule(
        rule_id="r", condition={"x": {"wat": 1}}, choice="c"
    )
    with pytest.raises(KeyError):
        r.matches({"x": 1})


def test_policy_version_first_match_wins():
    p = _v1()
    result = p.select({"jurisdiction": "US"})
    assert result.choice == "USDC"
    assert result.matched_rule_id == "r1"
    assert result.policy_version == "1.0.0"


def test_policy_version_default_when_no_match():
    p = _v1()
    result = p.select({"jurisdiction": "EU"})
    assert result.choice == "USDC"  # default
    assert result.matched_rule_id is None
    assert result.rationale == "default_choice"


def test_policy_version_no_match_no_default():
    p = PolicyVersion(
        policy_id="p", version="1", rules=[], default_choice=None,
    )
    result = p.select({"x": 1})
    assert result.choice is None
    assert result.rationale == "no_match"


# --------------------------------------------------------------------------
# Registry versioning
# --------------------------------------------------------------------------

def test_registry_get_latest_after_publish():
    reg = PolicyRegistry()
    reg.publish(_v1(), valid_from=_ts(0), transaction_time=_ts(0))
    loaded = reg.get("stablecoin_router")
    assert loaded is not None
    assert loaded.version == "1.0.0"


def test_registry_as_of_returns_historical_version():
    """The core replay primitive for policy governance."""
    reg = PolicyRegistry()
    reg.publish(_v1(), valid_from=_ts(0), transaction_time=_ts(0))
    reg.publish(_v2(), valid_from=_ts(30), transaction_time=_ts(30))

    # As-of day 10: v1 is in effect.
    loaded = reg.get("stablecoin_router", as_of_transaction_time=_ts(10))
    assert loaded.version == "1.0.0"

    # As-of day 60: v2 is in effect.
    loaded = reg.get("stablecoin_router", as_of_transaction_time=_ts(60))
    assert loaded.version == "2.0.0"


def test_registry_history_returns_all_versions():
    reg = PolicyRegistry()
    reg.publish(_v1(), valid_from=_ts(0))
    reg.publish(_v2(), valid_from=_ts(30))
    hist = reg.history("stablecoin_router")
    assert [v.version for v in hist] == ["1.0.0", "2.0.0"]


def test_registry_returns_none_for_unknown_policy():
    reg = PolicyRegistry()
    assert reg.get("missing") is None


# --------------------------------------------------------------------------
# Agent router — end-to-end replay
# --------------------------------------------------------------------------

def test_agent_router_replays_historical_rule():
    """The Bridge replay scenario: on April 17, why did the agent pick USDT for LATAM?

    Answer must be "rule r2 of policy v1.0.0 fired". Under v2.0.0 (published
    later) the same context would have picked USDC — reading the current
    policy would be wrong.
    """
    reg = PolicyRegistry()
    reg.publish(_v1(), valid_from=_ts(0), transaction_time=_ts(0))
    reg.publish(_v2(), valid_from=_ts(30), transaction_time=_ts(30))

    router = AgentRouter(
        reg,
        use_case="cross_border_payout",
        policy_id="stablecoin_router",
    )

    # At decision time (day 5), v1 was in effect — LATAM routes USDT.
    decision_historical = router.route(
        {"jurisdiction": "LATAM"},
        as_of_transaction_time=_ts(5),
    )
    assert decision_historical.selected == "USDT"
    assert decision_historical.matched_rule_id == "r2"
    assert decision_historical.policy_version == "1.0.0"

    # Without the clamp, the current (v2) policy says LATAM routes USDC.
    decision_current = router.route({"jurisdiction": "LATAM"})
    assert decision_current.selected == "USDC"
    assert decision_current.policy_version == "2.0.0"

    # That divergence is exactly what examiners need to see reproduced.
    assert decision_historical.selected != decision_current.selected


def test_agent_routing_decision_carries_evidence_refs():
    reg = PolicyRegistry()
    reg.publish(_v1(), valid_from=_ts(0))
    router = AgentRouter(
        reg, use_case="x", policy_id="stablecoin_router"
    )
    d = router.route({"jurisdiction": "US"}, evidence_refs=["rec-1", "rec-2"])
    assert d.evidence_refs == ["rec-1", "rec-2"]


def test_agent_router_raises_when_policy_not_visible_at_clamp():
    reg = PolicyRegistry()
    reg.publish(_v1(), valid_from=_ts(100), transaction_time=_ts(100))
    router = AgentRouter(reg, use_case="x", policy_id="stablecoin_router")
    with pytest.raises(LookupError):
        # Clamp before the policy was published.
        router.route({"jurisdiction": "US"}, as_of_transaction_time=_ts(10))


def test_agent_routing_decision_serialises():
    d = AgentRoutingDecision(
        decision_id="d1",
        use_case="x",
        context={"jurisdiction": "US"},
        candidates=["USDC", "USDT"],
        selected="USDC",
        policy_id="p",
        policy_version="1.0.0",
        matched_rule_id="r1",
    )
    dd = d.to_dict()
    assert dd["decision_id"] == "d1"
    assert dd["selected"] == "USDC"
    # decided_at is serialized as ISO-8601 string.
    assert isinstance(dd["decided_at"], str)
