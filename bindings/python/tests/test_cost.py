"""Tests for Python bindings - cost calculation functionality."""

from __future__ import annotations

import pytest
from briefcase.cost import CostCalculator


class TestCostCalculator:
    def test_cost_calculator_creation(self):
        calculator = CostCalculator()
        assert calculator is not None

    def test_estimate_cost_basic(self):
        calculator = CostCalculator()
        estimate = calculator.estimate_cost("gpt-4", 1000, 500)

        assert estimate.model_name == "gpt-4"
        assert estimate.input_tokens == 1000
        assert estimate.output_tokens == 500
        assert estimate.total_cost > 0
        assert estimate.currency == "USD"

    def test_estimate_cost_unknown_model(self):
        calculator = CostCalculator()
        with pytest.raises(Exception):
            calculator.estimate_cost("unknown-model", 1000, 500)

    def test_estimate_cost_invalid_tokens(self):
        calculator = CostCalculator()
        with pytest.raises(Exception):
            calculator.estimate_cost("gpt-4", 0, 0)

    def test_estimate_cost_from_text(self):
        calculator = CostCalculator()
        estimate = calculator.estimate_cost_from_text("gpt-3.5-turbo", "hello world", 100)

        assert estimate.input_tokens > 0
        assert estimate.output_tokens == 100
        assert estimate.total_cost > 0

    def test_check_budget(self):
        calculator = CostCalculator()

        ok_status = calculator.check_budget(50.0, 100.0)
        assert ok_status.status == "ok"
        assert ok_status.percent_used == 50.0
        assert ok_status.remaining_budget == 50.0

        warning_status = calculator.check_budget(85.0, 100.0)
        assert warning_status.status == "warning"

        critical_status = calculator.check_budget(96.0, 100.0)
        assert critical_status.status == "critical"

        exceeded_status = calculator.check_budget(110.0, 100.0)
        assert exceeded_status.status == "exceeded"
        assert exceeded_status.remaining_budget == -10.0

    def test_get_cheapest_model(self):
        calculator = CostCalculator()
        cheapest = calculator.get_cheapest_model(8000)

        assert cheapest is not None
        assert isinstance(cheapest, str)

    def test_get_models_under_cost(self):
        calculator = CostCalculator()
        models = calculator.get_models_under_cost(0.01)

        assert isinstance(models, list)
        assert all(isinstance(name, str) for name in models)

    def test_get_models_by_provider(self):
        calculator = CostCalculator()
        openai_models = calculator.get_models_by_provider("openai")

        assert isinstance(openai_models, list)
        assert all(isinstance(name, str) for name in openai_models)

    def test_compare_models(self):
        calculator = CostCalculator()
        comparison = calculator.compare_models("gpt-4", "gpt-3.5-turbo", 1000, 500)

        assert isinstance(comparison, dict)
        assert set(comparison.keys()) == {
            "cheaper_model",
            "savings",
            "percent_difference",
        }
        assert isinstance(comparison["cheaper_model"], str)
        assert isinstance(comparison["savings"], float)
        assert isinstance(comparison["percent_difference"], float)

    def test_get_available_models(self):
        calculator = CostCalculator()
        models = calculator.get_available_models()

        assert isinstance(models, list)
        assert len(models) > 0
        assert all(isinstance(name, str) for name in models)

    def test_project_monthly_cost(self):
        calculator = CostCalculator()
        monthly_cost = calculator.project_monthly_cost("gpt-4", 4000, 2000, 30.0)

        assert isinstance(monthly_cost, float)
        assert monthly_cost > 0.0

    def test_estimate_tokens(self):
        calculator = CostCalculator()
        tokens = calculator.estimate_tokens("abcd" * 10)

        assert isinstance(tokens, int)
        assert tokens > 0


class TestCostObjects:
    def test_cost_estimate_to_dict(self):
        calculator = CostCalculator()
        estimate = calculator.estimate_cost("gpt-4", 1000, 500)

        obj = estimate.to_dict()
        assert obj["model_name"] == "gpt-4"
        assert obj["input_tokens"] == 1000
        assert obj["output_tokens"] == 500
        assert obj["total_cost"] == estimate.total_cost

    def test_budget_status_to_dict(self):
        calculator = CostCalculator()
        status = calculator.check_budget(50.0, 100.0)

        obj = status.to_dict()
        assert obj["status"] == "ok"
        assert obj["current_spend"] == 50.0
        assert obj["budget_limit"] == 100.0
        assert obj["remaining_budget"] == 50.0
        assert obj["percent_used"] == 50.0


class TestCostIntegration:
    def test_model_selection_and_comparison(self):
        calculator = CostCalculator()
        models = calculator.get_available_models()

        assert len(models) > 1
        comparison = calculator.compare_models(models[0], models[1], 5000, 2000)
        assert comparison["cheaper_model"] in (models[0], models[1])

    def test_budget_monitoring_workflow(self):
        calculator = CostCalculator()
        budget = 1000.0
        current_spend = 750.0

        status = calculator.check_budget(current_spend, budget)
        assert status.status in {"ok", "warning", "critical", "exceeded"}

        additional_estimate = calculator.estimate_cost("gpt-4", 2000, 1000)
        projected_spend = current_spend + additional_estimate.total_cost
        new_status = calculator.check_budget(projected_spend, budget)
        assert new_status.status in {"ok", "warning", "critical", "exceeded"}


if __name__ == "__main__":
    pytest.main([__file__])
