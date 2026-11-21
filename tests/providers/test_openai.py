"""openai auto-instrumentation, driven through the SDK's own client with a
mock transport. Bodies are built from the SDK's response models."""

import asyncio
import json
import logging

import pytest

openai = pytest.importorskip("openai")

from openai.resources.chat.completions import AsyncCompletions, Completions  # noqa: E402
from openai.resources.responses import Responses  # noqa: E402
from openai.types import CreateEmbeddingResponse  # noqa: E402
from openai.types.chat import ChatCompletion, ChatCompletionChunk  # noqa: E402
from openai.types.responses import Response, ResponseCompletedEvent  # noqa: E402

from briefcase.auto import auto, undo  # noqa: E402
from briefcase.integrations import providers  # noqa: E402

from tests.providers._helpers import (  # noqa: E402
    COMPLETION,
    PROMPT,
    assert_base_record,
    assert_no_content,
    httpx_for,
    sse,
    static,
)

MODEL = "gpt-test"
SERVED_MODEL = "gpt-test-2026-01-01"
MESSAGES = [{"role": "user", "content": PROMPT}]


def _dump(model):
    return json.loads(model.model_dump_json(exclude_none=True))


CHAT_BODY = _dump(ChatCompletion.model_validate({
    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": SERVED_MODEL,
    "choices": [{
        "index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": COMPLETION},
    }],
    "usage": {
        "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 2},
    },
}))


def _chunk(**fields):
    base = {"id": "chatcmpl-s", "object": "chat.completion.chunk", "created": 1,
            "model": SERVED_MODEL}
    return _dump(ChatCompletionChunk.model_validate({**base, **fields}))


CHAT_CHUNKS = [
    _chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": COMPLETION[:5]}}]),
    _chunk(choices=[{"index": 0, "delta": {"content": COMPLETION[5:]}, "finish_reason": "stop"}]),
    _chunk(choices=[], usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
]

RESPONSE_OBJ = Response.model_validate({
    "id": "resp_1", "object": "response", "created_at": 1, "model": SERVED_MODEL,
    "status": "completed", "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
    "output": [{
        "id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": COMPLETION, "annotations": []}],
    }],
    "usage": {
        "input_tokens": 9, "output_tokens": 6, "total_tokens": 15,
        "input_tokens_details": {"cached_tokens": 1, "cache_write_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 3},
    },
})
RESPONSE_BODY = _dump(RESPONSE_OBJ)
RESPONSE_EVENTS = [
    {"type": "response.output_text.delta", "delta": COMPLETION, "item_id": "msg_1",
     "output_index": 0, "content_index": 0, "sequence_number": 1, "logprobs": []},
    _dump(ResponseCompletedEvent(
        response=RESPONSE_OBJ, sequence_number=2, type="response.completed")),
]

EMBEDDING_BODY = _dump(CreateEmbeddingResponse.model_validate({
    "object": "list", "model": "embed-test",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
    "usage": {"prompt_tokens": 5, "total_tokens": 5},
}))


def _transport(httpx, status=200):
    def handler(request):
        if status != 200:
            return httpx.Response(
                status, json={"error": {"message": f"bad {PROMPT}", "type": "rate_limit"}},
                headers={"x-request-id": "req_err"},
            )
        body = json.loads(request.content)
        path = request.url.path
        stream_headers = {"content-type": "text/event-stream", "x-request-id": "req_stream"}
        if path.endswith("/chat/completions"):
            if body.get("stream"):
                return httpx.Response(
                    200, headers=stream_headers,
                    content=sse([(None, c) for c in CHAT_CHUNKS], done=True),
                )
            return httpx.Response(200, json=CHAT_BODY, headers={"x-request-id": "req_chat"})
        if path.endswith("/responses"):
            if body.get("stream"):
                return httpx.Response(
                    200, headers=stream_headers,
                    content=sse([(e["type"], e) for e in RESPONSE_EVENTS]),
                )
            return httpx.Response(200, json=RESPONSE_BODY, headers={"x-request-id": "req_resp"})
        if path.endswith("/embeddings"):
            return httpx.Response(200, json=EMBEDDING_BODY, headers={"x-request-id": "req_emb"})
        return httpx.Response(404, json={"error": {"message": "no route"}})

    return httpx.MockTransport(handler)


def _client(status=200):
    httpx = httpx_for(openai.OpenAI(api_key="k"))
    return openai.OpenAI(
        api_key="k", max_retries=0,
        http_client=httpx.Client(transport=_transport(httpx, status)),
    )


def _async_client(status=200):
    httpx = httpx_for(openai.OpenAI(api_key="k"))
    return openai.AsyncOpenAI(
        api_key="k", max_retries=0,
        http_client=httpx.AsyncClient(transport=_transport(httpx, status)),
    )


# -- chat completions -------------------------------------------------------

def test_chat_emits_one_record_without_content(instrument, mem):
    instrument("openai")
    result = _client().chat.completions.create(model=MODEL, messages=MESSAGES, temperature=0.2)

    assert isinstance(result, ChatCompletion)
    assert result.choices[0].message.content == COMPLETION
    assert len(mem.records) == 1
    record = mem.records[0]
    assert_base_record(record, "llm.openai.chat")
    assert_no_content(record)
    assert record["function_name"] == "openai.chat.completions.create"
    assert record["status"] == "ok"
    assert record["stream"] is False
    assert record["outputs"] == {}
    assert record["model_parameters"] == {
        "provider": "openai", "model_name": SERVED_MODEL,
        "requested_model": MODEL, "temperature": 0.2,
    }
    assert record["token_usage"] == {
        "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
        "cached_tokens": 4, "reasoning_tokens": 2,
    }
    assert record["finish_reason"] == "stop"
    assert record["response_id"] == "chatcmpl-1"
    assert record["request_id"] == "req_chat"
    assert "error" not in record


def test_chat_content_is_recorded_when_enabled(instrument, mem):
    instrument("openai", capture_content=True)
    _client().chat.completions.create(model=MODEL, messages=MESSAGES)

    record = mem.records[0]
    assert record["inputs"] == {"messages": MESSAGES}
    assert record["outputs"] == {"content": COMPLETION}


def test_hash_mode_records_digests_not_content(instrument, mem):
    instrument("openai", capture_content="hash")
    _client().chat.completions.create(model=MODEL, messages=MESSAGES)

    record = mem.records[0]
    blob = json.dumps(record)
    assert PROMPT not in blob and COMPLETION not in blob
    assert set(record["inputs"]) == {"sha256", "chars"}
    assert set(record["outputs"]) == {"sha256", "chars"}


def test_redact_hook_rewrites_captured_content(instrument, mem):
    instrument("openai", capture_content=True, redact=lambda text: text.replace("SECRET", "***"))
    _client().chat.completions.create(model=MODEL, messages=MESSAGES)

    blob = json.dumps(mem.records[0])
    assert "SECRET" not in blob
    assert "PROMPT-***" in blob and "COMPLETION-***" in blob


def test_async_chat(instrument, mem):
    instrument("openai")

    async def main():
        return await _async_client().chat.completions.create(model=MODEL, messages=MESSAGES)

    result = asyncio.run(main())
    assert result.usage.total_tokens == 18
    assert len(mem.records) == 1
    assert mem.records[0]["token_usage"]["total_tokens"] == 18
    assert_no_content(mem.records[0])


def test_error_is_recorded_and_reraised(instrument, mem):
    instrument("openai")
    with pytest.raises(openai.RateLimitError):
        _client(status=429).chat.completions.create(model=MODEL, messages=MESSAGES)

    assert len(mem.records) == 1
    record = mem.records[0]
    assert record["status"] == "error"
    assert record["error_type"] == "RateLimitError"
    # Content capture is off: the class name, never the (prompt-bearing) message.
    assert record["error"] == "RateLimitError"
    assert record["http_status"] == 429
    assert record["request_id"] == "req_err"
    assert record["model_parameters"]["model_name"] == MODEL
    assert record["execution_time_ms"] >= 0
    assert_no_content(record)


def test_error_message_is_recorded_with_content_capture(instrument, mem):
    instrument("openai", capture_content=True)
    with pytest.raises(openai.RateLimitError):
        _client(status=429).chat.completions.create(model=MODEL, messages=MESSAGES)
    assert "bad" in mem.records[0]["error"]


def test_async_error_is_recorded_and_reraised(instrument, mem):
    instrument("openai")

    async def main():
        await _async_client(status=429).chat.completions.create(model=MODEL, messages=MESSAGES)

    with pytest.raises(openai.RateLimitError):
        asyncio.run(main())
    assert [r["status"] for r in mem.records] == ["error"]


# -- streaming --------------------------------------------------------------

def test_stream_emits_exactly_one_record_at_stream_end(instrument, mem):
    instrument("openai")
    stream = _client().chat.completions.create(model=MODEL, messages=MESSAGES, stream=True)

    assert isinstance(stream, openai.Stream)  # the SDK's own object, instrumented in place
    assert mem.records == []
    text = ""
    for seen, chunk in enumerate(stream, 1):
        if chunk.choices and chunk.choices[0].delta.content:
            text += chunk.choices[0].delta.content
        if seen < len(CHAT_CHUNKS):
            assert mem.records == []
    assert text == COMPLETION

    assert len(mem.records) == 1
    record = mem.records[0]
    assert_base_record(record, "llm.openai.chat")
    assert_no_content(record)
    assert record["stream"] is True
    assert record["outputs"] == {"chunk_count": 3, "stream_completed": True}
    assert record["token_usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert record["finish_reason"] == "stop"
    assert record["request_id"] == "req_stream"
    assert record["time_to_first_chunk_ms"] <= record["execution_time_ms"]

    stream.close()  # closing after the end adds nothing
    assert len(mem.records) == 1


def test_stream_content_is_accumulated_when_enabled(instrument, mem):
    instrument("openai", capture_content=True)
    for _ in _client().chat.completions.create(model=MODEL, messages=MESSAGES, stream=True):
        pass
    assert mem.records[0]["outputs"]["content"] == COMPLETION


def test_stream_closed_early_emits_one_incomplete_record(instrument, mem):
    instrument("openai")
    with _client().chat.completions.create(model=MODEL, messages=MESSAGES, stream=True) as stream:
        next(stream)
        assert mem.records == []

    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 1, "stream_completed": False}
    assert mem.records[0]["status"] == "ok"


def test_stream_dropped_unread_still_emits_one_record(instrument, mem):
    instrument("openai")
    stream = _client().chat.completions.create(model=MODEL, messages=MESSAGES, stream=True)
    del stream
    import gc

    gc.collect()
    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 0, "stream_completed": False}


def test_async_stream(instrument, mem):
    instrument("openai")

    async def main():
        stream = await _async_client().chat.completions.create(
            model=MODEL, messages=MESSAGES, stream=True)
        assert isinstance(stream, openai.AsyncStream)
        return [chunk async for chunk in stream]

    assert len(asyncio.run(main())) == 3
    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 3, "stream_completed": True}
    assert mem.records[0]["token_usage"]["total_tokens"] == 5


def test_stream_helper_is_recorded_once(instrument, mem):
    """chat.completions.stream() calls create(stream=True) underneath."""
    instrument("openai")
    with _client().chat.completions.stream(model=MODEL, messages=MESSAGES) as stream:
        for _ in stream:
            pass
    assert len(mem.records) == 1
    assert mem.records[0]["stream"] is True


def test_stream_usage_option_requests_usage(instrument, mem):
    inst = instrument("openai", stream_usage=True)
    assert inst.patched
    seen = {}
    httpx = httpx_for(openai.OpenAI(api_key="k"))

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=sse([(None, c) for c in CHAT_CHUNKS], done=True))

    client = openai.OpenAI(api_key="k", http_client=httpx.Client(
        transport=httpx.MockTransport(handler)))
    list(client.chat.completions.create(model=MODEL, messages=MESSAGES, stream=True))
    assert seen["stream_options"] == {"include_usage": True}


def test_stream_options_are_not_touched_by_default(instrument, mem):
    instrument("openai")
    seen = {}
    httpx = httpx_for(openai.OpenAI(api_key="k"))

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=sse([(None, c) for c in CHAT_CHUNKS], done=True))

    client = openai.OpenAI(api_key="k", http_client=httpx.Client(
        transport=httpx.MockTransport(handler)))
    list(client.chat.completions.create(model=MODEL, messages=MESSAGES, stream=True))
    assert "stream_options" not in seen


# -- other surfaces ---------------------------------------------------------

def test_with_raw_response_is_recorded_with_usage(instrument, mem):
    instrument("openai")
    raw = _client().chat.completions.with_raw_response.create(model=MODEL, messages=MESSAGES)

    assert raw.parse().choices[0].message.content == COMPLETION
    assert len(mem.records) == 1
    assert mem.records[0]["token_usage"]["total_tokens"] == 18


def test_with_streaming_response_leaves_the_body_to_the_caller(instrument, mem):
    instrument("openai")
    with _client().chat.completions.with_streaming_response.create(
            model=MODEL, messages=MESSAGES) as response:
        assert response.parse().choices[0].message.content == COMPLETION
    assert len(mem.records) == 1
    assert "token_usage" not in mem.records[0]
    assert mem.records[0]["request_id"] == "req_chat"


def test_responses_create(instrument, mem):
    instrument("openai", capture_content=True)
    result = _client().responses.create(model=MODEL, input=PROMPT, instructions="be brief")

    assert result.output_text == COMPLETION
    record = mem.records[0]
    assert_base_record(record, "llm.openai.responses")
    assert record["function_name"] == "openai.responses.create"
    assert record["token_usage"] == {
        "prompt_tokens": 9, "completion_tokens": 6, "total_tokens": 15,
        "cached_tokens": 1, "reasoning_tokens": 3,
    }
    assert record["finish_reason"] == "completed"
    assert record["response_id"] == "resp_1"
    assert record["inputs"] == {"instructions": "be brief", "input": PROMPT}
    assert record["outputs"] == {"content": COMPLETION}


def test_responses_stream_takes_usage_from_the_completed_event(instrument, mem):
    instrument("openai")
    events = list(_client().responses.create(model=MODEL, input=PROMPT, stream=True))

    assert [e.type for e in events] == ["response.output_text.delta", "response.completed"]
    assert len(mem.records) == 1
    record = mem.records[0]
    assert_no_content(record)
    assert record["token_usage"]["total_tokens"] == 15
    assert record["finish_reason"] == "completed"
    assert record["response_id"] == "resp_1"
    assert record["request_id"] == "req_stream"
    assert record["outputs"] == {"chunk_count": 2, "stream_completed": True}


def test_embeddings(instrument, mem):
    instrument("openai", capture_content=True)
    _client().embeddings.create(model="embed-test", input=PROMPT)

    record = mem.records[0]
    assert_base_record(record, "llm.openai.embeddings")
    assert record["token_usage"] == {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}
    assert record["embedding_count"] == 1
    assert record["inputs"] == {"input": PROMPT}
    assert record["outputs"] == {}


def test_no_exporter_means_no_capture(instrument):
    from briefcase.config import setup

    setup(exporter=None)
    instrument("openai", exporter=None)
    result = _client().chat.completions.create(model=MODEL, messages=MESSAGES)
    assert result.choices[0].message.content == COMPLETION


# -- patch lifecycle --------------------------------------------------------

TARGETS = [
    (Completions, "create"), (Completions, "parse"),
    (AsyncCompletions, "create"), (AsyncCompletions, "parse"),
    (Responses, "create"), (Responses, "parse"),
]


def test_undo_restores_the_original_methods(mem):
    originals = [static(owner, attr) for owner, attr in TARGETS]

    handler = auto("openai", exporter=mem, async_capture=False)
    for (owner, attr), original in zip(TARGETS, originals):
        assert static(owner, attr) is not original
        assert static(owner, attr).__wrapped__ is original
    assert handler.sdk_version == openai.__version__

    undo("openai")
    for (owner, attr), original in zip(TARGETS, originals):
        assert static(owner, attr) is original

    _client().chat.completions.create(model=MODEL, messages=MESSAGES)
    assert mem.records == []


def test_double_auto_does_not_double_wrap(mem):
    original = static(Completions, "create")
    first = auto("openai", exporter=mem, async_capture=False)
    wrapped = static(Completions, "create")
    second = auto("openai", exporter=mem, async_capture=False)

    assert first is second
    assert static(Completions, "create") is wrapped
    assert wrapped.__wrapped__ is original

    _client().chat.completions.create(model=MODEL, messages=MESSAGES)
    assert len(mem.records) == 1


def test_second_instrumentor_refuses_to_wrap_again(instrument, mem, caplog):
    instrument("openai")
    wrapped = static(Completions, "create")
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        with pytest.raises(RuntimeError, match="nothing was instrumented"):
            providers.instrument("openai", exporter=mem)
    assert static(Completions, "create") is wrapped


def test_missing_patch_target_is_skipped_with_a_warning(mem, mocker):
    from briefcase.integrations.providers import _base

    warning = mocker.patch.object(_base.logger, "warning")
    inst = _base.ProviderInstrumentor("openai", exporter=mem)
    try:
        assert inst.patch("openai.resources.chat.completions", "Completions", "no_such_method",
                          lambda original: original) is False
        assert inst.patch("openai.resources.no_such_module", "Completions", "create",
                          lambda original: original) is False
        assert inst.patched == []
        assert len(inst.skipped) == 2
        assert warning.call_count == 2
        assert "not found in the installed SDK" in warning.call_args[0][0]
    finally:
        inst.uninstall()
