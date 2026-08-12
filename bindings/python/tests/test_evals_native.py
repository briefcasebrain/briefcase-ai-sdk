"""Tests for EvalRun against the real native cost and drift calculators.

The tests/ suite mocks briefcase._native, so the lazy fallback in
EvalRun.summary() only ever meets a MagicMock there. These run against the
compiled extension.
"""

from __future__ import annotations

import pytest
from briefcase.cost import CostCalculator
from briefcase.exporters.memory import MemoryExporter
from briefcase.integrations.evals import EvalRun

MODEL = "claude-opus-4-5"


@pytest.fixture
def mem():
    return MemoryExporter()


class TestNativeCost:
    def test_lazy_native_calculator_produces_a_cost(self, mem):
        run = EvalRun("native-cost", exporter=mem, model=MODEL)
        run.log_case("a", input_tokens=1000, output_tokens=500)

        cost = run.summary()["cost"]
        assert isinstance(cost, float)
        assert cost > 0.0

    def test_cost_matches_a_direct_calculator_call(self, mem):
        run = EvalRun("native-cost", exporter=mem, model=MODEL)
        run.log_case("a", input_tokens=700, output_tokens=200)
        run.log_case("b", input_tokens=300, output_tokens=300)

        expected = CostCalculator().estimate_cost(MODEL, 1000, 500).total_cost
        assert run.summary()["cost"] == pytest.approx(expected)

    def test_unknown_model_degrades_to_none(self, mem):
        run = EvalRun("native-cost", exporter=mem, model="not-a-real-model")
        run.log_case("a", input_tokens=10, output_tokens=5)
        assert run.summary()["cost"] is None

    def test_token_count_beyond_u32_degrades_to_none(self, mem):
        run = EvalRun("native-cost", exporter=mem, model=MODEL)
        run.log_case("a", input_tokens=2**40, output_tokens=1)
        assert run.summary()["cost"] is None


class TestNativeDrift:
    def test_lazy_native_calculator_produces_drift_metrics(self, mem):
        run = EvalRun("native-drift", exporter=mem)
        for index, text in enumerate(["the answer is 4", "the answer is 4", "banana"]):
            run.log_case(f"c{index}", outputs=text)

        drift = run.summary(include_drift=True)["drift"]
        assert set(drift) == {"drift_score", "consistency_score", "agreement_rate"}
        assert all(isinstance(value, float) for value in drift.values())

    def test_identical_outputs_drift_less_than_divergent_ones(self, mem):
        same = EvalRun("same", exporter=mem)
        for index in range(4):
            same.log_case(f"c{index}", outputs="the answer is 4")

        different = EvalRun("different", exporter=mem)
        for index, text in enumerate(["alpha", "beta", "gamma", "delta"]):
            different.log_case(f"c{index}", outputs=text)

        assert (
            same.summary(include_drift=True)["drift"]["drift_score"]
            < different.summary(include_drift=True)["drift"]["drift_score"]
        )

    def test_drift_still_omitted_by_default(self, mem):
        run = EvalRun("native-drift", exporter=mem)
        run.log_case("a", outputs="one")
        run.log_case("b", outputs="two")
        assert run.summary()["drift"] is None


def test_run_record_carries_native_cost(mem):
    with EvalRun("native-run", exporter=mem, model=MODEL) as run:
        run.log_case("a", input_tokens=100, output_tokens=50, passed=True)

    outputs = mem.records[-1]["outputs"]
    assert outputs["cost"] > 0.0
    assert outputs["pass_rate"] == 1.0
