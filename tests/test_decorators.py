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


#  capture_content modes

class TestCaptureContentModes:
    def test_default_mode_is_full(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def fn(secret):
            return "made-" + secret

        fn("payload")
        record = exp.export.call_args[0][0]
        assert "payload" in record["inputs"]["args"]
        assert "made-payload" in record["outputs"]["result"]

    def test_hash_mode_carries_digests_not_content(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False, capture_content="hash")
        def fn(secret, flag=True):
            return "made-" + secret

        fn("payload", flag=False)
        record = exp.export.call_args[0][0]
        blob = repr(record)
        assert "payload" not in blob
        assert "made-payload" not in blob
        inputs = record["inputs"]
        assert set(inputs) == {"args_sha256", "args_chars", "kwargs_sha256", "kwargs_chars"}
        assert len(inputs["args_sha256"]) == 64
        assert inputs["args_chars"] == len(repr(("payload",)))
        outputs = record["outputs"]
        assert set(outputs) == {"result_sha256", "result_chars", "result_type"}
        assert outputs["result_type"] == "str"

    def test_hash_mode_is_deterministic(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False, capture_content="hash")
        def fn(x):
            return x

        fn("same")
        first = exp.export.call_args[0][0]["inputs"]["args_sha256"]
        fn("same")
        second = exp.export.call_args[0][0]["inputs"]["args_sha256"]
        assert first == second

    def test_none_mode_carries_shape_only(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False, capture_content="none")
        def fn(secret, flag=True):
            return "made-" + secret

        fn("payload", flag=False)
        record = exp.export.call_args[0][0]
        blob = repr(record)
        assert "payload" not in blob
        assert "sha256" not in blob
        assert record["inputs"] == {"args_count": 1, "kwargs_count": 1}
        assert record["outputs"] == {"result_type": "str"}

    def test_none_mode_error_is_class_name_only(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False, capture_content="none")
        def fn():
            raise ValueError("secret detail in message")

        with pytest.raises(ValueError):
            fn()
        record = exp.export.call_args[0][0]
        assert record["error"] == "ValueError"
        assert "secret detail" not in repr(record)

    def test_hash_mode_error_is_class_name_plus_digest(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False, capture_content="hash")
        def fn():
            raise ValueError("secret detail in message")

        with pytest.raises(ValueError):
            fn()
        record = exp.export.call_args[0][0]
        assert record["error"].startswith("ValueError:sha256:")
        assert "secret detail" not in repr(record)

    def test_full_mode_redact_hook_rewrites_content(self):
        exp = _make_exporter()

        @capture(
            exporter=exp,
            async_capture=False,
            capture_content="full",
            redact=lambda s: s.replace("payload", "[REDACTED]"),
        )
        def fn(secret):
            return "made-" + secret

        fn("payload")
        record = exp.export.call_args[0][0]
        assert "payload" not in repr(record)
        assert "[REDACTED]" in record["inputs"]["args"]
        assert "[REDACTED]" in record["outputs"]["result"]

    def test_unknown_mode_raises_at_decoration_time(self):
        with pytest.raises(ValueError):
            @capture(capture_content="partial")
            def fn():
                return 1

    def test_unreprable_argument_does_not_prevent_the_call(self):
        exp = _make_exporter()

        class Unreprable:
            def __repr__(self):
                raise RuntimeError("no repr")

        @capture(exporter=exp, async_capture=False)
        def fn(x):
            return "ran"

        assert fn(Unreprable()) == "ran"
        record = exp.export.call_args[0][0]
        assert record["outputs"]["result"] == "'ran'"

    def test_unreprable_result_does_not_discard_the_result(self):
        exp = _make_exporter()

        class Unreprable:
            def __repr__(self):
                raise RuntimeError("no repr")

        sentinel = Unreprable()

        @capture(exporter=exp, async_capture=False)
        def fn():
            return sentinel

        assert fn() is sentinel

    def test_redact_hook_raising_fails_closed(self):
        exp = _make_exporter()

        def bad_redact(_s):
            raise RuntimeError("redactor down")

        @capture(exporter=exp, async_capture=False, redact=bad_redact)
        def fn(secret):
            return "made-" + secret

        assert fn("payload") == "made-payload"
        record = exp.export.call_args[0][0]
        # The raw content must not leak when the redactor fails.
        assert "payload" not in repr(record)

    def test_full_mode_bounds_oversized_reprs_with_ellipsis(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def fn(x):
            return x

        fn("a" * 100_000)
        record = exp.export.call_args[0][0]
        args_text = record["inputs"]["args"]
        assert len(args_text) <= 1000
        # Bounded repr keeps both ends of an oversized value around an
        # ellipsis instead of computing the full repr and slicing a prefix.
        assert "..." in args_text
        assert args_text.rstrip("',)").endswith("a")

    def test_small_values_render_identically_to_plain_repr(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def fn(x, flag=True):
            return x + 1

        fn(41, flag=False)
        record = exp.export.call_args[0][0]
        assert record["inputs"]["args"] == repr((41,))
        assert record["inputs"]["kwargs"] == repr({"flag": False})
        assert record["outputs"]["result"] == repr(42)

    def test_no_exporter_anywhere_skips_record_work_entirely(self):
        from briefcase.config import BriefcaseConfig

        BriefcaseConfig.reset()
        repr_calls = []

        class Tracked:
            def __repr__(self):
                repr_calls.append(1)
                return "<tracked>"

        try:

            @capture(async_capture=False)
            def fn(x):
                return "ran"

            assert fn(Tracked()) == "ran"
            assert repr_calls == []
        finally:
            BriefcaseConfig.reset()

    def test_no_exporter_still_reraises_the_original_error(self):
        from briefcase.config import BriefcaseConfig

        BriefcaseConfig.reset()
        try:

            @capture(async_capture=False)
            def fn():
                raise KeyError("boom")

            with pytest.raises(KeyError):
                fn()
        finally:
            BriefcaseConfig.reset()

    def test_async_function_honors_capture_content(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False, capture_content="none")
        async def fn(secret):
            return "made-" + secret

        assert asyncio.run(fn("payload")) == "made-payload"
        record = exp.export.call_args[0][0]
        assert "payload" not in repr(record)
        assert record["inputs"] == {"args_count": 1}
