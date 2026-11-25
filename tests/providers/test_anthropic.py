"""anthropic auto-instrumentation, driven through the SDK's own client with
a mock transport. Bodies are built from the SDK's response models."""

import asyncio
import json

import pytest

anthropic = pytest.importorskip("anthropic")

from anthropic.resources.messages import AsyncMessages, Messages  # noqa: E402
from anthropic.types import Message  # noqa: E402

from briefcase.auto import auto, undo  # noqa: E402

from tests.providers._helpers import (  # noqa: E402
    COMPLETION,
    PROMPT,
    assert_base_record,
    assert_no_content,
    httpx_for,
    sse,
    static,
)

MODEL = "claude-opus-5-5"
DECISION_TYPE = "llm.anthropic.messages"
KWARGS = dict(model=MODEL, max_tokens=64, messages=[{"role": "user", "content": PROMPT}])

MESSAGE_BODY = json.loads(Message.model_validate({
    "id": "msg_1", "type": "message", "role": "assistant", "model": MODEL,
    "content": [{"type": "text", "text": COMPLETION}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {
        "input_tokens": 12, "output_tokens": 5,
        "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1,
    },
}).model_dump_json(exclude_none=True))

STREAM_EVENTS = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_s", "type": "message", "role": "assistant", "model": MODEL, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 9, "output_tokens": 1, "cache_read_input_tokens": 2},
    }}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": COMPLETION[:4]}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": COMPLETION[4:]}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 6}}),
    ("message_stop", {"type": "message_stop"}),
]
STREAM_USAGE = {
    "prompt_tokens": 9, "completion_tokens": 6, "total_tokens": 15,
    "cache_read_input_tokens": 2,
}


def _transport(httpx, status=200):
    def handler(request):
        if status != 200:
            return httpx.Response(
                status, headers={"request-id": "req_err"},
                json={"type": "error", "error": {"type": "overloaded_error",
                                                 "message": f"overloaded {PROMPT}"}},
            )
        if json.loads(request.content).get("stream"):
            return httpx.Response(
                200, content=sse(STREAM_EVENTS),
                headers={"content-type": "text/event-stream", "request-id": "req_stream"},
            )
        return httpx.Response(200, json=MESSAGE_BODY, headers={"request-id": "req_msg"})

    return httpx.MockTransport(handler)


def _client(status=200):
    httpx = httpx_for(anthropic.Anthropic(api_key="k"))
    return anthropic.Anthropic(
        api_key="k", max_retries=0,
        http_client=httpx.Client(transport=_transport(httpx, status)),
    )


def _async_client(status=200):
    httpx = httpx_for(anthropic.Anthropic(api_key="k"))
    return anthropic.AsyncAnthropic(
        api_key="k", max_retries=0,
        http_client=httpx.AsyncClient(transport=_transport(httpx, status)),
    )


def test_create_emits_one_record_without_content(instrument, mem):
    instrument("anthropic")
    message = _client().messages.create(
        service_tier="auto", output_config={"effort": "low"}, **KWARGS)

    assert message.content[0].text == COMPLETION
    assert len(mem.records) == 1
    record = mem.records[0]
    assert_base_record(record, DECISION_TYPE)
    assert_no_content(record)
    assert record["function_name"] == "anthropic.messages.create"
    assert record["status"] == "ok"
    assert record["stream"] is False
    assert record["outputs"] == {}
    assert record["model_parameters"] == {
        "provider": "anthropic", "model_name": MODEL, "max_tokens": 64,
        "service_tier": "auto", "effort": "low",
    }
    assert record["token_usage"] == {
        "prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17,
        "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1,
    }
    assert record["finish_reason"] == "end_turn"
    assert record["response_id"] == "msg_1"
    assert record["request_id"] == "req_msg"


def test_content_is_recorded_when_enabled(instrument, mem):
    instrument("anthropic", capture_content=True)
    _client().messages.create(system="be brief", **KWARGS)

    record = mem.records[0]
    assert record["inputs"] == {"system": "be brief", "messages": KWARGS["messages"]}
    assert record["outputs"] == {"content": COMPLETION}


def test_async_create(instrument, mem):
    instrument("anthropic")

    async def main():
        return await _async_client().messages.create(**KWARGS)

    assert asyncio.run(main()).usage.output_tokens == 5
    assert len(mem.records) == 1
    assert mem.records[0]["token_usage"]["total_tokens"] == 17
    assert_no_content(mem.records[0])


def test_error_is_recorded_and_reraised(instrument, mem):
    instrument("anthropic")
    with pytest.raises(anthropic.APIStatusError) as excinfo:
        _client(status=529).messages.create(**KWARGS)

    assert len(mem.records) == 1
    record = mem.records[0]
    assert record["status"] == "error"
    error_name = excinfo.type.__name__
    assert record["error_type"] == error_name
    assert record["error"] == error_name
    assert record["http_status"] == 529
    assert record["request_id"] == "req_err"
    assert_no_content(record)


def test_async_error_is_recorded_and_reraised(instrument, mem):
    instrument("anthropic")

    async def main():
        await _async_client(status=529).messages.create(**KWARGS)

    with pytest.raises(anthropic.APIStatusError):
        asyncio.run(main())
    assert [r["status"] for r in mem.records] == ["error"]


def test_create_stream_emits_one_record_at_stream_end(instrument, mem):
    instrument("anthropic")
    stream = _client().messages.create(stream=True, **KWARGS)

    assert isinstance(stream, anthropic.Stream)
    assert mem.records == []
    kinds = [event.type for event in stream]
    assert kinds == [name for name, _ in STREAM_EVENTS]

    assert len(mem.records) == 1
    record = mem.records[0]
    assert_base_record(record, DECISION_TYPE)
    assert_no_content(record)
    assert record["stream"] is True
    assert record["outputs"] == {"chunk_count": len(STREAM_EVENTS), "stream_completed": True}
    assert record["token_usage"] == STREAM_USAGE
    assert record["finish_reason"] == "end_turn"
    assert record["response_id"] == "msg_s"
    assert record["request_id"] == "req_stream"
    assert record["model_parameters"]["model_name"] == MODEL


def test_stream_content_is_accumulated_when_enabled(instrument, mem):
    instrument("anthropic", capture_content=True)
    list(_client().messages.create(stream=True, **KWARGS))
    assert mem.records[0]["outputs"]["content"] == COMPLETION


def test_stream_closed_early_emits_one_incomplete_record(instrument, mem):
    instrument("anthropic")
    stream = _client().messages.create(stream=True, **KWARGS)
    next(stream)
    stream.close()
    stream.close()

    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 1, "stream_completed": False}
    assert mem.records[0]["token_usage"]["prompt_tokens"] == 9


def test_stream_helper_emits_one_record(instrument, mem):
    """messages.stream() bypasses create(): its manager is hooked instead."""
    instrument("anthropic")
    with _client().messages.stream(**KWARGS) as stream:
        assert mem.records == []
        final = stream.get_final_message()

    assert final.content[0].text == COMPLETION
    assert len(mem.records) == 1
    record = mem.records[0]
    assert record["function_name"] == "anthropic.messages.stream"
    assert record["token_usage"] == STREAM_USAGE
    assert record["outputs"]["stream_completed"] is True
    assert_no_content(record)


def test_stream_helper_never_entered_emits_nothing(instrument, mem):
    instrument("anthropic")
    _client().messages.stream(**KWARGS)
    assert mem.records == []


def test_async_stream_paths(instrument, mem):
    instrument("anthropic")

    async def main():
        client = _async_client()
        stream = await client.messages.create(stream=True, **KWARGS)
        assert isinstance(stream, anthropic.AsyncStream)
        events = [event async for event in stream]
        assert len(mem.records) == 1
        async with client.messages.stream(**KWARGS) as helper:
            text = "".join([piece async for piece in helper.text_stream])
        return events, text

    events, text = asyncio.run(main())
    assert len(events) == len(STREAM_EVENTS)
    assert text == COMPLETION
    assert len(mem.records) == 2
    assert [r["token_usage"] for r in mem.records] == [STREAM_USAGE, STREAM_USAGE]
    assert [r["function_name"] for r in mem.records] == [
        "anthropic.messages.create", "anthropic.messages.stream"]


def test_beta_messages_are_covered(instrument, mem):
    instrument("anthropic")
    _client().beta.messages.create(**KWARGS)
    assert len(mem.records) == 1
    assert mem.records[0]["function_name"] == "anthropic.beta.messages.create"
    assert mem.records[0]["token_usage"]["total_tokens"] == 17


def test_with_raw_response_is_recorded_once(instrument, mem):
    instrument("anthropic")
    raw = _client().messages.with_raw_response.create(**KWARGS)
    assert raw.parse().content[0].text == COMPLETION
    assert len(mem.records) == 1
    assert mem.records[0]["token_usage"]["total_tokens"] == 17


TARGETS = [
    (Messages, "create"), (Messages, "stream"), (Messages, "parse"),
    (AsyncMessages, "create"), (AsyncMessages, "stream"), (AsyncMessages, "parse"),
]


def test_undo_restores_the_original_methods(mem):
    originals = [static(owner, attr) for owner, attr in TARGETS]

    auto("anthropic", exporter=mem, async_capture=False)
    for (owner, attr), original in zip(TARGETS, originals):
        assert static(owner, attr) is not original

    undo("anthropic")
    for (owner, attr), original in zip(TARGETS, originals):
        assert static(owner, attr) is original
    _client().messages.create(**KWARGS)
    assert mem.records == []


def test_double_auto_does_not_double_wrap(mem):
    original = static(Messages, "create")
    first = auto("anthropic", exporter=mem, async_capture=False)
    wrapped = static(Messages, "create")
    second = auto("anthropic", exporter=mem, async_capture=False)

    assert first is second
    assert static(Messages, "create") is wrapped
    assert wrapped.__wrapped__ is original
    _client().messages.create(**KWARGS)
    assert len(mem.records) == 1
