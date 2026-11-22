"""Red-team tests for the Bedrock integration: a real botocore client over
canned HTTP responses, so the event streams and bodies are botocore's own
``EventStream`` / ``StreamingBody`` objects."""

import asyncio
import io
import json
import sys
import types

import pytest

pytest.importorskip("boto3")

from botocore.eventstream import EventStream, EventStreamError  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.response import StreamingBody  # noqa: E402

from briefcase.integrations.providers import bedrock  # noqa: E402

from tests.providers._helpers import (  # noqa: E402
    COMPLETION,
    PROMPT,
    bedrock_client,
    drain_gc,
    encode_event,
    invoke_chunk,
)

MESSAGES = [{"role": "user", "content": [{"text": PROMPT}]}]
CONVERSE_STREAM = (
    encode_event("messageStart", b'{"role":"assistant"}')
    + encode_event("contentBlockDelta",
                   json.dumps({"contentBlockIndex": 0, "delta": {"text": COMPLETION}}).encode())
    + encode_event("messageStop", b'{"stopReason":"end_turn"}')
    + encode_event("metadata", b'{"usage":{"inputTokens":7,"outputTokens":5,"totalTokens":12,'
                               b'"cacheReadInputTokens":3},"metrics":{"latencyMs":9}}')
)
TITAN_BODY = json.dumps({"inputTextTokenCount": 5, "results": [
    {"tokenCount": 9, "outputText": COMPLETION, "completionReason": "FINISHED"}]}).encode()
ANTHROPIC_BODY = json.dumps({
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude",
    "content": [{"type": "text", "text": COMPLETION}], "stop_reason": "end_turn",
    "usage": {"input_tokens": 11, "output_tokens": 4, "cache_read_input_tokens": 2},
}).encode()
LLAMA_BODY = json.dumps({"generation": COMPLETION, "prompt_token_count": 6,
                         "generation_token_count": 3, "stop_reason": "stop"}).encode()
HEADERS = {"x-amzn-bedrock-input-token-count": "21", "x-amzn-bedrock-output-token-count": "8"}


def test_converse_stream_keeps_the_event_stream_type(instrument, mem):
    instrument("bedrock", capture_content=True)
    response = bedrock_client(CONVERSE_STREAM).converse_stream(modelId="m", messages=MESSAGES)
    stream = response["stream"]
    assert isinstance(stream, EventStream)  # was: a foreign proxy type
    assert callable(stream.close) and stream._operation_name == "ConverseStream"
    events = list(stream)
    assert [next(iter(e)) for e in events] == [
        "messageStart", "contentBlockDelta", "messageStop", "metadata"]
    record = mem.records[0]
    assert record["status"] == "ok" and record["finish_reason"] == "end_turn"
    assert record["token_usage"] == {
        "prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12,
        "cache_read_input_tokens": 3,
    }
    assert record["outputs"] == {"content": COMPLETION, "chunk_count": 4, "stream_completed": True}
    assert record["provider_latency_ms"] == 9


def test_converse_stream_abandoned_and_stream_errors(instrument, mem):
    instrument("bedrock")
    response = bedrock_client(CONVERSE_STREAM).converse_stream(modelId="m", messages=MESSAGES)
    for _ in response["stream"]:
        break
    del response
    drain_gc()
    assert [(r["status"], r["outputs"]["chunk_count"], r["token_usage"]) for r in mem.records] == [
        ("incomplete", 1, None)]

    mem.records.clear()
    failing = (encode_event("messageStart", b'{"role":"assistant"}')
               + encode_event("throttlingException", b'{"message":"slow down"}', "exception"))
    response = bedrock_client(failing).converse_stream(modelId="m", messages=MESSAGES)
    with pytest.raises(EventStreamError):
        list(response["stream"])
    assert [(r["status"], r["error_type"]) for r in mem.records] == [("error", "EventStreamError")]


def test_client_errors_propagate_unchanged(instrument, mem):
    instrument("bedrock")
    client = bedrock_client(b'{"message":"denied"}',
                            {"x-amzn-errortype": "AccessDeniedException"}, 403)
    with pytest.raises(ClientError) as caught:
        client.converse(modelId="m", messages=MESSAGES)
    assert caught.value.response["Error"]["Code"] == "AccessDeniedException"
    record = mem.records[0]
    assert (record["status"], record["http_status"], record["request_id"]) == ("error", 403, "req-1")


def test_invoke_model_headers_are_not_padded_with_zero(instrument, mem):
    """Only one of the two (undocumented) headers present: the other counter
    is unknown, not 0."""
    instrument("bedrock")
    client = bedrock_client(b'{"unknown":"shape"}', {"x-amzn-bedrock-input-token-count": "21"})
    response = client.invoke_model(modelId="some.model", body=b"{}")
    assert response["body"].read() == b'{"unknown":"shape"}'
    record = mem.records[0]
    assert record["token_usage"] == {
        "prompt_tokens": 21, "completion_tokens": None, "total_tokens": None}
    assert record["usage_source"] == "http_headers"
    assert "not recognised" in record["capture_limits"][0]


def test_invoke_model_with_both_headers_never_touches_the_body(instrument, mem):
    instrument("bedrock")
    response = bedrock_client(TITAN_BODY, HEADERS).invoke_model(modelId="m", body=b"{}")
    body = response["body"]
    assert type(body) is StreamingBody
    assert not {"read", "readinto", "close"} & set(vars(body))  # no wrappers installed
    assert len(mem.records) == 1  # emitted at return, as before
    assert mem.records[0]["token_usage"] == {
        "prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29}
    assert body.read() == TITAN_BODY


@pytest.mark.parametrize("payload,usage,finish", [
    (TITAN_BODY, {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}, "FINISHED"),
    (ANTHROPIC_BODY, {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15,
                      "cache_read_input_tokens": 2}, "end_turn"),
    (LLAMA_BODY, {"prompt_tokens": 6, "completion_tokens": 3, "total_tokens": 9}, "stop"),
])
def test_invoke_model_usage_comes_from_the_bytes_the_application_reads(
        instrument, mem, payload, usage, finish):
    instrument("bedrock", capture_content=True)
    response = bedrock_client(payload).invoke_model(modelId="m", body=b'{"p":1}')
    body = response["body"]
    assert type(body) is StreamingBody
    assert mem.records == []  # nothing was read on the application's behalf
    assert body._raw_stream.tell() == 0
    assert body.read() == payload  # identical bytes
    assert body.read() == b""
    assert len(mem.records) == 1
    record = mem.records[0]
    assert record["token_usage"] == usage and record["finish_reason"] == finish
    assert record["usage_source"] == "body"
    assert record["outputs"] == {"content": COMPLETION}


@pytest.mark.parametrize("reader", ["chunks", "iter_chunks", "iter_lines", "readinto", "iterate"])
def test_every_way_of_reading_the_body_yields_identical_bytes(instrument, mem, reader):
    plain = bedrock_client(TITAN_BODY).invoke_model(modelId="m", body=b"{}")["body"]
    instrument("bedrock")
    body = bedrock_client(TITAN_BODY).invoke_model(modelId="m", body=b"{}")["body"]

    def consume(stream):
        if reader == "chunks":
            out = []
            while True:
                piece = stream.read(7)
                if not piece:
                    return out
                out.append(piece)
        if reader == "iter_chunks":
            return list(stream.iter_chunks(chunk_size=5))
        if reader == "iter_lines":
            return list(stream.iter_lines())
        if reader == "iterate":
            return list(stream)
        buffer = bytearray(len(TITAN_BODY) + 10)
        count = stream.readinto(buffer)
        return [bytes(buffer[:count]), stream.readinto(buffer)]

    assert consume(body) == consume(plain)
    body.close()
    assert len(mem.records) == 1
    assert mem.records[0]["token_usage"]["total_tokens"] == 14


def test_invoke_model_body_never_read_or_too_large(instrument, mem, monkeypatch):
    instrument("bedrock")
    response = bedrock_client(TITAN_BODY).invoke_model(modelId="m", body=b"{}")
    response["body"].close()
    assert mem.records[0]["token_usage"] is None
    assert "not read to its end" in mem.records[0]["capture_limits"][0]

    mem.records.clear()
    response = bedrock_client(TITAN_BODY).invoke_model(modelId="m", body=b"{}")
    del response
    drain_gc()
    assert len(mem.records) == 1 and mem.records[0]["token_usage"] is None

    mem.records.clear()
    monkeypatch.setattr(bedrock, "MAX_BODY_BYTES", 10)
    body = bedrock_client(TITAN_BODY).invoke_model(modelId="m", body=b"{}")["body"]
    assert body.read() == TITAN_BODY  # the application still gets all of it
    assert mem.records[0]["token_usage"] is None and mem.records[0]["capture_limits"]


def test_invoke_model_read_errors_are_the_applications_to_see(instrument, mem):
    instrument("bedrock")
    body = bedrock_client(b"not json at all").invoke_model(modelId="m", body=b"{}")["body"]
    assert body.read() == b"not json at all"
    assert mem.records[0]["status"] == "ok" and mem.records[0]["token_usage"] is None


def test_invoke_stream_usage_sources(instrument, mem):
    instrument("bedrock", capture_content=True)
    cases = [
        # Bedrock's metering object wins when present
        ([{"outputText": COMPLETION, "index": 0, "inputTextTokenCount": 99,
           "totalOutputTextTokenCount": None, "completionReason": None},
          {"outputText": "", "index": 1, "totalOutputTextTokenCount": 98,
           "completionReason": "FINISHED",
           "amazon-bedrock-invocationMetrics": {"inputTokenCount": 4, "outputTokenCount": 2}}],
         {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
         "amazon-bedrock-invocationMetrics"),
        # Titan chunks without it
        ([{"outputText": COMPLETION, "index": 0, "inputTextTokenCount": 5,
           "totalOutputTextTokenCount": None, "completionReason": None},
          {"outputText": "", "index": 1, "totalOutputTextTokenCount": 9,
           "completionReason": "FINISHED"}],
         {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}, "body"),
        # Anthropic Messages events
        ([{"type": "message_start", "message": {"usage": {"input_tokens": 11, "output_tokens": 1}}},
          {"type": "content_block_delta", "index": 0,
           "delta": {"type": "text_delta", "text": COMPLETION}},
          {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
           "usage": {"output_tokens": 4}},
          {"type": "message_stop"}],
         {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}, "body"),
    ]
    for chunks, usage, source in cases:
        mem.records.clear()
        client = bedrock_client(b"".join(invoke_chunk(c) for c in chunks))
        response = client.invoke_model_with_response_stream(modelId="m", body=b"{}")
        events = list(response["body"])
        assert isinstance(response["body"], EventStream)
        assert all(isinstance(e["chunk"]["bytes"], bytes) for e in events)
        record = mem.records[0]
        assert record["token_usage"] == usage and record["usage_source"] == source
        assert record["outputs"]["content"] == COMPLETION and record["status"] == "ok"


def test_invoke_stream_of_an_unknown_shape_says_usage_is_unknown(instrument, mem):
    instrument("bedrock")
    chunks = (invoke_chunk({"text": "hi", "usage_hint": "TokenCount"})
              + invoke_chunk(b"not json invocationMetrics") + invoke_chunk([1, 2, 3]))
    response = bedrock_client(chunks).invoke_model_with_response_stream(modelId="m", body=b"{}")
    assert len(list(response["body"])) == 3  # malformed chunks never break iteration
    record = mem.records[0]
    assert record["token_usage"] is None and "usage_source" not in record
    assert "no recognised usage" in record["capture_limits"][0]


def test_other_services_and_clients_are_untouched(instrument, mem):
    import boto3

    instrument("bedrock")
    s3 = boto3.client("s3", region_name="us-east-1",
                      aws_access_key_id="t", aws_secret_access_key="t")
    assert "_make_api_call" not in vars(type(s3))
    assert mem.records == []


# -- aiobotocore ---------------------------------------------------------------

@pytest.fixture
def fake_aiobotocore(monkeypatch):
    """A stand-in with the shape of ``aiobotocore.client.AioBaseClient``
    (3.10.0 source): ``_make_api_call`` is a coroutine function of the same
    signature, event streams are async-iterable, the body's ``read`` is a
    coroutine. The real package is not installable next to this botocore."""
    package = types.ModuleType("aiobotocore")
    package.__version__ = "0.0-test"
    package.__path__ = []
    client_module = types.ModuleType("aiobotocore.client")

    class AioEventStream:
        def __init__(self, events):
            self._events = events

        def __aiter__(self):
            async def gen():
                for event in self._events:
                    yield event
            return gen()

    class AioStreamingBody:
        def __init__(self, data):
            self._data = io.BytesIO(data)

        async def read(self, amt=None):
            return self._data.read() if amt is None else self._data.read(amt)

        def close(self):
            pass

    class _Meta:
        class service_model:
            service_name = "bedrock-runtime"

    class AioBaseClient:
        meta = _Meta()
        responses = {}

        async def _make_api_call(self, operation_name, api_params):
            result = self.responses[operation_name]
            if isinstance(result, BaseException):
                raise result
            return result

    client_module.AioBaseClient = AioBaseClient
    client_module.AioEventStream = AioEventStream
    client_module.AioStreamingBody = AioStreamingBody
    package.client = client_module
    monkeypatch.setitem(sys.modules, "aiobotocore", package)
    monkeypatch.setitem(sys.modules, "aiobotocore.client", client_module)
    return client_module


def test_aiobotocore_calls_are_recorded(instrument, mem, fake_aiobotocore):
    import inspect

    original = fake_aiobotocore.AioBaseClient.__dict__["_make_api_call"]
    inst = instrument("aiobotocore", capture_content=True)
    wrapped = fake_aiobotocore.AioBaseClient.__dict__["_make_api_call"]
    assert wrapped.__wrapped__ is original and inspect.iscoroutinefunction(wrapped)
    assert inst.tested_versions == ()  # honest: never run against the real package
    client = fake_aiobotocore.AioBaseClient()
    events = [{"messageStart": {"role": "assistant"}},
              {"contentBlockDelta": {"delta": {"text": COMPLETION}, "contentBlockIndex": 0}},
              {"messageStop": {"stopReason": "end_turn"}},
              {"metadata": {"usage": {"inputTokens": 7, "outputTokens": 5, "totalTokens": 12}}}]
    client.responses = {
        "Converse": {"output": {"message": {"content": [{"text": COMPLETION}]}},
                     "stopReason": "end_turn",
                     "usage": {"inputTokens": 1, "outputTokens": 2, "totalTokens": 3},
                     "ResponseMetadata": {"RequestId": "r1"}},
        "ConverseStream": {"stream": fake_aiobotocore.AioEventStream(events),
                           "ResponseMetadata": {"RequestId": "r2"}},
        "InvokeModel": {"body": fake_aiobotocore.AioStreamingBody(TITAN_BODY),
                        "ResponseMetadata": {"RequestId": "r3", "HTTPHeaders": {}}},
        "ListFoundationModels": {"ok": True},
    }

    async def main():
        params = {"modelId": "m", "messages": MESSAGES}
        await client._make_api_call("Converse", params)
        response = await client._make_api_call("ConverseStream", params)
        assert isinstance(response["stream"], fake_aiobotocore.AioEventStream)
        seen = [event async for event in response["stream"]]
        invoked = await client._make_api_call("InvokeModel", {"modelId": "m", "body": b"{}"})
        data = await invoked["body"].read()
        assert await client._make_api_call("ListFoundationModels", {}) == {"ok": True}
        return seen, data

    seen, data = asyncio.run(main())
    assert seen == events and data == TITAN_BODY
    assert [r["function_name"] for r in mem.records] == [
        "bedrock-runtime.converse", "bedrock-runtime.converse_stream",
        "bedrock-runtime.invoke_model"]
    assert [r["token_usage"]["total_tokens"] for r in mem.records] == [3, 12, 14]
    assert mem.records[1]["outputs"]["stream_completed"] is True


def test_aiobotocore_errors_propagate_and_are_recorded(instrument, mem, fake_aiobotocore):
    instrument("aiobotocore")
    client = fake_aiobotocore.AioBaseClient()
    error = RuntimeError("throttled")
    client.responses = {"Converse": error}

    async def main():
        with pytest.raises(RuntimeError) as caught:
            await client._make_api_call("Converse", {"modelId": "m"})
        assert caught.value is error

    asyncio.run(main())
    assert [r["status"] for r in mem.records] == ["error"]


def test_aiobotocore_missing_is_an_import_error(mocker):
    from briefcase.integrations import providers

    mocker.patch.dict("sys.modules", {"aiobotocore": None})
    with pytest.raises(ImportError, match="not installed"):
        providers.instrument("aiobotocore")


def test_what_is_read_matches_botocores_bundled_service_model():
    """The fields read from Converse / ConverseStream are in botocore's own
    service model; the InvokeModel token sources are NOT (no usage member,
    no token-count header, no invocationMetrics on PayloadPart), which is
    why the module reads them only when present and labels the record with
    ``usage_source``. If a botocore release documents them, this fails and
    the module should switch to the documented members."""
    import botocore.session

    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    usage = model.shape_for("TokenUsage")
    assert {"inputTokens", "outputTokens", "totalTokens"} <= set(usage.required_members)
    assert {"cacheReadInputTokens", "cacheWriteInputTokens"} <= set(usage.members)
    assert "usage" in model.shape_for("ConverseStreamMetadataEvent").members
    assert {"stopReason", "usage", "metrics"} <= set(model.shape_for("ConverseResponse").members)
    assert "stopReason" in model.shape_for("MessageStopEvent").members

    invoke = model.operation_model("InvokeModel").output_shape
    assert set(invoke.members) >= {"body", "contentType"}
    header_names = {m.serialization.get("name", "").lower() for m in invoke.members.values()}
    assert "x-amzn-bedrock-input-token-count" not in header_names
    assert "x-amzn-bedrock-output-token-count" not in header_names
    assert not any("token" in name.lower() for name in invoke.members)

    stream = model.operation_model("InvokeModelWithResponseStream").output_shape
    payload = stream.members["body"].members["chunk"]
    assert set(payload.members) == {"bytes"}  # opaque: invocationMetrics is undocumented
