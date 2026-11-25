"""Red-team tests for the google-genai integration."""

import asyncio
import inspect

import pytest

pytest.importorskip("google.genai")

from google.genai import errors  # noqa: E402
from google.genai.models import AsyncModels, Models  # noqa: E402

from tests.providers import test_google_genai as base  # noqa: E402
from tests.providers._helpers import drain_gc, static  # noqa: E402

MODEL, PROMPT = base.MODEL, base.PROMPT


def test_the_stream_method_is_still_a_generator_function(instrument):
    kinds = {
        (cls, name): (inspect.isgeneratorfunction(static(cls, name)),
                      inspect.iscoroutinefunction(static(cls, name)),
                      inspect.signature(static(cls, name)))
        for cls in (Models, AsyncModels)
        for name in ("generate_content", "generate_content_stream")
    }
    instrument("google-genai")
    for (cls, name), (is_generator, is_coroutine, signature) in kinds.items():
        wrapped = static(cls, name)
        assert inspect.isgeneratorfunction(wrapped) == is_generator
        assert inspect.iscoroutinefunction(wrapped) == is_coroutine
        assert inspect.signature(wrapped) == signature


def test_a_stream_that_is_never_iterated_sent_nothing_and_records_nothing(instrument, mem):
    """The original is a generator function: no request until ``next()``.
    The first version recorded an ``ok`` call for a request never made."""
    instrument("google-genai")
    stream = base._client().models.generate_content_stream(model=MODEL, contents=PROMPT)
    assert inspect.isgenerator(stream)
    del stream
    drain_gc()
    assert mem.records == []


def test_errors_carry_the_http_status(instrument, mem):
    instrument("google-genai")
    with pytest.raises(errors.APIError):
        base._client(status=429).models.generate_content(model=MODEL, contents=PROMPT)
    with pytest.raises(errors.APIError):
        list(base._client(status=503).models.generate_content_stream(model=MODEL, contents=PROMPT))
    assert [(r["status"], r["http_status"]) for r in mem.records] == [("error", 429), ("error", 503)]


def test_stream_closed_early_is_incomplete_with_unknown_usage(instrument, mem):
    instrument("google-genai")
    stream = base._client().models.generate_content_stream(model=MODEL, contents=PROMPT)
    next(stream)
    stream.close()
    assert mem.records[0]["status"] == "incomplete"
    assert mem.records[0]["token_usage"] is None


def test_async_stream_is_an_async_generator_and_abandonment_reports(instrument, mem):
    instrument("google-genai")

    async def main():
        stream = await base._client().aio.models.generate_content_stream(
            model=MODEL, contents=PROMPT)
        assert inspect.isasyncgen(stream)
        async for _ in stream:
            break
        await stream.aclose()

    asyncio.run(main())
    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 1, "stream_completed": False}


def test_a_changed_signature_patches_nothing_at_all(mem, mocker):
    from briefcase.integrations import providers

    def generate_content(self, *, model, prompt):  # ``contents`` renamed
        return None

    stream = static(Models, "generate_content_stream")
    mocker.patch.object(AsyncModels, "generate_content", generate_content)
    with pytest.raises(RuntimeError, match="nothing was instrumented") as caught:
        providers.instrument("google-genai", exporter=mem)
    assert "signature changed" in str(caught.value)
    assert static(Models, "generate_content_stream") is stream
