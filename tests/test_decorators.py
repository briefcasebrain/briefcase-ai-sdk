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

    def test_duration_matches_the_recorded_timestamps(self):
        from datetime import datetime

        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def trivial():
            return 1

        async def atrivial():
            return 1

        def gen():
            yield 1

        trivial()
        asyncio.run(capture(exporter=exp, async_capture=False)(atrivial)())
        list(capture(exporter=exp, async_capture=False)(gen)())
        assert exp.export.call_count == 3
        for call in exp.export.call_args_list:
            record = call[0][0]
            started = datetime.fromisoformat(record["started_at"])
            ended = datetime.fromisoformat(record["ended_at"])
            assert (ended - started).total_seconds() * 1000 == record["execution_time_ms"]

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


#  Generators, cancellation, correlation, sampling, typing

class TestCaptureGenerators:
    def test_sync_generator_record_holds_chunks_and_total_duration(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def stream(n):
            for i in range(n):
                time.sleep(0.01)
                yield f"tok{i}"

        gen = stream(3)
        exp.export.assert_not_called()  # nothing until the generator runs
        assert list(gen) == ["tok0", "tok1", "tok2"]
        exp.export.assert_called_once()
        record = exp.export.call_args[0][0]
        assert record["outputs"]["result"] == "['tok0', 'tok1', 'tok2']"
        assert "generator object" not in record["outputs"]["result"]
        assert record["outputs"]["chunk_count"] == 3
        assert record["outputs"]["stream_completed"] is True
        assert record["execution_time_ms"] >= 25
        assert "error" not in record

    def test_sync_generator_keeps_send_and_return_semantics(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def echo():
            received = yield "ready"
            yield f"got {received}"
            return "done"

        gen = echo()
        assert next(gen) == "ready"
        assert gen.send("x") == "got x"
        with pytest.raises(StopIteration) as stop:
            next(gen)
        assert stop.value.value == "done"
        assert exp.export.call_args[0][0]["outputs"]["chunk_count"] == 2

    def test_sync_generator_closed_early_emits_one_record(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def stream():
            yield 1
            yield 2
            yield 3

        for value in stream():
            if value == 2:
                break
        exp.export.assert_called_once()
        outputs = exp.export.call_args[0][0]["outputs"]
        assert outputs["chunk_count"] == 2
        assert outputs["stream_completed"] is False

    def test_sync_generator_error_is_recorded_and_reraised(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def stream():
            yield 1
            raise RuntimeError("mid-stream")

        with pytest.raises(RuntimeError, match="mid-stream"):
            list(stream())
        record = exp.export.call_args[0][0]
        assert record["error"] == "mid-stream"
        assert record["outputs"]["chunk_count"] == 1

    def test_collected_chunks_are_capped(self, monkeypatch):
        monkeypatch.setattr("briefcase.decorators.MAX_COLLECTED_CHUNKS", 5)
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False, capture_content="hash")
        def stream():
            yield from range(50)

        assert sum(stream()) == sum(range(50))
        outputs = exp.export.call_args[0][0]["outputs"]
        assert outputs["chunk_count"] == 50
        assert outputs["chunks_truncated"] is True
        assert outputs["result_type"] == "generator"
        assert outputs["result_chars"] == len(repr(list(range(5))))

    def test_generator_without_exporter_passes_through(self):
        @capture
        def stream():
            yield 1
            return "r"

        gen = stream()
        assert next(gen) == 1
        with pytest.raises(StopIteration) as stop:
            next(gen)
        assert stop.value.value == "r"

    def test_async_generator_is_wrapped_as_an_async_generator(self):
        import inspect

        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        async def stream(n):
            for i in range(n):
                await asyncio.sleep(0.01)
                yield i

        assert inspect.isasyncgenfunction(stream)

        async def main():
            return [v async for v in stream(3)]

        assert asyncio.run(main()) == [0, 1, 2]
        exp.export.assert_called_once()
        record = exp.export.call_args[0][0]
        assert record["outputs"]["result"] == "[0, 1, 2]"
        assert record["outputs"]["stream_completed"] is True
        assert record["execution_time_ms"] >= 25

    def test_async_generator_error_and_early_close(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        async def failing():
            yield 1
            raise ValueError("bad chunk")

        @capture(exporter=exp, async_capture=False)
        async def endless():
            i = 0
            while True:
                yield i
                i += 1

        async def main():
            with pytest.raises(ValueError):
                async for _ in failing():
                    pass
            first = exp.export.call_args[0][0]
            agen = endless()
            async for value in agen:
                if value == 1:
                    break
            await agen.aclose()
            return first, exp.export.call_args[0][0]

        first, second = asyncio.run(main())
        assert first["error"] == "bad chunk"
        assert second["outputs"]["chunk_count"] == 2
        assert second["outputs"]["stream_completed"] is False
        assert exp.export.call_count == 2


class TestCaptureCancellation:
    def test_timed_out_async_call_leaves_a_cancelled_record(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        async def slow():
            await asyncio.sleep(10)

        async def main():
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(slow(), timeout=0.02)

        asyncio.run(main())
        exp.export.assert_called_once()
        record = exp.export.call_args[0][0]
        assert record["status"] == "cancelled"
        assert record["error"] == "CancelledError"
        assert "ended_at" in record

    def test_cancelled_task_reraises_cancellation(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        async def slow():
            await asyncio.sleep(10)

        async def main():
            task = asyncio.ensure_future(slow())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(main())
        assert exp.export.call_args[0][0]["status"] == "cancelled"

    def test_ordinary_error_has_no_status_field(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def boom():
            raise ValueError()

        with pytest.raises(ValueError):
            boom()
        record = exp.export.call_args[0][0]
        assert "status" not in record
        assert record["error"] == "ValueError"  # empty message falls back to the class


class TestCaptureCorrelation:
    def test_schema_version_present(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def fn():
            return 1

        fn()
        assert exp.export.call_args[0][0]["schema_version"] == "1.2"

    def test_workflow_id_is_picked_up_from_the_correlation_context(self):
        from briefcase.correlation import briefcase_workflow

        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def fn():
            return 1

        with briefcase_workflow("wf", None, workflow_id="wf_test123"):
            fn()
        assert exp.export.call_args[0][0]["workflow_id"] == "wf_test123"
        fn()
        assert "workflow_id" not in exp.export.call_args[0][0]

    def test_active_otel_span_context_is_recorded(self):
        trace = pytest.importorskip("opentelemetry.trace")
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False)
        def fn():
            return 1

        parent = trace.NonRecordingSpan(trace.SpanContext(
            trace_id=0x1234, span_id=0xABCD, is_remote=False,
            trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
        ))
        with trace.use_span(parent):
            fn()
        record = exp.export.call_args[0][0]
        assert record["trace_id"] == format(0x1234, "032x")
        assert record["parent_span_id"] == format(0xABCD, "016x")
        assert record["trace_flags"] == 1
        fn()
        assert "trace_id" not in exp.export.call_args[0][0]


class TestCaptureSampling:
    def test_sample_rate_is_deterministic_with_an_injected_rng(self):
        import random

        def run(seed):
            exp = _make_exporter()

            @capture(exporter=exp, async_capture=False, sample_rate=0.5,
                     rng=random.Random(seed))
            def fn(i):
                return i

            assert [fn(i) for i in range(200)] == list(range(200))
            return [c[0][0]["inputs"]["args"] for c in exp.export.call_args_list]

        first, again = run(7), run(7)
        assert first == again
        assert 60 < len(first) < 140

    def test_sample_rate_zero_and_one(self):
        exp = _make_exporter()

        @capture(exporter=exp, async_capture=False, sample_rate=0.0)
        def never():
            return 1

        @capture(exporter=exp, async_capture=False, sample_rate=1.0)
        def always():
            return 1

        never()
        exp.export.assert_not_called()
        always()
        exp.export.assert_called_once()

    @pytest.mark.parametrize("bad", [-0.1, 1.5, "0.5", True])
    def test_invalid_sample_rate_rejected(self, bad):
        with pytest.raises(ValueError, match="sample_rate"):
            capture(sample_rate=bad)


class TestCaptureTyping:
    def test_decorated_functions_keep_their_signatures_under_mypy(self, tmp_path):
        api = pytest.importorskip("mypy.api")
        snippet = tmp_path / "snippet.py"
        snippet.write_text(
            "from briefcase.decorators import capture\n"
            "@capture\n"
            "def bare(x: int) -> str:\n"
            "    return str(x)\n"
            "@capture(decision_type='t', sample_rate=0.5)\n"
            "async def configured(x: int, *, y: str = '') -> bytes:\n"
            "    return b''\n"
            "reveal_type(bare)\n"
            "reveal_type(configured)\n"
            "bare('wrong')\n"
        )
        out, _err, _status = api.run([str(snippet), "--no-incremental"])
        assert 'def (x: int) -> str' in out.replace("builtins.", "")
        assert "Coroutine[Any, Any, bytes]" in out.replace("builtins.", "")
        assert 'Argument 1 to "bare" has incompatible type "str"' in out
