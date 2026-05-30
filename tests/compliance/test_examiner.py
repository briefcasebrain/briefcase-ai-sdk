"""ExaminerBundle end-to-end: build, serialize, verify, tamper-detect."""

import json
from datetime import datetime, timezone, timedelta

import pytest

from briefcase.bitemporal import (
    BitemporalRecord,
    InMemoryBitemporalStore,
    append_correction,
)
from briefcase.compliance import ExaminerBundle, BundleIntegrityError
from briefcase.routing import (
    AgentRouter,
    PolicyRegistry,
    PolicyRule,
    PolicyVersion,
)


UTC = timezone.utc


def _ts(days: int = 0) -> datetime:
    return datetime(2026, 4, 17, tzinfo=UTC) + timedelta(days=days)


def _build_scenario():
    """Set up an evidence store, a policy registry, and a decision."""
    # Evidence: OFAC status for two counterparties.
    evidence = InMemoryBitemporalStore()
    ofac_clean = BitemporalRecord.new(
        key="ofac:counterparty:cp-42",
        valid_time=_ts(0),
        value={"sanctioned": False},
        source="ofac",
        transaction_time=_ts(0),
    )
    evidence.append(ofac_clean)

    # A version-1 policy.
    reg = PolicyRegistry()
    reg.publish(
        PolicyVersion(
            policy_id="payout_router",
            version="1.0.0",
            rules=[
                PolicyRule(
                    rule_id="r1",
                    condition={"sanctioned": False, "jurisdiction": "US"},
                    choice="USDC",
                    rationale="US clean: route USDC",
                ),
            ],
            default_choice="human_review",
        ),
        valid_from=_ts(0),
        transaction_time=_ts(0),
    )

    router = AgentRouter(
        reg, use_case="cross_border_payout", policy_id="payout_router"
    )
    decision = router.route(
        {"sanctioned": False, "jurisdiction": "US"},
        evidence_refs=[ofac_clean.record_id],
    )
    return evidence, reg, decision, ofac_clean


def test_bundle_build_includes_decision_policy_and_evidence():
    evidence, reg, decision, ofac_clean = _build_scenario()
    bundle = ExaminerBundle.build(decision, evidence, reg)
    assert bundle.decision["selected"] == "USDC"
    assert bundle.policy is not None
    assert bundle.policy["version"] == "1.0.0"
    assert len(bundle.evidence) == 1
    assert bundle.evidence[0]["record_id"] == ofac_clean.record_id
    assert bundle.content_hash.startswith("sha256:")


def test_bundle_verify_roundtrips_cleanly():
    evidence, reg, decision, _ = _build_scenario()
    bundle = ExaminerBundle.build(decision, evidence, reg)

    payload = bundle.to_json()
    revived = ExaminerBundle.from_json(payload)
    revived.verify()  # does not raise


def test_bundle_detects_tamper():
    evidence, reg, decision, _ = _build_scenario()
    bundle = ExaminerBundle.build(decision, evidence, reg)

    tampered = json.loads(bundle.to_json())
    # Flip the selected choice; hash must stop matching.
    tampered["decision"]["selected"] = "USDT"
    tampered_bundle = ExaminerBundle.from_dict(tampered)
    with pytest.raises(BundleIntegrityError):
        tampered_bundle.verify()


def test_bundle_missing_evidence_raises():
    evidence, reg, decision, _ = _build_scenario()
    # Decision references a record not in the store.
    decision.evidence_refs.append("not-in-store")
    with pytest.raises(BundleIntegrityError):
        ExaminerBundle.build(decision, evidence, reg)


def test_bundle_reflects_policy_asof_decision():
    """Examiner-replay: bundle built later uses the policy that was in effect
    on the decision date, not whatever the registry currently holds."""
    evidence, reg, decision, _ = _build_scenario()

    # Publish a v2 policy long after the decision.
    reg.publish(
        PolicyVersion(
            policy_id="payout_router",
            version="2.0.0",
            rules=[PolicyRule(rule_id="r1", condition={}, choice="USDT")],
            default_choice=None,
        ),
        valid_from=_ts(100),
        transaction_time=_ts(100),
    )

    # Decision was made at decision.decided_at; bundle must reconstruct
    # the v1 policy, not v2.
    bundle = ExaminerBundle.build(decision, evidence, reg)
    assert bundle.policy["version"] == "1.0.0"
