"""The anthropic instrumentation and replay executor against the Messages API
contract.

Anthropic publishes no machine-readable API specification (no OpenAPI or
similar document in its docs or GitHub organisation as of 2026-11-23), so
the contract is the request and response types shipped inside the
``anthropic`` package, rendered as JSON Schema and pinned in
``specs/anthropic-types.json.gz`` (see ``specs/manifest.json``). Requests
are validated with every object closed (``additionalProperties: false``),
so a parameter the types do not define is an error.
"""

import pytest

anthropic = pytest.importorskip("anthropic")

from briefcase.integrations.providers import anthropic as prov  # noqa: E402
from briefcase.replay.recorded import AnthropicExecutor, Overrides, analyze  # noqa: E402

from tests.contracts import _specs  # noqa: E402
from tests.contracts._examples import JsonSchemaExamples, int_leaves  # noqa: E402
from tests.contracts._harness import anthropic_client, json_body, sse_body  # noqa: E402

MODEL = "claude-contract-1"
REQUEST = "/$defs/MessageCreateParamsNonStreaming"
STREAM_REQUEST = "/$defs/MessageCreateParamsStreaming"
MESSAGE = "/$defs/Message"
EVENT = "/$defs/RawMessageStreamEvent"

#: Recorded only for older SDK releases: anthropic 2.12.0's
#: MessageCreateParams has no sampling parameters. Replay refuses a record
#: carrying them unless they are dropped (test below).
LEGACY_PARAMS = {"temperature", "top_p", "top_k"}

#: Integer usage fields the types define that the record does not keep.
UNRECORDED_USAGE = {
    ("cache_creation", "ephemeral_5m_input_tokens"): "TTL split of cache_creation_input_tokens",
    ("cache_creation", "ephemeral_1h_input_tokens"): "TTL split of cache_creation_input_tokens",
    ("server_tool_use", "web_search_requests"): "a request count, not tokens",
    ("server_tool_use", "web_fetch_requests"): "a request count, not tokens",
}

BASE = {"max_tokens": 64, "messages": [{"role": "user", "content": "Say hi"}]}


def spec(strict=False):
    return _specs.anthropic_spec(strict)


def example(pointer, *prefer, start=1001):
    return JsonSchemaExamples(spec().doc, prefer=prefer, start=start).generate({"$ref": "#" + pointer})


def assert_valid_request(body, pointer=REQUEST):
    errors = spec(strict=True).errors(pointer, body, strict=True)
    assert not errors, "request is not valid against the anthropic types:\n" + "\n".join(errors)


def full_request():
    props = spec().properties(REQUEST)
    gen = JsonSchemaExamples(spec().doc, prefer=("custom", "text"))
    request = {"model": MODEL, **BASE, "system": "Be terse."}
    for param in prov._PARAMS:
        if param not in LEGACY_PARAMS:
            request[param] = gen.generate(props[param], (param,))
    request["output_config"] = {"effort": "high"}
    request["tools"] = [gen.generate(props["tools"]["items"], ("tools",))]
    return request


def message_body():
    body = example(MESSAGE, "text")
    tool = JsonSchemaExamples(spec().doc, prefer=("tool_use",), start=5001).generate(
        {"$ref": "#" + MESSAGE})["content"][0]
    body["content"].append(tool)
    return body


# -- requests ------------------------------------------------------------------

def test_recorded_parameter_names_are_request_properties():
    declared = set(spec().properties(REQUEST))
    recorded = (set(prov._PARAMS) - LEGACY_PARAMS) | set(prov._INPUT_KEYS) | {"model", "output_config"}
    assert recorded <= declared, sorted(recorded - declared)
    assert not LEGACY_PARAMS & declared, "the types define sampling parameters again: drop LEGACY_PARAMS"


def test_requests_with_every_recorded_parameter_are_valid(instrument, mem):
    request = full_request()
    assert_valid_request(request)
    instrument("anthropic")
    client, wire = anthropic_client(json_body(message_body()))
    client.messages.create(**request)
    assert_valid_request(wire.last[2])
    record = mem.records[-1]
    for param in set(prov._PARAMS) - LEGACY_PARAMS:
        assert record["model_parameters"][param] == request[param]
    assert record["model_parameters"]["effort"] == "high"


@pytest.mark.parametrize("overrides", [
    Overrides(),
    Overrides(model="claude-contract-2", params={"max_tokens": 32}, drop={"speed"}),
], ids=["as-recorded", "what-if"])
def test_replay_requests_are_valid_against_the_types(overrides, instrument, mem):
    client, _ = anthropic_client(json_body(message_body()))
    instrument("anthropic")
    client.messages.create(**full_request())
    call = analyze(mem.records[-1])
    assert call.replayable, call.reasons

    replay_client, wire = anthropic_client(json_body(message_body()))
    executor = AnthropicExecutor(client=replay_client)
    assert executor.check(call, overrides) == []
    executor.execute(call, overrides)
    path, _, sent = wire.last
    assert path.endswith("/v1/messages")
    assert_valid_request(sent)
    assert sent["output_config"] == {"effort": "high"}
    assert "effort" not in sent
    assert sent["model"] == (overrides.model or MODEL)
    for key in overrides.drop:
        assert key not in sent
    assert len(mem.records) == 1


def test_replay_refuses_sampling_parameters_the_types_do_not_define(instrument, mem):
    client, _ = anthropic_client(json_body(message_body()))
    instrument("anthropic")
    client.messages.create(model=MODEL, **BASE)
    record = dict(mem.records[-1])
    record["model_parameters"] = dict(record["model_parameters"], temperature=0.2, top_k=5)
    call = analyze(record)
    executor = AnthropicExecutor(client=client)
    reasons = executor.check(call, Overrides())
    assert reasons and "temperature" in reasons[0] and "top_k" in reasons[0]
    assert executor.check(call, Overrides(drop={"temperature", "top_k"})) == []


# -- responses -------------------------------------------------------------------

USAGE_FIELDS = {
    "prompt_tokens": ("input_tokens",),
    "completion_tokens": ("output_tokens",),
    "cache_read_input_tokens": ("cache_read_input_tokens",),
    "cache_creation_input_tokens": ("cache_creation_input_tokens",),
    "reasoning_tokens": ("output_tokens_details", "thinking_tokens"),
}


def dig(value, path):
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def test_fields_the_records_read_exist_with_the_assumed_types():
    for path, kind in ((("model",), "string"), (("id",), "string"), (("stop_reason",), "string"),
                       (("content", "[]", "type"), "string"), (("content", "[]", "text"), "string"),
                       (("content", "[]", "name"), "string")):
        assert kind in spec().field_types(MESSAGE, path), path
    for key, path in USAGE_FIELDS.items():
        assert "integer" in spec().field_types(MESSAGE, ("usage",) + path), key


def test_spec_generated_messages_become_correct_records(instrument, mem):
    body = message_body()
    assert spec().errors(MESSAGE, body) == []
    instrument("anthropic")
    client, _ = anthropic_client(json_body(body, request_id="req-spec"))
    client.messages.create(model=MODEL, **BASE)
    record = mem.records[-1]

    assert record["status"] == "ok"
    assert record["request_id"] == "req-spec"
    assert record["response_id"] == body["id"]
    assert record["model_parameters"]["model_name"] == body["model"]
    assert record["finish_reason"] == body["stop_reason"]
    usage = record["token_usage"]
    for key, path in USAGE_FIELDS.items():
        assert usage[key] == dig(body["usage"], path), key
    assert usage["total_tokens"] == body["usage"]["input_tokens"] + body["usage"]["output_tokens"]
    recorded = set(usage.values())
    missing = [(p, v) for p, v in int_leaves(body["usage"])
               if v not in recorded and p[:2] not in UNRECORDED_USAGE]
    assert not missing, f"usage counters not recorded: {missing}"
    assert record["outputs"]["content"] == body["content"][0]["text"]
    tool = body["content"][1]
    assert record["outputs"]["tool_calls"] == [
        {"id": tool["id"], "name": tool["name"], "input": tool["input"]}]
    assert record["tool_call_count"] == 1

    replay_client, _ = anthropic_client(json_body(body))
    execution = AnthropicExecutor(client=replay_client).execute(analyze(record), Overrides())
    assert execution.usage == usage
    assert execution.finish_reason == body["stop_reason"]
    assert execution.output["text"] == body["content"][0]["text"]


# -- streams ---------------------------------------------------------------------

def stream_events():
    kinds = [("message_start", "text"), ("content_block_start", "text"),
             ("content_block_delta", "text_delta"), ("content_block_stop",),
             ("message_delta",), ("message_stop",)]
    events = []
    for number, prefer in enumerate(kinds):
        event = example(EVENT, *prefer, start=2001 + 1000 * number)
        assert event["type"] == prefer[0]
        if "index" in event:
            event["index"] = 0
        events.append(event)
    events[0]["message"]["content"] = []
    for event in events:
        assert spec().errors(EVENT, event) == [], event["type"]
    return events


def test_stream_events_from_the_types(instrument, mem):
    events = stream_events()
    instrument("anthropic")
    client, wire = anthropic_client(sse_body([(e["type"], e) for e in events]))
    list(client.messages.create(model=MODEL, stream=True, **BASE))
    assert_valid_request(wire.last[2], STREAM_REQUEST)
    record = mem.records[-1]
    start, delta, final = events[0]["message"], events[2]["delta"], events[4]
    assert record["status"] == "ok" and record["stream"] is True
    assert record["response_id"] == start["id"]
    assert record["model_parameters"]["model_name"] == start["model"]
    assert record["finish_reason"] == final["delta"]["stop_reason"]
    usage = record["token_usage"]
    # message_delta carries the cumulative counters: they win over message_start's.
    assert usage["prompt_tokens"] == final["usage"]["input_tokens"]
    assert usage["completion_tokens"] == final["usage"]["output_tokens"]
    assert usage["cache_read_input_tokens"] == final["usage"]["cache_read_input_tokens"]
    assert usage["cache_creation_input_tokens"] == final["usage"]["cache_creation_input_tokens"]
    assert usage["reasoning_tokens"] == final["usage"]["output_tokens_details"]["thinking_tokens"]
    assert record["outputs"]["content"] == delta["text"]


def test_stream_event_shapes_the_code_reads_are_in_the_types():
    gen = JsonSchemaExamples(spec().doc)
    variants = spec().resolve(EVENT).get("anyOf") or spec().resolve(EVENT).get("oneOf")
    kinds = {gen.discriminator(v) for v in variants}
    assert {"message_start", "message_delta", "message_stop", "content_block_delta"} <= kinds
    delta = "/$defs/RawContentBlockDeltaEvent"
    assert "text_delta" in {gen.discriminator(v) for v in
                            spec().resolve(delta)["properties"]["delta"]["anyOf"]}
    assert "string" in spec().field_types(delta, ("delta", "text"))
    for path, kind in ((("delta", "stop_reason"), "string"), (("usage", "output_tokens"), "integer"),
                       (("usage", "input_tokens"), "integer"),
                       (("usage", "output_tokens_details", "thinking_tokens"), "integer")):
        assert kind in spec().field_types("/$defs/RawMessageDeltaEvent", path), path
    for path in (("message", "usage", "input_tokens"), ("message", "id"), ("message", "model")):
        assert spec().field_types("/$defs/RawMessageStartEvent", path), path
