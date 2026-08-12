"""
Tests for @briefcase.capture decorator.
"""

import asyncio
import time
import pytest
from unittest.mock import MagicMock

from briefcase.decorators import capture


#  Helper

def _make_exporter():
    exp = MagicMock()
    exp.export = MagicMock(return_value=None)
    return exp


#  Sync function tests

class TestCaptureSync:
    def test_decorated_function_returns_original_value(self):
        @capture
        def add(x, y):
            return x + y

        result = add(2, 3)
        assert result == 5

    def test_exporter_called_with_record(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def greet(name):
            return f"Hello, {name}"

        greet("Alice")
        time.sleep(0.05)
        exp.export.assert_called_once()
        record = exp.export.call_args[0][0]
        # decision_type defaults to __qualname__, so "greet" is contained in it
        assert "greet" in record["decision_type"]
        assert "outputs" in record
        assert "Hello, Alice" in record["outputs"]["result"]

    def test_default_decision_type_is_qualname(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def my_special_function():
            return 42

        my_special_function()
        record = exp.export.call_args[0][0]
        assert "my_special_function" in record["decision_type"]

    def test_custom_decision_type(self):
        exp = _make_exporter()

        @capture(decision_type="risk_score", exporter=exp, async_capture=False)
        def score():
            return 0.9

        score()
        record = exp.export.call_args[0][0]
        assert record["decision_type"] == "risk_score"

    def test_context_version_in_record(self):
        exp = _make_exporter()

        @capture(context_version="v3", exporter=exp, async_capture=False)
        def predict(x):
            return x * 2

        predict(5)
        record = exp.export.call_args[0][0]
        assert record["context_version"] == "v3"

    def test_no_context_version_omitted(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def func():
            return None

        func()
        record = exp.export.call_args[0][0]
        assert "context_version" not in record

    def test_timing_fields_present(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def slow():
            time.sleep(0.01)
            return "done"

        slow()
        record = exp.export.call_args[0][0]
        assert "started_at" in record
        assert "ended_at" in record
        assert record["execution_time_ms"] >= 0

    def test_inputs_captured_in_record(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def transform(data, factor=2):
            return data * factor

        transform("abc", factor=3)
        record = exp.export.call_args[0][0]
        assert "inputs" in record
        assert "abc" in record["inputs"].get("args", "")

    def test_error_recorded_and_reraised(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def boom():
            raise ValueError("something went wrong")

        with pytest.raises(ValueError, match="something went wrong"):
            boom()

        record = exp.export.call_args[0][0]
        assert "error" in record
        assert "something went wrong" in record["error"]

    def test_error_record_has_no_outputs(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def fail():
            raise RuntimeError("fail!")

        with pytest.raises(RuntimeError):
            fail()

        record = exp.export.call_args[0][0]
        assert record.get("outputs") == {}

    def test_function_name_field(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def my_func():
            return 1

        my_func()
        record = exp.export.call_args[0][0]
        assert record["function_name"] == "my_func"

    def test_decision_id_is_unique(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def noop():
            return None

        noop()
        noop()
        calls = exp.export.call_args_list
        ids = [c[0][0]["decision_id"] for c in calls]
        assert ids[0] != ids[1]

    def test_max_output_chars_truncates(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False, max_output_chars=10)
        def long_output():
            return "A" * 1000

        long_output()
        record = exp.export.call_args[0][0]
        assert len(record["outputs"]["result"]) <= 12  # repr adds quotes


#  Async function tests

class TestCaptureAsync:
    def test_async_function_returns_value(self):
        @capture
        async def async_add(x, y):
            return x + y

        result = asyncio.run(async_add(3, 4))
        assert result == 7

    def test_async_exporter_called(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        async def fetch(url):
            return f"data from {url}"

        asyncio.run(fetch("http://example.com"))
        exp.export.assert_called_once()

    def test_async_error_recorded_and_reraised(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        async def fail_async():
            raise TypeError("async failure")

        with pytest.raises(TypeError, match="async failure"):
            asyncio.run(fail_async())

        record = exp.export.call_args[0][0]
        assert "error" in record
        assert "async failure" in record["error"]

    def test_async_timing_recorded(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        async def async_noop():
            return None

        asyncio.run(async_noop())
        record = exp.export.call_args[0][0]
        assert "execution_time_ms" in record


#  Sync export path with coroutine exporters

class TestSyncExportWithAsyncExporter:
    def test_sync_capture_with_coroutine_exporter_no_running_loop(self):
        """A sync function exports through an async exporter synchronously."""
        from briefcase.exporters.memory import MemoryExporter
        mem = MemoryExporter()

        @capture(exporter=mem, async_capture=False)
        def classify(text):
            return text.upper()

        assert classify("hi") == "HI"
        assert len(mem.records) == 1
        assert mem.records[0]["function_name"] == "classify"

    def test_sync_capture_inside_running_loop_exports_record(self):
        """An async function with async_capture=False exports the record even
        though the caller's event loop is running."""
        from briefcase.exporters.memory import MemoryExporter
        mem = MemoryExporter()

        @capture(exporter=mem, async_capture=False)
        async def decide(x):
            return x * 2

        result = asyncio.run(decide(21))
        assert result == 42
        assert len(mem.records) == 1
        assert mem.records[0]["function_name"] == "decide"


#  Decorator form tests

class TestCaptureDecoratorForms:
    def test_bare_decorator_no_parens(self):
        """@capture works without parentheses."""
        []

        @capture
        def func(x):
            return x + 1

        # Should be callable and return correct value
        assert func(10) == 11

    def test_decorator_with_empty_parens(self):
        """@capture() works with empty parentheses."""
        @capture()
        def func(x):
            return x * 2

        assert func(5) == 10

    def test_functools_wraps_preserves_name(self):
        @capture
        def original_name(x):
            return x

        assert original_name.__name__ == "original_name"

    def test_functools_wraps_preserves_doc(self):
        @capture
        def documented_func(x):
            """My docstring."""
            return x

        assert documented_func.__doc__ == "My docstring."

    def test_direct_call_forwards_configuration(self):
        """capture(fn, ...) honors the keyword configuration it receives."""
        exp = _make_exporter()

        def func(x):
            return x + 1

        wrapped = capture(
            func,
            decision_type="direct",
            context_version="v9",
            exporter=exp,
            async_capture=False,
        )

        assert wrapped(1) == 2
        exp.export.assert_called_once()
        record = exp.export.call_args[0][0]
        assert record["decision_type"] == "direct"
        assert record["context_version"] == "v9"
