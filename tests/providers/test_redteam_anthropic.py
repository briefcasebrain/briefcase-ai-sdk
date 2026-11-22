"""Red-team tests for the anthropic integration."""

import asyncio
import inspect
import logging

import pytest

anthropic = pytest.importorskip("anthropic")

from anthropic.resources.messages import AsyncMessages, Messages  # noqa: E402

from briefcase.integrations.providers import anthropic as bc_anthropic  # noqa: E402

from tests.providers import test_anthropic as base  # noqa: E402
from tests.providers._helpers import drain_gc, static  # noqa: E402

KWARGS = base.KWARGS


def test_signatures_are_preserved(instrument):
    names = ("create", "parse", "stream")
    before = {(cls, n): (static(cls, n), inspect.signature(static(cls, n)))
              for cls in (Messages, AsyncMessages) for n in names}
    instrument("anthropic")
    for (cls, name), (original, signature) in before.items():
        wrapped = static(cls, name)
        assert wrapped.__wrapped__ is original and inspect.signature(wrapped) == signature
        assert inspect.iscoroutinefunction(wrapped) == inspect.iscoroutinefunction(original)


def test_a_stream_abandoned_before_message_delta_has_no_output_count(instrument, mem):
    """``message_start`` carries a placeholder ``output_tokens``; reporting
    it as the completion size was wrong."""
    instrument("anthropic")
    stream = base._client().messages.create(stream=True, **KWARGS)
    for event in stream:
        if event.type == "content_block_delta":
            break
    stream.close()
    record = mem.records[0]
    assert record["status"] == "incomplete"
    assert record["token_usage"] == {
        "prompt_tokens": 9, "completion_tokens": None, "total_tokens": None,
        "cache_read_input_tokens": 2,
    }


def test_stream_helper_surface_is_fully_usable_and_recorded_once(instrument, mem):
    instrument("anthropic", capture_content=True)
    client = base._client()
    with client.messages.stream(**KWARGS) as stream:
        assert type(stream).__name__ == "MessageStream"
        assert stream.response.status_code == 200
        text = "".join(stream.text_stream)
        final = stream.get_final_message()
        stream.until_done()
    stream.close()
    assert text == base.COMPLETION and final.usage.output_tokens == 6
    assert len(mem.records) == 1
    record = mem.records[0]
    assert record["function_name"] == "anthropic.messages.stream"
    assert record["status"] == "ok" and record["token_usage"] == base.STREAM_USAGE
    assert record["outputs"]["content"] == base.COMPLETION
    assert record["request_id"] == "req_stream"


def test_stream_helper_exited_early_or_never_read(instrument, mem):
    instrument("anthropic")
    client = base._client()
    with client.messages.stream(**KWARGS) as stream:
        for event in stream:
            if event.type == "content_block_delta":
                break
    with client.messages.stream(**KWARGS):
        pass
    assert [r["status"] for r in mem.records] == ["incomplete", "incomplete"]
    assert mem.records[0]["token_usage"]["completion_tokens"] is None
    assert mem.records[1]["outputs"] == {"chunk_count": 0, "stream_completed": False}
    assert mem.records[1]["token_usage"] is None


def test_an_exception_in_the_with_body_propagates_and_one_record_is_emitted(instrument, mem):
    instrument("anthropic")
    error = ValueError("application bug")
    with pytest.raises(ValueError) as caught:
        with base._client().messages.stream(**KWARGS) as stream:
            next(iter(stream))
            raise error
    assert caught.value is error
    assert [r["status"] for r in mem.records] == ["incomplete"]


def test_stream_helper_request_error_is_the_sdks_own_exception(instrument, mem):
    instrument("anthropic")
    with pytest.raises(anthropic.APIStatusError):
        with base._client(status=529).messages.stream(**KWARGS):
            pass
    assert [(r["status"], r["http_status"]) for r in mem.records] == [("error", 529)]


def test_a_manager_that_is_never_entered_records_and_changes_nothing(instrument, mem):
    instrument("anthropic")
    manager = base._client().messages.stream(**KWARGS)
    assert type(manager).__name__ == "MessageStreamManager"
    del manager
    drain_gc()
    assert mem.records == []


def test_manually_entered_manager_that_is_dropped_still_reports(instrument, mem):
    instrument("anthropic")
    manager = base._client().messages.stream(**KWARGS)
    stream = manager.__enter__()
    next(stream)
    del stream, manager
    drain_gc()
    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 1, "stream_completed": False}


def test_stream_capture_does_not_depend_on_private_manager_attributes(instrument, mem, mocker):
    """The first version wrapped the manager's name-mangled ``__api_request``.
    Rename every private attribute of the manager: capture must not notice."""
    import anthropic.lib.streaming._messages as streaming

    original_init = streaming.MessageStreamManager.__init__
    original_enter = streaming.MessageStreamManager.__enter__
    original_exit = streaming.MessageStreamManager.__exit__

    def init(self, api_request, *, output_format):
        self._renamed_request = api_request
        self._renamed_format = output_format
        self._renamed_stream = None

    def enter(self):
        self._renamed_stream = streaming.MessageStream(
            self._renamed_request(), output_format=self._renamed_format)
        return self._renamed_stream

    def exit_(self, *exc):
        if self._renamed_stream is not None:
            self._renamed_stream.close()

    mocker.patch.object(streaming.MessageStreamManager, "__init__", init)
    mocker.patch.object(streaming.MessageStreamManager, "__enter__", enter)
    mocker.patch.object(streaming.MessageStreamManager, "__exit__", exit_)
    assert original_init and original_enter and original_exit
    instrument("anthropic")
    with base._client().messages.stream(**KWARGS) as stream:
        stream.until_done()
    assert len(mem.records) == 1 and mem.records[0]["token_usage"] == base.STREAM_USAGE


def test_fallback_to_the_public_snapshot_when_the_stream_cannot_be_observed(instrument, mem, mocker):
    import anthropic.lib.streaming._messages as streaming

    def refuse(self, name, value):
        if name == "_iterator" and "_iterator" in vars(self):
            raise AttributeError(name)
        object.__setattr__(self, name, value)

    mocker.patch.object(streaming.MessageStream, "__setattr__", refuse, create=True)
    instrument("anthropic", capture_content=True)
    with base._client().messages.stream(**KWARGS) as stream:
        assert type(stream) is streaming.MessageStream and "close" not in vars(stream)
        assert "".join(stream.text_stream) == base.COMPLETION
    assert len(mem.records) == 1
    record = mem.records[0]
    assert record["status"] == "ok"
    assert record["token_usage"]["completion_tokens"] == 6
    assert record["outputs"]["chunk_count"] is None
    assert "current_message_snapshot" in record["capture_limits"][0]

    mem.records.clear()
    with base._client().messages.stream(**KWARGS) as stream:
        for event in stream:
            if event.type == "content_block_delta":
                break
    assert mem.records[0]["status"] == "incomplete"
    assert mem.records[0]["token_usage"]["completion_tokens"] is None


def test_undo_restores_the_manager_classes_too(mem):
    import anthropic.lib.streaming._messages as streaming
    from briefcase.auto import auto, undo

    enter, exit_ = static(streaming.MessageStreamManager, "__enter__"), static(
        streaming.MessageStreamManager, "__exit__")
    aenter = static(streaming.AsyncMessageStreamManager, "__aenter__")
    auto("anthropic", exporter=mem, async_capture=False)
    client = base._client()
    with client.messages.stream(**KWARGS) as stream:
        stream.until_done()

    async def main():
        async with base._async_client().messages.stream(**KWARGS) as stream:
            await stream.until_done()

    asyncio.run(main())
    assert static(streaming.MessageStreamManager, "__enter__") is not enter
    undo("anthropic")
    assert static(streaming.MessageStreamManager, "__enter__") is enter
    assert static(streaming.MessageStreamManager, "__exit__") is exit_
    assert static(streaming.AsyncMessageStreamManager, "__aenter__") is aenter
    with client.messages.stream(**KWARGS) as stream:
        stream.until_done()
    assert len(mem.records) == 2


def test_async_raw_and_streaming_response_modes_have_usage(instrument, mem):
    instrument("anthropic")

    async def main():
        client = base._async_client()
        raw = await client.messages.with_raw_response.create(**KWARGS)
        assert type(raw).__name__ == "AsyncAPIResponse"
        message = await raw.parse()
        async with client.messages.with_streaming_response.create(**KWARGS) as response:
            parsed = await response.parse()
        async with client.messages.with_streaming_response.create(**KWARGS) as response:
            body = b"".join([chunk async for chunk in response.iter_bytes()])
        return message, parsed, body

    message, parsed, body = asyncio.run(main())
    assert message.id == parsed.id == "msg_1" and b"msg_1" in body
    assert [r["response_wrapper"] for r in mem.records] == [
        "with_raw_response", "with_streaming_response", "with_streaming_response"]
    assert [bool(r["token_usage"]) for r in mem.records] == [True, True, False]
    assert "capture_limits" in mem.records[2] and "capture_limits" not in mem.records[0]


def test_async_stream_cancelled_mid_read(instrument, mem):
    instrument("anthropic")

    async def main():
        client = base._async_client()

        async def consume():
            async with client.messages.stream(**KWARGS) as stream:
                async for _ in stream:
                    await asyncio.sleep(30)

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert len(mem.records) == 1
    assert mem.records[0]["status"] == "incomplete"
    assert mem.records[0]["outputs"]["chunk_count"] == 1


def test_a_missing_method_skips_that_surface_whole(mem, mocker, caplog):
    import anthropic.resources.beta.messages as beta
    from briefcase.auto import auto, status, undo

    create = static(beta.Messages, "create")
    mocker.patch.object(beta, "AsyncMessages", object())
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        inst = auto("anthropic", exporter=mem, async_capture=False)
    try:
        assert static(beta.Messages, "create") is create
        assert list(inst.skipped) == ["beta.messages"]
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        entry = status()["integrations"]["anthropic"]
        assert entry["state"] == "partial"
        assert entry["tested_versions"] == list(bc_anthropic.TESTED_VERSIONS)
        base._client().messages.create(**KWARGS)
        assert len(mem.records) == 1
    finally:
        undo("anthropic")
