"""Tests for Python bindings - core data models."""

from __future__ import annotations

import pytest
from briefcase import (
    DecisionSnapshot,
    ExecutionContext,
    Input,
    ModelParameters,
    Output,
    Snapshot,
    SnapshotQuery,
    init_with_config,
    is_initialized,
)
from briefcase.storage import SqliteBackend


class TestInput:
    def test_input_creation(self):
        input_obj = Input("test_input", "hello world", "string")

        assert input_obj.name == "test_input"
        assert input_obj.value == "hello world"
        assert input_obj.data_type == "string"

    def test_input_with_json_value(self):
        json_data = {"key": "value", "number": 42}
        input_obj = Input("json_input", json_data, "object")

        assert input_obj.value == json_data

    def test_input_to_dict(self):
        input_obj = Input("test", "value", "string")
        obj = input_obj.to_dict()

        assert obj["name"] == "test"
        assert obj["value"] == "value"
        assert obj["data_type"] == "string"


class TestOutput:
    def test_output_creation(self):
        output_obj = Output("test_output", "result", "string")

        assert output_obj.name == "test_output"
        assert output_obj.value == "result"
        assert output_obj.data_type == "string"
        assert output_obj.confidence is None

    def test_output_with_confidence(self):
        output_obj = Output("test", "result", "string")
        output_obj.with_confidence(0.95)

        assert output_obj.confidence == 0.95

    def test_output_to_dict_with_confidence(self):
        output_obj = Output("test", "result", "string")
        output_obj.with_confidence(0.85)

        obj = output_obj.to_dict()
        assert obj["name"] == "test"
        assert obj["confidence"] == 0.85


class TestModelParameters:
    def test_model_parameters_creation(self):
        params = ModelParameters("gpt-4")

        assert params.model_name == "gpt-4"
        assert params.provider is None

    def test_model_parameters_with_provider_and_parameters(self):
        params = ModelParameters("claude-3")
        params.with_provider("anthropic")
        params.with_parameter("temperature", 0.7)
        params.with_parameter("max_tokens", 1000)

        assert params.provider == "anthropic"
        assert params.parameters["temperature"] == 0.7
        assert params.parameters["max_tokens"] == 1000


class TestExecutionContext:
    def test_execution_context_properties(self):
        ctx = ExecutionContext()
        ctx.with_runtime_version("python3.11")
        ctx.with_dependency("pydantic", "2.0.0")
        ctx.with_random_seed(1234)
        ctx.with_env_var("ENV", "test")

        assert ctx.runtime_version == "python3.11"
        assert ctx.dependencies["pydantic"] == "2.0.0"
        assert ctx.random_seed == 1234
        assert ctx.environment_variables["ENV"] == "test"


class TestDecisionSnapshot:
    def test_decision_snapshot_creation(self):
        snapshot = DecisionSnapshot("my_function")

        assert snapshot.function_name == "my_function"
        assert snapshot.module_name is None
        assert snapshot.execution_time_ms is None

    def test_decision_snapshot_with_inputs_outputs_model_and_tags(self):
        snapshot = DecisionSnapshot("classify")
        snapshot.with_module("nlp")

        input_obj = Input("text", "hello", "string")
        output_obj = Output("label", "greeting", "string")
        params = ModelParameters("gpt-4")
        params.with_parameter("temperature", 0.1)

        snapshot.add_input(input_obj)
        snapshot.add_output(output_obj)
        snapshot.with_model_parameters(params)
        snapshot.with_execution_time(12.5)
        snapshot.add_tag("environment", "test")

        assert snapshot.function_name == "classify"
        assert snapshot.module_name == "nlp"
        assert snapshot.execution_time_ms == 12.5
        assert len(snapshot.inputs) == 1
        assert len(snapshot.outputs) == 1
        assert snapshot.tags["environment"] == "test"


class TestSnapshot:
    def test_snapshot_creation(self):
        snapshot = Snapshot("session")

        assert snapshot.snapshot_type == "session"
        assert len(snapshot.decisions) == 0

    def test_snapshot_with_invalid_type(self):
        with pytest.raises(Exception):
            Snapshot("invalid_type")

    def test_snapshot_add_decision(self):
        snapshot = Snapshot("batch")
        decision = DecisionSnapshot("test_func")

        snapshot.add_decision(decision)
        assert len(snapshot.decisions) == 1


class TestSnapshotQuery:
    def test_snapshot_query_builder(self):
        query = SnapshotQuery()
        query.with_function_name("fn")
        query.with_module_name("module")
        query.with_limit(10)
        query.with_offset(0)
        query.with_tag("env", "test")

        assert "SnapshotQuery" in repr(query)


class TestIntegration:
    def test_complete_storage_workflow(self):
        if not is_initialized():
            init_with_config(2)

        decision = DecisionSnapshot("text_classification")
        decision.with_module("nlp_service")
        decision.add_input(Input("text", "This is great", "string"))
        decision.add_output(
            Output("sentiment", "positive", "string").with_confidence(0.92)
        )
        decision.with_execution_time(45.2)
        decision.add_tag("environment", "staging")

        session = Snapshot("session")
        session.add_decision(decision)

        storage = SqliteBackend.in_memory()
        decision_id = storage.save_decision(decision)
        loaded_decision = storage.load_decision(decision_id)

        snapshot_id = storage.save(session)
        loaded_snapshot = storage.load(snapshot_id)

        assert loaded_decision.function_name == "text_classification"
        assert loaded_snapshot.snapshot_type == "session"
        assert len(loaded_snapshot.decisions) == 1
        assert storage.health_check() is True


class TestPythonToJsonConversion:
    def test_tuple_value_records_as_list(self):
        inp = Input("args", (1, 2), "tuple")
        assert inp.value == [1, 2]

    def test_unsupported_type_raises_type_error(self):
        with pytest.raises(TypeError):
            Input("payload", {1, 2, 3}, "set")

    def test_non_string_dict_key_raises_type_error(self):
        with pytest.raises(TypeError):
            Input("mapping", {1: "a"}, "dict")

    def test_large_int_round_trips_exactly(self):
        big = 2**63  # exceeds i64::MAX but fits u64
        inp = Input("count", big, "int")
        assert inp.value == big
        assert isinstance(inp.value, int)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_float_raises_type_error(self, value):
        """JSON has no NaN or Infinity. Storing null instead is indistinguishable
        from a field that was never computed, so it raises like every other
        value with no JSON equivalent."""
        with pytest.raises(TypeError):
            Input("score", value, "float")

    def test_non_finite_float_nested_in_a_dict_raises(self):
        with pytest.raises(TypeError):
            Input("metrics", {"drift_score": float("nan")}, "dict")

    def test_finite_floats_still_convert(self):
        assert Input("score", 0.5, "float").value == 0.5
        assert Input("score", -1.5e300, "float").value == -1.5e300

    def test_datetime_converts_to_an_iso_string(self):
        """datetime has exactly one JSON form and appears in most real
        payloads, so raising on it would reject ordinary captures."""
        from datetime import datetime, timezone

        moment = datetime(2026, 8, 12, 7, 30, tzinfo=timezone.utc)
        assert Input("seen_at", moment, "datetime").value == moment.isoformat()

    def test_date_and_time_convert_to_iso_strings(self):
        from datetime import date, time

        assert Input("day", date(2026, 8, 12), "date").value == "2026-08-12"
        assert Input("at", time(7, 30), "time").value == "07:30:00"

    def test_uuid_converts_to_its_canonical_string(self):
        import uuid

        value = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert Input("trace", value, "uuid").value == str(value)

    def test_datetime_nested_in_a_dict_converts(self):
        from datetime import datetime, timezone

        moment = datetime(2026, 8, 12, 7, 30, tzinfo=timezone.utc)
        assert Input("meta", {"seen_at": moment}, "dict").value == {
            "seen_at": moment.isoformat()
        }

    def test_types_without_one_obvious_json_form_still_raise(self):
        """A set has no defined order and a non-str key has no JSON spelling,
        so these keep raising rather than guessing."""
        with pytest.raises(TypeError):
            Input("payload", {1, 2, 3}, "set")
        with pytest.raises(TypeError):
            Input("mapping", {1: "a"}, "dict")
        with pytest.raises(TypeError):
            Input("obj", object(), "object")


if __name__ == "__main__":
    pytest.main([__file__])
