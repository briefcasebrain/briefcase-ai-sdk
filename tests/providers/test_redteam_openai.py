"""Red-team tests for the openai integration, through the SDK's own client
and a mock transport."""

import asyncio
import inspect
import json
import logging

import pytest

openai = pytest.importorskip("openai")

from openai.resources.chat.completions import AsyncCompletions, Completions  # noqa: E402
from openai.resources.embeddings import Embeddings  # noqa: E402

import briefcase.auto as auto_mod  # noqa: E402
from briefcase.auto import auto, status, undo  # noqa: E402
from briefcase.integrations.providers import openai as bc_openai  # noqa: E402

from tests.providers import test_openai as base  # noqa: E402
from tests.providers._helpers import drain_gc, httpx_for, sse, static  # noqa: E402

MODEL, MESSAGES = base.MODEL, base.MESSAGES
NO_USAGE_CHUNKS = base.CHAT_CHUNKS[:2]


def _httpx():
    return httpx_for(openai.OpenAI(api_key="k"))


def _client_for(handler, base_url=None, is_async=False):
    httpx = _httpx()
    kwargs = {"api_key": "k", "max_retries": 0}
    if base_url:
        kwargs["base_url"] = base_url
    if is_async:
        return openai.AsyncOpenAI(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)
    return openai.OpenAI(http_client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)


def _usage_aware(seen):
    """Like the API: the usage chunk (and ``usage: null`` on the others)
    only when the request asks for it."""
    httpx = _httpx()

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if (body.get("stream_options") or {}).get("include_usage"):
            chunks = [dict(c, usage=None) for c in NO_USAGE_CHUNKS] + [base.CHAT_CHUNKS[2]]
        else:
            chunks = NO_USAGE_CHUNKS
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse([(None, c) for c in chunks], done=True))
    return handler


def _json_handler(body, status=200):
    httpx = _httpx()
    return lambda request: httpx.Response(status, json=body, headers={"x-request-id": "req_x"})


# -- transparency -------------------------------------------------------------

def test_signatures_docs_and_coroutine_ness_are_preserved(instrument):
    originals = {
        (cls, name): static(cls, name)
        for cls in (Completions, AsyncCompletions) for name in ("create", "parse")
    }
    signatures = {key: inspect.signature(fn) for key, fn in originals.items()}
    instrument("openai")
    for (cls, name), original in originals.items():
        wrapped = static(cls, name)
        assert wrapped is not original and wrapped.__wrapped__ is original
        assert inspect.signature(wrapped) == signatures[(cls, name)]
        assert wrapped.__doc__ == original.__doc__ and wrapped.__name__ == original.__name__
        assert inspect.iscoroutinefunction(wrapped) == inspect.iscoroutinefunction(original)


def test_argument_errors_are_raised_at_the_call_like_the_sdk_does(instrument, mem):
    """The SDK's async ``create`` validates its arguments when called, before
    anything is awaited; the wrapper must not move that to the await."""
    client = base._async_client()
    with pytest.raises(TypeError) as plain:
        client.chat.completions.create()
    instrument("openai")
    with pytest.raises(TypeError) as wrapped:
        client.chat.completions.create()
    assert str(wrapped.value) == str(plain.value)


def test_break_then_garbage_collection_reports_what_was_read(instrument, mem):
    instrument("openai")
    stream = base._client().chat.completions.create(model=MODEL, messages=MESSAGES, stream=True)
    for _ in stream:
        break
    assert mem.records == []
    del stream
    drain_gc()
    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 1, "stream_completed": False}
    assert mem.records[0]["status"] == "incomplete"


def test_stream_helpers_and_double_close_emit_one_record(instrument, mem):
    instrument("openai")
    client = base._client()
    with client.chat.completions.stream(model=MODEL, messages=MESSAGES) as stream:
        stream.until_done()
        final = stream.get_final_completion()
    assert final.choices[0].message.content == base.COMPLETION
    stream.close()
    raw = client.chat.completions.create(model=MODEL, messages=MESSAGES, stream=True)
    assert isinstance(raw, openai.Stream) and raw.response.status_code == 200
    next(raw)
    raw.close()
    raw.close()
    assert [r["status"] for r in mem.records] == ["ok", "incomplete"]


def test_async_with_raw_response_is_recorded_with_usage(instrument, mem):
    instrument("openai")

    async def main():
        raw = await base._async_client().chat.completions.with_raw_response.create(
            model=MODEL, messages=MESSAGES)
        return raw.parse()

    assert asyncio.run(main()).usage.total_tokens == 18
    assert len(mem.records) == 1
    assert mem.records[0]["token_usage"]["total_tokens"] == 18
    assert mem.records[0]["response_wrapper"] == "with_raw_response"


def test_with_streaming_response_read_as_bytes_says_usage_is_unknown(instrument, mem):
    instrument("openai")
    with base._client().chat.completions.with_streaming_response.create(
            model=MODEL, messages=MESSAGES) as response:
        assert mem.records == []  # not before the caller is done with the body
        body = b"".join(response.iter_bytes())
    assert json.loads(body)["id"] == "chatcmpl-1"  # the caller got every byte
    assert len(mem.records) == 1
    record = mem.records[0]
    assert record["token_usage"] is None and record["status"] == "ok"
    assert "with_streaming_response" in record["capture_limits"][0]


def test_async_with_streaming_response_is_recorded_from_parse(instrument, mem):
    instrument("openai")

    async def main():
        client = base._async_client()
        async with client.chat.completions.with_streaming_response.create(
                model=MODEL, messages=MESSAGES) as response:
            return await response.parse()

    assert asyncio.run(main()).id == "chatcmpl-1"
    assert [r["token_usage"]["total_tokens"] for r in mem.records] == [18]


def test_with_streaming_response_stream_is_instrumented_through_parse(instrument, mem):
    instrument("openai")
    with base._client().chat.completions.with_streaming_response.create(
            model=MODEL, messages=MESSAGES, stream=True) as response:
        stream = response.parse()
        assert isinstance(stream, openai.Stream)
        assert len(list(stream)) == 3
    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 3, "stream_completed": True}
    assert mem.records[0]["token_usage"]["total_tokens"] == 5


def test_a_stream_without_the_private_iterator_is_returned_untouched(instrument, mem, mocker):
    instrument("openai")
    client = base._client()
    stream_cls = type(client.chat.completions.create(model=MODEL, messages=MESSAGES, stream=True))
    mem.records.clear()

    # A future SDK that renamed ``_iterator``: assignment is refused.
    def refuse(self, name, value):
        if name == "_iterator" and "_iterator" in vars(self):
            raise AttributeError(name)
        object.__setattr__(self, name, value)

    mocker.patch.object(stream_cls, "__setattr__", refuse, create=True)
    stream = client.chat.completions.create(model=MODEL, messages=MESSAGES, stream=True)
    assert type(stream) is stream_cls and "close" not in vars(stream)
    assert len(list(stream)) == 3
    assert len(mem.records) == 1
    assert mem.records[0]["token_usage"] is None
    assert "not observed" in mem.records[0]["capture_limits"][0]


# -- exceptions ---------------------------------------------------------------

def test_keyboard_interrupt_mid_stream_is_one_cancelled_record(instrument, mem):
    instrument("openai")
    stream = base._client().chat.completions.create(model=MODEL, messages=MESSAGES, stream=True)
    interrupt = KeyboardInterrupt()
    next(stream)
    with pytest.raises(KeyboardInterrupt) as caught:
        stream._iterator.throw(interrupt)
    assert caught.value is interrupt
    assert len(mem.records) == 1
    assert mem.records[0]["status"] == "cancelled"
    assert mem.records[0]["error_type"] == "KeyboardInterrupt"
    assert mem.records[0]["outputs"]["chunk_count"] == 1


def test_async_cancellation_is_recorded_as_cancelled_and_reraised(instrument, mem):
    instrument("openai")

    async def slow(request):
        await asyncio.sleep(30)

    async def main():
        client = _client_for(slow, is_async=True)
        task = asyncio.ensure_future(
            client.chat.completions.create(model=MODEL, messages=MESSAGES))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert [(r["status"], r["error_type"]) for r in mem.records] == [("cancelled", "CancelledError")]


@pytest.mark.parametrize("mutation", [
    {"usage": "garbage"}, {"usage": {"prompt_tokens": "many"}}, {"choices": None},
    {"choices": [], "usage": None}, {"model": None, "id": None},
])
def test_unexpected_response_fields_never_break_the_call(instrument, mem, mutation):
    instrument("openai", capture_content=True)
    client = _client_for(_json_handler(dict(base.CHAT_BODY, **mutation)))
    result = client.chat.completions.create(model=MODEL, messages=MESSAGES)
    assert result is not None
    assert len(mem.records) == 1 and mem.records[0]["status"] == "ok"
    usage = mem.records[0]["token_usage"]
    assert usage is None or all(v is None or isinstance(v, int) for v in usage.values())


# -- data ---------------------------------------------------------------------

def test_nothing_sensitive_is_recorded_without_content_capture(instrument, mem):
    instrument("openai")
    client = _client_for(_json_handler(base.CHAT_BODY))
    client.api_key = "sk-SECRET-KEY"
    client.chat.completions.create(
        model=MODEL, messages=MESSAGES, user="user-4711@example.com",
        metadata={"customer": "ACME-SECRET"},
        tools=[{"type": "function", "function": {"name": "lookup_SECRET", "parameters": {}}}],
        extra_headers={"Authorization": "Bearer sk-HEADER-SECRET"},
        extra_body={"internal": "BODY-SECRET"},
    )
    blob = json.dumps(mem.records[0], default=str)
    for secret in ("SECRET", "user-4711", "sk-", base.PROMPT, base.COMPLETION):
        assert secret not in blob


def test_multiple_choices_tool_calls_refusal_and_audio_tokens(instrument, mem):
    instrument("openai")
    body = dict(base.CHAT_BODY)
    body["choices"] = [
        {"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "refusal": "no",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "f", "arguments": "{\"secret\": 1}"}}]}},
        {"index": 1, "finish_reason": "content_filter",
         "message": {"role": "assistant", "content": "x"}},
    ]
    body["usage"] = {
        "prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
        "prompt_tokens_details": {"cached_tokens": 2, "audio_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 1, "audio_tokens": 2,
                                      "accepted_prediction_tokens": 5,
                                      "rejected_prediction_tokens": 6},
    }
    _client_for(_json_handler(body)).chat.completions.create(model=MODEL, messages=MESSAGES, n=2)
    record = mem.records[0]
    assert record["finish_reason"] == "tool_calls"
    assert record["finish_reasons"] == ["tool_calls", "content_filter"]
    assert record["choice_count"] == 2 and record["tool_call_count"] == 1
    assert record["refusal"] is True
    assert record["token_usage"] == {
        "prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "cached_tokens": 2,
        "audio_prompt_tokens": 3, "reasoning_tokens": 1, "audio_completion_tokens": 2,
        "accepted_prediction_tokens": 5, "rejected_prediction_tokens": 6,
    }
    assert "secret" not in json.dumps(record)  # tool arguments are content


def test_content_capture_is_bounded_and_json_safe(instrument, mem):
    instrument("openai", capture_content=True, max_content_chars=50)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "A" * 5000},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "B" * 100000}},
    ]}] * 40
    _client_for(_json_handler(base.CHAT_BODY)).chat.completions.create(
        model=MODEL, messages=messages)
    blob = json.dumps(mem.records[0])
    assert len(blob) < 5000
    assert "A" * 51 not in blob and "B" * 51 not in blob


# -- streaming usage ----------------------------------------------------------

def test_a_stream_without_usage_says_so_instead_of_reporting_zero(instrument, mem):
    instrument("openai")
    seen = []
    client = _client_for(_usage_aware(seen))
    chunks = list(client.chat.completions.create(model=MODEL, messages=MESSAGES, stream=True))
    assert "stream_options" not in seen[0]  # nothing was added to the request
    assert [len(c.choices) for c in chunks] == [1, 1]
    record = mem.records[0]
    assert record["status"] == "ok" and record["token_usage"] is None
    assert "include_usage" in record["capture_limits"][0]


def test_stream_usage_is_invisible_to_the_application(instrument, mem):
    seen = []
    client = _client_for(_usage_aware(seen))  # default base_url: api.openai.com
    plain = list(client.chat.completions.create(model=MODEL, messages=MESSAGES, stream=True))
    with client.chat.completions.stream(model=MODEL, messages=MESSAGES) as helper:
        plain_final = helper.get_final_completion()

    instrument("openai", stream_usage=True)
    seen.clear()
    kwargs = dict(model=MODEL, messages=MESSAGES, stream=True)
    injected = list(client.chat.completions.create(**kwargs))
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in kwargs  # the caller's dict is not touched
    # same chunks, same serialisation: no usage-only chunk, no "usage": null
    assert [c.to_json() for c in injected] == [c.to_json() for c in plain]
    assert [c.model_dump(exclude_unset=True) for c in injected] == [
        c.model_dump(exclude_unset=True) for c in plain]
    assert all(c.choices for c in injected)
    with client.chat.completions.stream(model=MODEL, messages=MESSAGES) as helper:
        assert helper.get_final_completion().to_json() == plain_final.to_json()

    assert [r["token_usage"]["total_tokens"] for r in mem.records] == [5, 5]
    assert [r["outputs"]["chunk_count"] for r in mem.records] == [2, 2]
    assert all("capture_limits" not in r for r in mem.records)


def test_stream_usage_is_never_sent_to_other_endpoints(instrument, mem):
    instrument("openai", stream_usage=True)
    for base_url in ("https://my-resource.openai.azure.com/openai/v1",
                     "http://localhost:11434/v1", "https://gateway.example.com/v1"):
        seen = []
        client = _client_for(_usage_aware(seen), base_url=base_url)
        list(client.chat.completions.create(model=MODEL, messages=MESSAGES, stream=True))
        assert "stream_options" not in seen[0], base_url
    assert all(r["token_usage"] is None for r in mem.records)


def test_stream_usage_respects_the_callers_own_stream_options(instrument, mem):
    instrument("openai", stream_usage=True)
    for options in ({"include_usage": True}, {"include_usage": False}, {}):
        seen = []
        client = _client_for(_usage_aware(seen))
        chunks = list(client.chat.completions.create(
            model=MODEL, messages=MESSAGES, stream=True, stream_options=options))
        assert seen[0]["stream_options"] == options
        # the caller asked for the usage chunk: it is theirs to see
        assert len(chunks) == (3 if options.get("include_usage") else 2)


def test_stream_usage_is_not_injected_into_raw_response_modes(instrument, mem):
    instrument("openai", stream_usage=True)
    seen = []
    client = _client_for(_usage_aware(seen))
    with client.chat.completions.with_streaming_response.create(
            model=MODEL, messages=MESSAGES, stream=True) as response:
        b"".join(response.iter_bytes())
    assert "stream_options" not in seen[0]


def test_stream_usage_hosts_can_be_extended_and_env_enables_it(instrument, mem, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_OPENAI_STREAM_USAGE", "1")
    inst = instrument("openai", stream_usage_hosts=["gateway.example.com"])
    assert inst.options == {"stream_usage": True, "stream_usage_hosts": ["gateway.example.com"]}
    seen = []
    client = _client_for(_usage_aware(seen), base_url="https://gateway.example.com/v1")
    chunks = list(client.chat.completions.create(model=MODEL, messages=MESSAGES, stream=True))
    assert seen[0]["stream_options"] == {"include_usage": True} and len(chunks) == 2
    assert mem.records[0]["token_usage"]["total_tokens"] == 5


# -- lifecycle and version drift ----------------------------------------------

def test_auto_undo_auto_and_a_client_built_before_auto(mem):
    client = base._client()
    originals = {cls: static(cls, "create") for cls in (Completions, AsyncCompletions, Embeddings)}
    for _ in range(2):
        auto("openai", exporter=mem, async_capture=False)
        assert auto("openai", exporter=mem, async_capture=False) is auto_mod._PATCHES["openai"]["handler"]
        client.chat.completions.create(model=MODEL, messages=MESSAGES)
        undo("openai")
        assert all(static(cls, "create") is fn for cls, fn in originals.items())
        client.chat.completions.create(model=MODEL, messages=MESSAGES)
    assert len(mem.records) == 2


def test_an_explicit_option_after_auto_is_applied_not_ignored(mem, caplog):
    first = auto("openai", exporter=mem, async_capture=False)
    assert first.options["stream_usage"] is False
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        second = auto("openai", stream_usage=True)
    assert second is not first and second.options["stream_usage"] is True
    assert first.active is False and second._exporter is mem  # earlier options are kept
    assert "different options (stream_usage)" in caplog.text
    assert auto("openai", stream_usage=True) is second  # same again: idempotent
    seen = []
    list(_client_for(_usage_aware(seen)).chat.completions.create(
        model=MODEL, messages=MESSAGES, stream=True))
    assert seen[0]["stream_options"] == {"include_usage": True}


def test_a_surface_with_a_missing_method_is_skipped_whole_with_one_warning(mem, mocker, caplog):
    import openai.resources.embeddings as embeddings_module

    original_sync = static(Embeddings, "create")
    mocker.patch.object(embeddings_module, "AsyncEmbeddings", object())  # SDK renamed it
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        inst = auto("openai", exporter=mem, async_capture=False)
    try:
        assert static(Embeddings, "create") is original_sync  # not half-patched
        assert list(inst.skipped) == ["embeddings"]
        assert any("chat.completions.Completions.create" in p for p in inst.patched)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "embeddings" in warnings[0].getMessage()
        entry = status()["integrations"]["openai"]
        assert entry["state"] == "partial" and "embeddings" in entry["skipped"]
        assert entry["sdk_version"] == openai.__version__
        assert entry["tested_versions"] == list(bc_openai.TESTED_VERSIONS)
        base._client().chat.completions.create(model=MODEL, messages=MESSAGES)
        assert len(mem.records) == 1
    finally:
        undo("openai")


def test_a_changed_signature_leaves_the_surface_untouched(mem, mocker):
    def create(self, *, engine, prompt):  # no ``model`` any more
        return None

    mocker.patch.object(Embeddings, "create", create)
    inst = auto("openai", exporter=mem, async_capture=False)
    try:
        assert static(Embeddings, "create") is create
        assert "signature changed" in inst.skipped["embeddings"]
    finally:
        undo("openai")


def test_status_reports_limits_and_never_imports_anything(mem):
    report = status()
    assert report["integrations"]["openai"]["state"] in ("inactive", "not_installed")
    auto("openai", exporter=mem)
    entry = status()["integrations"]["openai"]
    assert entry["state"] == "patched" and entry["skipped"] == {}
    assert len(entry["patched"]) == 12 and entry["capture_content"] == "none"
    assert any("stream_options.include_usage" in limit for limit in entry["limits"])
    text = "\n".join(auto_mod.format_status())
    assert "openai: patched" in text and "tested with 3.57.0" in text and "limit:" in text
    assert entry["reported_versions"] == ["3.3.0"] and "reported working with 3.3.0" in text
    assert "NOT TESTED" not in text
    auto_mod._PATCHES["openai"]["handler"].sdk_version = "9.9.9"
    assert "sdk 9.9.9" in "\n".join(auto_mod.format_status())
    assert "THIS VERSION WAS NOT TESTED" in "\n".join(auto_mod.format_status())
