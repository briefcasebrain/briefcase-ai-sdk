"""The bedrock instrumentation and replay executor against the Bedrock
Runtime service model botocore ships (``specs/botocore-bedrock-runtime-
2023-09-30.json.gz``, pinned from botocore 1.44.9; see the manifest).

Requests are checked with botocore's own ``ParamValidator`` against the
pinned model, which is how the AWS SDK itself validates a call; responses
and ConverseStream events are generated from the model's output shapes,
sent through a real ``bedrock-runtime`` client's HTTP layer (JSON body,
``application/vnd.amazon.eventstream`` frames) and parsed by botocore.
"""

import json

import pytest

boto3 = pytest.importorskip("boto3")

from briefcase.integrations.providers import bedrock as prov  # noqa: E402
from briefcase.replay.recorded import BedrockExecutor, Overrides, analyze  # noqa: E402

from tests.contracts import _specs  # noqa: E402
from tests.contracts._examples import ShapeExamples, int_leaves  # noqa: E402
from tests.contracts._harness import bedrock_wire_client, jsonify  # noqa: E402
from tests.providers._helpers import encode_event  # noqa: E402

MODEL = "anthropic.claude-contract-1"
BASE = {"modelId": MODEL, "messages": [{"role": "user", "content": [{"text": "Say hi"}]}],
        "system": [{"text": "Be terse."}]}

USAGE_FIELDS = {
    "prompt_tokens": "inputTokens", "completion_tokens": "outputTokens", "total_tokens": "totalTokens",
    "cache_read_input_tokens": "cacheReadInputTokens", "cache_write_input_tokens": "cacheWriteInputTokens",
}
#: Integer usage fields the model defines that the record does not keep.
UNRECORDED_USAGE = {"cacheDetails": "TTL split of cacheWriteInputTokens"}


def service():
    return _specs.bedrock_service()


def shape(operation, side="input"):
    model = service().operation_model(operation)
    return model.input_shape if side == "input" else model.output_shape


def assert_valid(operation, params):
    errors = _specs.bedrock_errors(operation, params)
    assert not errors, f"{operation} request is not valid against the service model:\n" + "\n".join(errors)


def inference_config():
    members = shape("Converse").members["inferenceConfig"].members
    gen = ShapeExamples()
    config = {name: gen.generate(members[name], (name,)) for name in prov._INFERENCE_PARAMS}
    config["temperature"] = 0.5
    config["topP"] = 0.5
    return config


def converse_response(start=1001):
    gen = ShapeExamples(prefer=("text",), start=start)
    body = gen.generate(shape("Converse", "output"))
    tool = ShapeExamples(prefer=("toolUse",), start=start + 500).generate(
        shape("Converse", "output").members["output"])["message"]["content"][0]
    body["output"]["message"]["content"].append(tool)
    body["output"]["message"]["role"] = "assistant"
    return body


def converse_client(body):
    payload = json.dumps(jsonify(body)).encode()
    return bedrock_wire_client(payload, {"content-type": "application/json"})


# -- requests ------------------------------------------------------------------

def test_recorded_parameters_are_inference_config_members():
    members = shape("Converse").members
    assert set(prov._INFERENCE_PARAMS) <= set(members["inferenceConfig"].members)
    assert {"modelId", "messages", "system", "inferenceConfig"} <= set(members)
    for operation in ("ConverseStream",):
        assert set(prov._INFERENCE_PARAMS) <= set(shape(operation).members["inferenceConfig"].members)
    assert {"modelId", "body"} <= set(shape("InvokeModel").members)


def test_installed_executor_reads_the_same_members_as_the_pinned_model():
    top, inference = BedrockExecutor._members()
    assert top == set(shape("Converse").members)
    assert inference == set(shape("Converse").members["inferenceConfig"].members)


@pytest.mark.parametrize("overrides", [
    Overrides(),
    Overrides(model="anthropic.claude-contract-2", params={"temperature": 0.25, "stopSequences": ["END"]},
              drop={"topP"}),
    Overrides(params={"toolConfig": {"tools": [{"toolSpec": {
        "name": "lookup", "inputSchema": {"json": {"type": "object"}}}}]}}),
], ids=["as-recorded", "what-if", "top-level-param"])
def test_replay_requests_are_valid_against_the_service_model(overrides, instrument, mem):
    request = dict(BASE, inferenceConfig=inference_config())
    assert_valid("Converse", request)
    client, _ = converse_client(converse_response())
    instrument("bedrock")
    client.converse(**request)
    record = mem.records[-1]
    for name in prov._INFERENCE_PARAMS:
        assert record["model_parameters"][name] == request["inferenceConfig"][name]
    call = analyze(record)
    assert call.replayable, call.reasons

    replay_client, wire = converse_client(converse_response())
    executor = BedrockExecutor(client=replay_client)
    assert executor.check(call, overrides) == []
    api = executor.build(call, overrides)
    assert_valid("Converse", api)
    executor.execute(call, overrides)
    url, _, sent = wire.last
    assert url.endswith(f"/model/{overrides.model or MODEL}/converse")
    assert sent["messages"] == BASE["messages"] and sent["system"] == BASE["system"]
    assert set(sent.get("inferenceConfig", {})) <= set(shape("Converse").members["inferenceConfig"].members)
    for key in overrides.drop:
        assert key not in sent.get("inferenceConfig", {})
    if "toolConfig" in overrides.params:
        assert sent["toolConfig"] == overrides.params["toolConfig"]
    assert len(mem.records) == 1


def test_invoke_model_records_are_explained_not_replayed(instrument, mem):
    params = {"modelId": MODEL, "body": json.dumps({"prompt": "hi", "max_tokens": 5}),
              "contentType": "application/json", "accept": "application/json"}
    assert_valid("InvokeModel", params)
    client, _ = bedrock_wire_client(b'{"generation": "hi", "prompt_token_count": 2, '
                                    b'"generation_token_count": 1, "stop_reason": "stop"}')
    instrument("bedrock")
    client.invoke_model(**params)["body"].read()
    call = analyze(mem.records[-1])
    assert not call.replayable and "InvokeModel" in call.reasons[0]


# -- responses -------------------------------------------------------------------

def test_fields_the_records_read_exist_with_the_assumed_types():
    out = shape("Converse", "output").members
    assert out["stopReason"].type_name == "string"
    usage = out["usage"].members
    for field in USAGE_FIELDS.values():
        assert usage[field].type_name == "integer", field
    assert out["metrics"].members["latencyMs"].type_name == "long"
    content = out["output"].members["message"].members["content"].member.members
    assert content["text"].type_name == "string" and "toolUse" in content


def test_spec_generated_responses_become_correct_records(instrument, mem):
    body = converse_response()
    client, _ = converse_client(body)
    instrument("bedrock")
    client.converse(**BASE)
    record = mem.records[-1]
    assert record["status"] == "ok"
    assert record["finish_reason"] == body["stopReason"]
    assert record["request_id"] == "req-1"
    usage = record["token_usage"]
    for key, field in USAGE_FIELDS.items():
        assert usage[key] == body["usage"][field], key
    recorded = set(usage.values()) | {record["provider_latency_ms"]}
    missing = [(p, v) for p, v in int_leaves(body["usage"])
               if v not in recorded and p[0] not in UNRECORDED_USAGE]
    assert not missing, f"usage counters not recorded: {missing}"
    assert record["provider_latency_ms"] == body["metrics"]["latencyMs"]
    assert record["tool_call_count"] == 1
    assert record["outputs"]["content"] == body["output"]["message"]["content"][0]["text"]

    replay_client, _ = converse_client(body)
    execution = BedrockExecutor(client=replay_client).execute(analyze(record), Overrides())
    assert execution.usage == usage
    assert execution.finish_reason == body["stopReason"]
    tool = body["output"]["message"]["content"][1]["toolUse"]
    assert execution.output["tool_calls"][0]["name"] == tool["name"]


# -- streams ---------------------------------------------------------------------

def test_converse_stream_events_from_the_service_model(instrument, mem):
    stream_shape = shape("ConverseStream", "output").members["stream"]
    events = []
    for number, member in enumerate(("messageStart", "contentBlockDelta", "contentBlockStop",
                                     "messageStop", "metadata")):
        value = ShapeExamples(prefer=("text",), start=2001 + 1000 * number).generate(
            stream_shape.members[member], (member,))
        events.append((member, value))
    events[1][1]["contentBlockIndex"] = 0
    events[2][1]["contentBlockIndex"] = 0
    frames = b"".join(encode_event(member, json.dumps(jsonify(value)).encode())
                      for member, value in events)
    client, wire = bedrock_wire_client(frames, {"content-type": "application/vnd.amazon.eventstream"})
    instrument("bedrock")
    request = dict(BASE, inferenceConfig=inference_config())
    assert_valid("ConverseStream", request)
    response = client.converse_stream(**request)
    seen = list(response["stream"])
    assert [next(iter(e)) for e in seen] == [m for m, _ in events]
    record = mem.records[-1]
    stop, metadata = events[3][1], events[4][1]
    assert record["status"] == "ok" and record["stream"] is True
    assert record["finish_reason"] == stop["stopReason"]
    for key, field in USAGE_FIELDS.items():
        assert record["token_usage"][key] == metadata["usage"][field], key
    assert record["provider_latency_ms"] == metadata["metrics"]["latencyMs"]
    assert record["outputs"]["content"] == events[1][1]["delta"]["text"]


def test_stream_event_shapes_the_code_reads_are_in_the_model():
    events = shape("ConverseStream", "output").members["stream"]
    assert events.serialization.get("eventstream")
    members = events.members
    assert members["messageStop"].members["stopReason"].type_name == "string"
    metadata = members["metadata"].members
    assert set(USAGE_FIELDS.values()) <= set(metadata["usage"].members)
    assert metadata["metrics"].members["latencyMs"].type_name == "long"
    assert members["contentBlockDelta"].members["delta"].members["text"].type_name == "string"
