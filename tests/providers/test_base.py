"""Shared provider machinery: stream wrapping, content bounds, options."""

import asyncio
import json

import pytest

import briefcase
from briefcase.integrations import providers
from briefcase.integrations.providers._base import (
    AsyncStreamProxy,
    ProviderInstrumentor,
    StreamProxy,
    instrument_stream,
    jsonable,
    normalize_capture_content,
    scalar_params,
    token_usage,
)


def _inst(mem, **options):
    return ProviderInstrumentor("test", exporter=mem, async_capture=False, **options)


def test_capture_content_values():
    assert normalize_capture_content(False) == "none"
    assert normalize_capture_content(None) == "none"
    assert normalize_capture_content(True) == "full"
    assert normalize_capture_content("hash") == "hash"
    with pytest.raises(ValueError):
        normalize_capture_content("everything")
    assert ProviderInstrumentor("test").captures_content is False


def test_jsonable_bounds_and_never_reprs_objects():
    class Opaque:
        def __repr__(self):
            return "SECRET-REPR"

    out = jsonable({"a": "x" * 50, "b": b"12345", "c": [Opaque()], "d": (1, 2.5, None, True)}, 10)
    assert out == {
        "a": "x" * 10 + "...", "b": "<5 bytes>", "c": ["<Opaque>"], "d": [1, 2.5, None, True]}
    json.dumps(out)
    # An iterator is named, not consumed.
    gen = (i for i in range(3))
    assert jsonable(gen) == "<generator>"
    assert list(gen) == [0, 1, 2]


def test_scalar_params_drop_non_scalars():
    class Omit:
        pass

    assert scalar_params(
        {"temperature": 0.1, "stop": ["a"], "top_p": Omit(), "seed": 3}, ("temperature", "stop", "top_p", "seed"),
    ) == {"temperature": 0.1, "seed": 3}


def test_token_usage_normalization():
    assert token_usage(None, None, None) is None
    assert token_usage(3, 4) == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert token_usage(3, 4, 9, cached=2, absent=None) == {
        "prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 9, "cached": 2}


def test_record_reuses_the_capture_layout(mem):
    """Same schema_version / id / timing fields as an @capture record."""
    @briefcase.capture(exporter=mem, async_capture=False)
    def decorated():
        return 1

    decorated()
    call = _inst(mem, context_version="v7").begin("llm.test.chat", "test.create", model="m")
    call.finish({"usage": token_usage(1, 2), "finish_reason": "stop"})

    captured, provider = mem.records
    assert set(captured) <= set(provider)
    assert provider["schema_version"] == captured["schema_version"]
    assert provider["context_version"] == "v7"
    assert provider["decision_id"] != captured["decision_id"]


def test_finish_is_idempotent(mem):
    call = _inst(mem).begin("llm.test.chat", "test.create", model="m")
    call.finish({"finish_reason": "stop"})
    call.finish({"finish_reason": "again"})
    call.finish(error=RuntimeError("late"))
    assert len(mem.records) == 1
    assert mem.records[0]["finish_reason"] == "stop"


def test_cancellation_is_recorded_as_cancelled(mem):
    call = _inst(mem).begin("llm.test.chat", "test.create", model="m")
    call.finish(error=asyncio.CancelledError())
    assert mem.records[0]["status"] == "cancelled"
    assert mem.records[0]["error_type"] == "CancelledError"


def test_trace_context_is_attached(mem):
    trace = pytest.importorskip("opentelemetry.trace")
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")

    tracer = sdk_trace.TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("parent") as span:
        call = _inst(mem).begin("llm.test.chat", "test.create", model="m")
        call.finish()
    context = span.get_span_context()
    assert mem.records[0]["trace_id"] == format(context.trace_id, "032x")
    assert mem.records[0]["parent_span_id"] == format(context.span_id, "016x")
    assert trace is not None


class _PlainStream:
    """A stream with no ``_iterator``: forces the proxy fallback."""

    def __init__(self, items):
        self.items = items
        self.closed = False
        self.response = "resp"

    def __iter__(self):
        return iter(self.items)

    def close(self):
        self.closed = True


def test_stream_without_iterator_attribute_falls_back_to_a_proxy():
    seen, ends = [], []
    inner = _PlainStream([1, 2, 3])
    proxy = instrument_stream(inner, seen.append, lambda done, err: ends.append((done, err)),
                              is_async=False)

    assert isinstance(proxy, StreamProxy)
    assert proxy.response == "resp"  # everything else is delegated
    assert list(proxy) == [1, 2, 3]
    assert seen == [1, 2, 3]
    assert ends == [(True, None)]
    with proxy:
        pass
    assert inner.closed is True


def test_async_proxy_fallback():
    class _AsyncPlain:
        closed = False

        async def __aiter__(self):
            for item in (1, 2):
                yield item

        async def close(self):
            self.closed = True

    seen, ends = [], []
    inner = _AsyncPlain()

    async def main():
        proxy = instrument_stream(
            inner, seen.append, lambda done, err: ends.append(done), is_async=True)
        assert isinstance(proxy, AsyncStreamProxy)
        async with proxy:
            return [item async for item in proxy]

    assert asyncio.run(main()) == [1, 2]
    assert seen == [1, 2]
    assert ends[0] is True
    assert inner.closed is True


def test_a_failing_capture_callback_never_breaks_iteration():
    def boom(_item):
        raise RuntimeError("capture bug")

    proxy = instrument_stream(_PlainStream([1, 2]), boom, boom, is_async=False)
    assert list(proxy) == [1, 2]


def test_stream_error_reaches_on_end_and_is_reraised():
    def failing():
        yield 1
        raise ValueError("mid-stream")

    ends = []
    proxy = StreamProxy(failing(), lambda item: None, lambda done, err: ends.append((done, err)))
    with pytest.raises(ValueError):
        list(proxy)
    assert ends[0][0] is False
    assert isinstance(ends[0][1], ValueError)


def test_unknown_provider_and_registry():
    assert set(providers.PROVIDERS) == {
        "openai", "anthropic", "google-genai", "bedrock", "aiobotocore"}
    with pytest.raises(ValueError, match="Unknown provider"):
        providers.instrument("nope")


@pytest.mark.parametrize("name,blocked", [
    ("openai", "openai"), ("anthropic", "anthropic"),
    ("google-genai", "google"), ("bedrock", "botocore"),
])
def test_missing_sdk_raises_import_error_and_patches_nothing(mocker, name, blocked):
    mocker.patch.dict("sys.modules", {blocked: None})
    with pytest.raises(ImportError, match="not installed"):
        providers.instrument(name)
