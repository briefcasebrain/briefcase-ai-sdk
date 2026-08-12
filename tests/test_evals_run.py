"""Tests for EvalRun: per-case records, run summary, cost/drift enrichment,
and export through the ExportMixin path."""

import pytest

import briefcase
from briefcase.config import BriefcaseConfig
from briefcase.exporters.memory import MemoryExporter
from briefcase.integrations.evals.run import EvalRun


@pytest.fixture
def mem():
    return MemoryExporter()


@pytest.fixture(autouse=True)
def _restore_global_config():
    previous = BriefcaseConfig.get().exporter
    yield
    BriefcaseConfig.get().exporter = previous


class _FakeEstimate:
    def __init__(self, total):
        self.total_cost = total


class _FakeCostCalculator:
    """Records the arguments it was called with and returns a fixed cost."""

    def __init__(self, total=0.25):
        self.total = total
        self.calls = []

    def estimate_cost(self, model_name, input_tokens, output_tokens):
        self.calls.append((model_name, input_tokens, output_tokens))
        return _FakeEstimate(self.total)


class _RaisingCostCalculator:
    def estimate_cost(self, *args, **kwargs):
        raise RuntimeError("native boom")


class _FakeDriftMetrics:
    def __init__(self):
        self.drift_score = 0.4
        self.consistency_score = 0.6
        self.agreement_rate = 0.5


class _FakeDriftCalculator:
    def __init__(self):
        self.calls = []

    def calculate_drift(self, outputs):
        self.calls.append(list(outputs))
        return _FakeDriftMetrics()


class _RaisingDriftCalculator:
    def calculate_drift(self, outputs):
        raise RuntimeError("native boom")


class TestLogCase:
    def test_case_record_shape_and_export(self, mem):
        run = EvalRun("acc-eval", exporter=mem, run_id="run-1", model="claude-opus-5")
        record = run.log_case(
            "case-1",
            inputs="what is 2+2?",
            outputs="4",
            target="4",
            scores={"accuracy": 1.0},
            passed=True,
            input_tokens=12,
            output_tokens=3,
            tags=["math"],
            metadata={"split": "test"},
        )

        assert record["decision_type"] == "eval.case"
        assert record["function_name"] == "acc-eval"
        assert record["run_id"] == "run-1"
        assert record["case_id"] == "case-1"
        assert record["model"] == "claude-opus-5"
        assert record["inputs"] == {"inputs": "'what is 2+2?'"}
        assert record["outputs"] == {"result": "'4'"}
        assert record["target"] == "'4'"
        assert record["scores"] == {"accuracy": 1.0}
        assert record["passed"] is True
        assert record["input_tokens"] == 12
        assert record["output_tokens"] == 3
        assert record["tags"] == ["math"]
        assert record["metadata"] == {"split": "test"}
        assert "decision_id" in record
        assert "started_at" in record

        assert mem.records == [record]

    def test_run_id_defaults_to_uuid(self, mem):
        a = EvalRun("e", exporter=mem)
        b = EvalRun("e", exporter=mem)
        assert a.run_id != b.run_id
        assert len(a.run_id) == 36

    def test_long_values_are_truncated(self, mem):
        run = EvalRun("e", exporter=mem)
        record = run.log_case("c", inputs="x" * 5000, outputs="y" * 5000)
        assert len(record["inputs"]["inputs"]) == 1000
        assert len(record["outputs"]["result"]) == 1000

    def test_omitted_fields_are_absent(self, mem):
        run = EvalRun("e", exporter=mem)
        record = run.log_case("c")
        assert record["inputs"] == {}
        assert record["outputs"] == {}
        assert "target" not in record
        assert record["passed"] is None
        assert record["scores"] == {}


class TestSummary:
    def test_pass_rate_and_counts(self, mem):
        run = EvalRun("e", exporter=mem)
        run.log_case("a", passed=True)
        run.log_case("b", passed=False)
        run.log_case("c", passed=True)

        summary = run.summary()
        assert summary["total_cases"] == 3
        assert summary["passed"] == 2
        assert summary["failed"] == 1
        assert summary["pass_rate"] == pytest.approx(2 / 3)

    def test_pass_rate_is_none_without_pass_flags(self, mem):
        run = EvalRun("e", exporter=mem)
        run.log_case("a", scores={"f1": 0.5})
        assert run.summary()["pass_rate"] is None

    def test_score_statistics(self, mem):
        run = EvalRun("e", exporter=mem)
        run.log_case("a", scores={"f1": 0.2, "bleu": 1.0})
        run.log_case("b", scores={"f1": 0.8})

        scores = run.summary()["scores"]
        assert scores["f1"] == {"mean": pytest.approx(0.5), "min": 0.2, "max": 0.8, "count": 2}
        assert scores["bleu"] == {"mean": 1.0, "min": 1.0, "max": 1.0, "count": 1}

    def test_non_numeric_scores_are_ignored(self, mem):
        run = EvalRun("e", exporter=mem)
        run.log_case("a", scores={"grade": "C", "f1": 0.5})
        assert set(run.summary()["scores"]) == {"f1"}

    def test_empty_run_summary(self, mem):
        summary = EvalRun("e", exporter=mem).summary()
        assert summary["total_cases"] == 0
        assert summary["pass_rate"] is None
        assert summary["scores"] == {}
        assert summary["cost"] is None

    def test_token_totals(self, mem):
        run = EvalRun("e", exporter=mem)
        run.log_case("a", input_tokens=10, output_tokens=2)
        run.log_case("b", input_tokens=5, output_tokens=1)
        summary = run.summary()
        assert summary["input_tokens"] == 15
        assert summary["output_tokens"] == 3


class TestCost:
    def test_cost_from_injected_calculator(self, mem):
        calc = _FakeCostCalculator(total=0.25)
        run = EvalRun("e", exporter=mem, model="claude-opus-5", cost_calculator=calc)
        run.log_case("a", input_tokens=10, output_tokens=2)

        assert run.summary()["cost"] == 0.25
        assert calc.calls == [("claude-opus-5", 10, 2)]

    def test_cost_is_none_without_model(self, mem):
        run = EvalRun("e", exporter=mem, cost_calculator=_FakeCostCalculator())
        run.log_case("a", input_tokens=10, output_tokens=2)
        assert run.summary()["cost"] is None

    def test_cost_is_none_without_tokens(self, mem):
        run = EvalRun("e", exporter=mem, model="m", cost_calculator=_FakeCostCalculator())
        run.log_case("a")
        assert run.summary()["cost"] is None

    def test_cost_degrades_to_none_when_calculator_raises(self, mem):
        run = EvalRun("e", exporter=mem, model="m", cost_calculator=_RaisingCostCalculator())
        run.log_case("a", input_tokens=10, output_tokens=2)
        assert run.summary()["cost"] is None

    def test_cost_degrades_to_none_on_magicmock_calculator(self, mem):
        from unittest.mock import MagicMock

        run = EvalRun("e", exporter=mem, model="m", cost_calculator=MagicMock())
        run.log_case("a", input_tokens=10, output_tokens=2)
        assert run.summary()["cost"] is None

    def test_lazy_native_calculator_never_raises(self, mem):
        # tests/mock_core.py replaces briefcase._native with a MagicMock, so the
        # lazy fallback yields non-numeric costs; summary must still produce None.
        run = EvalRun("e", exporter=mem, model="m")
        run.log_case("a", input_tokens=10, output_tokens=2)
        assert run.summary()["cost"] is None


class TestDrift:
    def test_drift_omitted_by_default(self, mem):
        run = EvalRun("e", exporter=mem, drift_calculator=_FakeDriftCalculator())
        run.log_case("a", outputs="one")
        run.log_case("b", outputs="two")
        assert run.summary()["drift"] is None

    def test_drift_from_injected_calculator(self, mem):
        calc = _FakeDriftCalculator()
        run = EvalRun("e", exporter=mem, drift_calculator=calc)
        run.log_case("a", outputs="one")
        run.log_case("b", outputs="two")

        drift = run.summary(include_drift=True)["drift"]
        assert drift == {
            "drift_score": 0.4,
            "consistency_score": 0.6,
            "agreement_rate": 0.5,
        }
        assert calc.calls == [["one", "two"]]

    def test_drift_needs_two_string_outputs(self, mem):
        calc = _FakeDriftCalculator()
        run = EvalRun("e", exporter=mem, drift_calculator=calc)
        run.log_case("a", outputs="only one")
        assert run.summary(include_drift=True)["drift"] is None
        assert calc.calls == []

    def test_drift_degrades_to_none_when_calculator_raises(self, mem):
        run = EvalRun("e", exporter=mem, drift_calculator=_RaisingDriftCalculator())
        run.log_case("a", outputs="one")
        run.log_case("b", outputs="two")
        assert run.summary(include_drift=True)["drift"] is None

    def test_drift_degrades_to_none_on_magicmock_calculator(self, mem):
        from unittest.mock import MagicMock

        run = EvalRun("e", exporter=mem, drift_calculator=MagicMock())
        run.log_case("a", outputs="one")
        run.log_case("b", outputs="two")
        assert run.summary(include_drift=True)["drift"] is None


class TestFinish:
    def test_context_manager_emits_run_record(self, mem):
        with EvalRun("acc-eval", exporter=mem, model="m") as run:
            run.log_case("a", passed=True)
            run.log_case("b", passed=False)

        assert [r["decision_type"] for r in mem.records] == [
            "eval.case",
            "eval.case",
            "eval.run",
        ]
        run_record = mem.records[-1]
        assert run_record["function_name"] == "acc-eval"
        assert run_record["outputs"]["total_cases"] == 2
        assert run_record["outputs"]["pass_rate"] == 0.5
        assert "execution_time_ms" in run_record
        assert "ended_at" in run_record

    def test_finish_is_idempotent(self, mem):
        run = EvalRun("e", exporter=mem)
        run.log_case("a", passed=True)
        first = run.finish()
        second = run.finish()
        assert first == second
        assert sum(1 for r in mem.records if r["decision_type"] == "eval.run") == 1

    def test_exit_does_not_suppress_exceptions(self, mem):
        with pytest.raises(ValueError):
            with EvalRun("e", exporter=mem) as run:
                run.log_case("a")
                raise ValueError("boom")
        assert mem.records[-1]["decision_type"] == "eval.run"

    def test_logging_after_finish_raises(self, mem):
        run = EvalRun("e", exporter=mem)
        run.finish()
        with pytest.raises(RuntimeError, match="finished"):
            run.log_case("a")


class TestIngest:
    def test_ingest_normalized_cases(self, mem):
        run = EvalRun("e", exporter=mem)
        count = run.ingest([
            {"case_id": "a", "inputs": "q1", "outputs": "r1", "scores": {"f1": 1.0}, "passed": True},
            {"case_id": "b", "inputs": "q2", "outputs": "r2", "scores": {"f1": 0.0}, "passed": False},
        ])
        assert count == 2
        assert [r["case_id"] for r in mem.records] == ["a", "b"]
        assert run.summary()["pass_rate"] == 0.5

    def test_ingest_generates_case_ids_when_missing(self, mem):
        run = EvalRun("e", exporter=mem)
        run.ingest([{"outputs": "r"}])
        assert mem.records[0]["case_id"] == "case-0"

    def test_ingest_keeps_falsy_case_ids(self, mem):
        run = EvalRun("e", exporter=mem)
        run.ingest([{"case_id": 0, "outputs": "r"}, {"case_id": "", "outputs": "s"}])
        assert [r["case_id"] for r in mem.records] == [0, ""]


class TestGlobalExporterFallback:
    def test_falls_back_to_observe_exporter(self):
        collected = briefcase.observe("memory")
        with EvalRun("e") as run:
            run.log_case("a", passed=True)
        assert [r["decision_type"] for r in collected.records] == ["eval.case", "eval.run"]
