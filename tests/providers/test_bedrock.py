"""Bedrock Runtime auto-instrumentation through boto3 / botocore.

Non-streaming operations use botocore's ``Stubber`` on a real client. The
Stubber validates responses against the service model and cannot hand back
an event stream, so the streaming tests replace the client's
``_make_request`` (the HTTP step inside ``_make_api_call``) and return an
iterable of the event dicts botocore's ``EventStream`` yields."""

import io
import json

import pytest

boto3 = pytest.importorskip("boto3")

from botocore.client import BaseClient  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.response import StreamingBody  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

from briefcase.auto import auto, undo  # noqa: E402

from tests.providers._helpers import (  # noqa: E402
    COMPLETION,
    PROMPT,
    assert_base_record,
    assert_no_content,
    static,
)

MODEL = "anthropic.claude-opus-5-5"
CONVERSE_KWARGS = dict(
    modelId=MODEL,
    messages=[{"role": "user", "content": [{"text": PROMPT}]}],
    inferenceConfig={"maxTokens": 64, "temperature": 0.1},
)
CONVERSE_RESPONSE = {
    "output": {"message": {"role": "assistant", "content": [{"text": COMPLETION}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 10, "outputTokens": 6, "totalTokens": 16, "cacheReadInputTokens": 3},
    "metrics": {"latencyMs": 42},
    "ResponseMetadata": {"RequestId": "req-converse", "HTTPStatusCode": 200},
}
CONVERSE_EVENTS = [
    {"messageStart": {"role": "assistant"}},
    {"contentBlockDelta": {"delta": {"text": COMPLETION[:3]}, "contentBlockIndex": 0}},
    {"contentBlockDelta": {"delta": {"text": COMPLETION[3:]}, "contentBlockIndex": 0}},
    {"contentBlockStop": {"contentBlockIndex": 0}},
    {"messageStop": {"stopReason": "max_tokens"}},
    {"metadata": {"usage": {"inputTokens": 7, "outputTokens": 5, "totalTokens": 12},
                  "metrics": {"latencyMs": 9}}},
]


def _client(service="bedrock-runtime"):
    return boto3.client(
        service, region_name="us-east-1",
        aws_access_key_id="test", aws_secret_access_key="test",
    )


class _FakeEventStream:
    """Stands in for botocore.eventstream.EventStream: iterable, closable."""

    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __iter__(self):
        return iter(self._events)

    def close(self):
        self.closed = True


class _FakeHTTP:
    status_code = 200


def _stream_client(mocker, key, events):
    client = _client()
    stream = _FakeEventStream(events)
    mocker.patch.object(client, "_make_request", return_value=(
        _FakeHTTP(), {key: stream, "ResponseMetadata": {"RequestId": "req-stream"}}))
    return client, stream


def test_converse_emits_one_record_without_content(instrument, mem):
    instrument("bedrock")
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_response("converse", CONVERSE_RESPONSE)
        response = client.converse(**CONVERSE_KWARGS)

    assert response["output"]["message"]["content"][0]["text"] == COMPLETION
    assert len(mem.records) == 1
    record = mem.records[0]
    assert_base_record(record, "llm.bedrock.converse")
    assert_no_content(record)
    assert record["function_name"] == "bedrock-runtime.converse"
    assert record["status"] == "ok"
    assert record["stream"] is False
    assert record["outputs"] == {}
    assert record["model_parameters"] == {
        "provider": "bedrock", "model_name": MODEL, "maxTokens": 64, "temperature": 0.1,
    }
    assert record["token_usage"] == {
        "prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16,
        "cache_read_input_tokens": 3,
    }
    assert record["finish_reason"] == "end_turn"
    assert record["request_id"] == "req-converse"


def test_converse_content_is_recorded_when_enabled(instrument, mem):
    instrument("bedrock", capture_content=True)
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_response("converse", CONVERSE_RESPONSE)
        client.converse(system=[{"text": "be brief"}], **CONVERSE_KWARGS)

    record = mem.records[0]
    assert record["inputs"] == {
        "system": [{"text": "be brief"}], "messages": CONVERSE_KWARGS["messages"]}
    assert record["outputs"] == {"content": COMPLETION}


def test_error_is_recorded_and_reraised(instrument, mem):
    instrument("bedrock")
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "converse", service_error_code="ThrottlingException",
            service_message=f"slow down {PROMPT}", http_status_code=429,
        )
        with pytest.raises(ClientError):
            client.converse(**CONVERSE_KWARGS)

    assert len(mem.records) == 1
    record = mem.records[0]
    assert record["status"] == "error"
    assert record["error_type"] == "ThrottlingException"
    assert record["error"] == "ThrottlingException"
    assert record["http_status"] == 429
    assert record["model_parameters"]["model_name"] == MODEL
    assert_no_content(record)


def test_invoke_model_reads_tokens_from_headers_and_leaves_the_body(instrument, mem):
    instrument("bedrock", capture_content=True)
    client = _client()
    payload = json.dumps({"completion": COMPLETION}).encode()
    request_body = json.dumps({"prompt": PROMPT})
    with Stubber(client) as stubber:
        stubber.add_response("invoke_model", {
            "body": StreamingBody(io.BytesIO(payload), len(payload)),
            "contentType": "application/json",
            "ResponseMetadata": {
                "RequestId": "req-invoke", "HTTPStatusCode": 200,
                "HTTPHeaders": {
                    "x-amzn-bedrock-input-token-count": "21",
                    "x-amzn-bedrock-output-token-count": "9",
                },
            },
        })
        response = client.invoke_model(modelId=MODEL, body=request_body)

    # The one-shot body is still unread for the caller.
    assert json.loads(response["body"].read()) == {"completion": COMPLETION}
    record = mem.records[0]
    assert_base_record(record, "llm.bedrock.invoke_model")
    assert record["token_usage"] == {
        "prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30}
    assert record["request_id"] == "req-invoke"
    assert record["inputs"] == {"body": request_body}
    assert record["outputs"] == {}


def test_converse_stream_emits_exactly_one_record_at_stream_end(instrument, mem, mocker):
    instrument("bedrock")
    client, _ = _stream_client(mocker, "stream", CONVERSE_EVENTS)
    response = client.converse_stream(**CONVERSE_KWARGS)

    assert mem.records == []
    seen = []
    for event in response["stream"]:
        seen.append(event)
        if len(seen) < len(CONVERSE_EVENTS):
            assert mem.records == []
    assert seen == CONVERSE_EVENTS

    assert len(mem.records) == 1
    record = mem.records[0]
    assert_base_record(record, "llm.bedrock.converse")
    assert_no_content(record)
    assert record["function_name"] == "bedrock-runtime.converse_stream"
    assert record["stream"] is True
    assert record["outputs"] == {"chunk_count": len(CONVERSE_EVENTS), "stream_completed": True}
    assert record["token_usage"] == {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
    assert record["finish_reason"] == "max_tokens"
    assert record["request_id"] == "req-stream"


def test_converse_stream_content_is_accumulated_when_enabled(instrument, mem, mocker):
    instrument("bedrock", capture_content=True)
    client, _ = _stream_client(mocker, "stream", CONVERSE_EVENTS)
    list(client.converse_stream(**CONVERSE_KWARGS)["stream"])
    assert mem.records[0]["outputs"]["content"] == COMPLETION


def test_converse_stream_closed_early_emits_one_incomplete_record(instrument, mem, mocker):
    instrument("bedrock")
    client, inner = _stream_client(mocker, "stream", CONVERSE_EVENTS)
    stream = client.converse_stream(**CONVERSE_KWARGS)["stream"]
    next(iter(stream))
    stream.close()

    assert inner.closed is True
    assert len(mem.records) == 1
    assert mem.records[0]["outputs"] == {"chunk_count": 1, "stream_completed": False}


def test_invoke_model_stream_reads_invocation_metrics(instrument, mem, mocker):
    instrument("bedrock")
    events = [
        {"chunk": {"bytes": json.dumps({"delta": COMPLETION}).encode()}},
        {"chunk": {"bytes": json.dumps({"amazon-bedrock-invocationMetrics": {
            "inputTokenCount": 4, "outputTokenCount": 2}}).encode()}},
    ]
    client, _ = _stream_client(mocker, "body", events)
    response = client.invoke_model_with_response_stream(modelId=MODEL, body="{}")
    assert list(response["body"]) == events

    assert len(mem.records) == 1
    record = mem.records[0]
    assert record["decision_type"] == "llm.bedrock.invoke_model"
    assert record["token_usage"] == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    assert_no_content(record)


def test_other_services_and_operations_are_passed_through(instrument, mem):
    instrument("bedrock")
    sts = _client("sts")
    with Stubber(sts) as stubber:
        stubber.add_response("get_caller_identity", {
            "Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test", "UserId": "AIDATEST"})
        assert sts.get_caller_identity()["UserId"] == "AIDATEST"

    runtime = _client()
    with Stubber(runtime) as stubber:
        stubber.add_response("count_tokens", {"inputTokens": 3})
        runtime.count_tokens(
            modelId=MODEL,
            input={"converse": {"messages": CONVERSE_KWARGS["messages"]}},
        )
    assert mem.records == []


def test_undo_restores_the_original_method(mem):
    original = static(BaseClient, "_make_api_call")

    auto("bedrock", exporter=mem, async_capture=False)
    assert static(BaseClient, "_make_api_call") is not original

    undo("bedrock")
    assert static(BaseClient, "_make_api_call") is original
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_response("converse", CONVERSE_RESPONSE)
        client.converse(**CONVERSE_KWARGS)
    assert mem.records == []


def test_double_auto_does_not_double_wrap(mem):
    original = static(BaseClient, "_make_api_call")
    first = auto("bedrock", exporter=mem, async_capture=False)
    wrapped = static(BaseClient, "_make_api_call")
    second = auto("bedrock", exporter=mem, async_capture=False)

    assert first is second
    assert static(BaseClient, "_make_api_call") is wrapped
    assert wrapped.__wrapped__ is original
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_response("converse", CONVERSE_RESPONSE)
        client.converse(**CONVERSE_KWARGS)
    assert len(mem.records) == 1
