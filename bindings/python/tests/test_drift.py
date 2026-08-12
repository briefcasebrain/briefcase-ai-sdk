"""Tests for Python bindings - drift detection functionality."""

from __future__ import annotations

import pytest
from briefcase.drift import DriftCalculator


class TestDriftCalculator:
    def test_drift_calculator_creation(self):
        calculator = DriftCalculator()
        assert calculator is not None

    def test_drift_calculator_with_similarity_threshold(self):
        calculator = DriftCalculator()
        calculator.with_similarity_threshold(0.9)
        assert calculator.similarity_threshold == 0.9

    def test_drift_calculator_with_invalid_threshold(self):
        calculator = DriftCalculator()
        with pytest.raises(Exception):
            calculator.with_similarity_threshold(1.5)

    def test_calculate_drift_empty_outputs(self):
        calculator = DriftCalculator()
        metrics = calculator.calculate_drift([])

        assert metrics.consistency_score == 1.0
        assert metrics.agreement_rate == 1.0
        assert metrics.drift_score == 0.0
        assert metrics.consensus_output is None
        assert metrics.consensus_confidence == "none"
        assert list(metrics.outliers) == []

    def test_calculate_drift_single_output(self):
        calculator = DriftCalculator()
        metrics = calculator.calculate_drift(["hello world"])

        assert metrics.consistency_score == 1.0
        assert metrics.agreement_rate == 1.0
        assert metrics.drift_score == 0.0
        assert metrics.consensus_output == "hello world"
        assert metrics.consensus_confidence == "high"

    def test_calculate_drift_with_outlier(self):
        calculator = DriftCalculator()
        metrics = calculator.calculate_drift(
            ["consistent", "consistent", "consistent", "different"]
        )

        assert metrics.drift_score > 0
        assert len(metrics.outliers) >= 1

    def test_get_status(self):
        calculator = DriftCalculator()
        stable = calculator.calculate_drift(["same", "same", "same"])
        mixed = calculator.calculate_drift(["same", "different", "same"])

        assert stable.get_status(calculator) in {"stable", "drifting", "critical"}
        assert mixed.get_status(calculator) in {"stable", "drifting", "critical"}

    def test_metrics_to_dict(self):
        calculator = DriftCalculator()
        metrics = calculator.calculate_drift(["test1", "test2", "test1"])
        obj = metrics.to_dict()

        assert "consistency_score" in obj
        assert "agreement_rate" in obj
        assert "drift_score" in obj
        assert "consensus_output" in obj
        assert "consensus_confidence" in obj
        assert "outliers" in obj
        assert "total_samples" in obj

    def test_total_samples_counts_inputs_not_outliers(self):
        calculator = DriftCalculator()
        metrics = calculator.calculate_drift(["a", "b", "a"])
        assert metrics.total_samples == 3
        assert metrics.to_dict()["total_samples"] == 3


class TestDriftIntegration:
    def test_threshold_impact(self):
        strict = DriftCalculator()
        strict.with_similarity_threshold(0.95)

        lenient = DriftCalculator()
        lenient.with_similarity_threshold(0.7)

        outputs = ["hello", "helo", "hello"]
        strict_metrics = strict.calculate_drift(outputs)
        lenient_metrics = lenient.calculate_drift(outputs)

        assert strict_metrics.agreement_rate <= lenient_metrics.agreement_rate

    def test_large_output_set(self):
        calculator = DriftCalculator()
        outputs = ["consistent_result"] * 90 + ["outlier"] * 10

        metrics = calculator.calculate_drift(outputs)

        assert metrics.consensus_output == "consistent_result"
        assert metrics.agreement_rate > 0.8


if __name__ == "__main__":
    pytest.main([__file__])
