"""google-genai auto-instrumentation, driven through the SDK's own client.

The SDK accepts caller-supplied httpx clients through ``HttpOptions``
(``httpx_client`` / ``httpx_async_client``), so a mock transport is
injected there. Bodies are built from the SDK's response model."""

import asyncio
import json

import pytest

genai = pytest.importorskip("google.genai")

from google.genai import errors, types  # noqa: E402
from google.genai.models import AsyncModels, Models  # noqa: E402

from briefcase.auto import auto, undo  # noqa: E402

from tests.providers._helpers import (  # noqa: E402
    COMPLETION,
    PROMPT,
    assert_base_record,
    assert_no_content,
    sse,
    static,
)

try:
    import httpx2 as httpx
except ImportError:  # older google-genai releases are built on httpx
    import httpx

MODEL = "gemini-test"
SERVED_MODEL = "gemini-test-001"
DECISION_TYPE = "llm.google_genai.generate_content"


def _body(text, usage=True, finish="STOP"):
    response = types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            finish_reason=finish, index=0,
        )],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=8, candidates_token_count=4, total_token_count=14,
            thoughts_token_count=2,
        ) if usage else None,
        model_version=SERVED_MODEL,
        response_id="resp_1",
    )
    return json.loads(response.model_dump_json(exclude_none=True, by_alias=True))


RESPONSE_BODY = _body(COMPLETION)
STREAM_BODIES = [
    _body(COMPLETION[:6], usage=False, finish=None),
    _body(COMPLETION[6:]),
]
USAGE = {
    "prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 14, "thoughts_token_count": 2,
}


def _handler(status=200):
    def handler(request):
        if status != 200:
            return httpx.Response(status, json={"error": {
                "code": status, "message": f"quota {PROMPT}", "status": "RESOURCE_EXHAUSTED"}})
        if "streamGenerateContent" in str(request.url):
            return httpx.Response(
                200, content=sse([(None, body) for body in STREAM_BODIES]),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=RESPONSE_BODY)

    return handler


def _client(status=200):
    return genai.Client(api_key="k", http_options=types.HttpOptions(
        httpx_client=httpx.Client(transport=httpx.MockTransport(_handler(status))),
        httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler(status))),
        retry_options=types.HttpRetryOptions(attempts=1),
    ))


def test_generate_content_emits_one_record_without_content(instrument, mem):
    instrument("google-genai")
    response = _client().models.generate_content(
        model=MODEL, contents=PROMPT,
        config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=50),
    )

    assert response.text == COMPLETION
    assert len(mem.records) == 1
    record = mem.records[0]
    assert_base_record(record, DECISION_TYPE)
    assert_no_content(record)
    assert record["function_name"] == "google.genai.models.generate_content"
    assert record["status"] == "ok"
    assert record["stream"] is False
    assert record["outputs"] == {}
    assert record["model_parameters"] == {
        "provider": "google-genai", "model_name": SERVED_MODEL, "requested_model": MODEL,
        "temperature": 0.3, "max_output_tokens": 50,
    }
    assert record["token_usage"] == USAGE
    assert record["finish_reason"] == "STOP"
    assert record["response_id"] == "resp_1"


def test_content_is_recorded_when_enabled(instrument, mem):
    instrument("google-genai", capture_content=True)
    _client().models.generate_content(
        model=MODEL, contents=PROMPT,
        config={"system_instruction": "be brief"},
    )
    record = mem.records[0]
    assert record["inputs"] == {"contents": PROMPT, "system_instruction": "be brief"}
    assert record["outputs"] == {"content": COMPLETION}


def test_error_is_recorded_and_reraised(instrument, mem):
    instrument("google-genai")
    with pytest.raises(errors.APIError) as excinfo:
        _client(status=429).models.generate_content(model=MODEL, contents=PROMPT)

    assert len(mem.records) == 1
    record = mem.records[0]
    assert record["status"] == "error"
    error_name = excinfo.type.__name__
    assert record["error_type"] == error_name
    assert record["error"] == error_name
    assert record["model_parameters"]["model_name"] == MODEL
    assert_no_content(record)


def test_stream_emits_exactly_one_record_at_stream_end(instrument, mem):
    instrument("google-genai")
    stream = _client().models.generate_content_stream(model=MODEL, contents=PROMPT)

    assert mem.records == []
    chunks = []
    for chunk in stream:
        chunks.append(chunk.text)
        if len(chunks) < len(STREAM_BODIES):
            assert mem.records == []
    assert "".join(chunks) == COMPLETION

    assert len(mem.records) == 1
    record = mem.records[0]
    assert_base_record(record, DECISION_TYPE)
    assert_no_content(record)
    assert record["function_name"] == "google.genai.models.generate_content_stream"
    assert record["stream"] is True
    assert record["outputs"] == {"chunk_count": 2, "stream_completed": True}
    assert record["token_usage"] == USAGE
    assert record["finish_reason"] == "STOP"
    assert record["time_to_first_chunk_ms"] <= record["execution_time_ms"]


def test_stream_content_is_accumulated_when_enabled(instrument, mem):
    instrument("google-genai", capture_content=True)
    list(_client().models.generate_content_stream(model=MODEL, contents=PROMPT))
    assert mem.records[0]["outputs"]["content"] == COMPLETION


def test_stream_closed_early_emits_one_incomplete_record(instrument, mem):
    instrument("google-genai")
    stream = _client().models.generate_content_stream(model=MODEL, contents=PROMPT)
    next(stream)
    stream.close()

    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 1, "stream_completed": False}


def test_stream_error_is_recorded_and_reraised(instrument, mem):
    instrument("google-genai")
    stream = _client(status=429).models.generate_content_stream(model=MODEL, contents=PROMPT)
    with pytest.raises(errors.APIError):
        list(stream)
    assert [r["status"] for r in mem.records] == ["error"]
    assert mem.records[0]["outputs"] == {"chunk_count": 0, "stream_completed": False}


def test_async_paths(instrument, mem):
    instrument("google-genai")

    async def main():
        client = _client()
        response = await client.aio.models.generate_content(model=MODEL, contents=PROMPT)
        assert len(mem.records) == 1
        stream = await client.aio.models.generate_content_stream(model=MODEL, contents=PROMPT)
        assert len(mem.records) == 1
        return response, [chunk.text async for chunk in stream]

    response, pieces = asyncio.run(main())
    assert response.text == COMPLETION
    assert "".join(pieces) == COMPLETION
    assert len(mem.records) == 2
    assert [r["function_name"] for r in mem.records] == [
        "google.genai.aio.models.generate_content",
        "google.genai.aio.models.generate_content_stream",
    ]
    assert [r["token_usage"] for r in mem.records] == [USAGE, USAGE]
    assert mem.records[1]["outputs"] == {"chunk_count": 2, "stream_completed": True}
    for record in mem.records:
        assert_no_content(record)


def test_async_error_is_recorded_and_reraised(instrument, mem):
    instrument("google-genai")

    async def main():
        await _client(status=429).aio.models.generate_content(model=MODEL, contents=PROMPT)

    with pytest.raises(errors.APIError):
        asyncio.run(main())
    assert [r["status"] for r in mem.records] == ["error"]


def test_chat_sessions_are_covered(instrument, mem):
    instrument("google-genai")
    chat = _client().chats.create(model=MODEL)
    chat.send_message(PROMPT)
    assert len(mem.records) == 1


TARGETS = [
    (Models, "generate_content"), (Models, "generate_content_stream"),
    (AsyncModels, "generate_content"), (AsyncModels, "generate_content_stream"),
]


def test_undo_restores_the_original_methods(mem):
    originals = [static(owner, attr) for owner, attr in TARGETS]

    auto("google-genai", exporter=mem, async_capture=False)
    for (owner, attr), original in zip(TARGETS, originals):
        assert static(owner, attr) is not original

    undo("google-genai")
    for (owner, attr), original in zip(TARGETS, originals):
        assert static(owner, attr) is original
    _client().models.generate_content(model=MODEL, contents=PROMPT)
    assert mem.records == []


def test_double_auto_does_not_double_wrap(mem):
    original = static(Models, "generate_content")
    first = auto("google-genai", exporter=mem, async_capture=False)
    wrapped = static(Models, "generate_content")
    second = auto("google-genai", exporter=mem, async_capture=False)

    assert first is second
    assert static(Models, "generate_content") is wrapped
    assert wrapped.__wrapped__ is original
    _client().models.generate_content(model=MODEL, contents=PROMPT)
    assert len(mem.records) == 1
