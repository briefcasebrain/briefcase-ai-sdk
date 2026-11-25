"""The openai instrumentation and replay executor against OpenAI's published
OpenAPI spec (``specs/openai-openapi.json.gz``, see ``specs/manifest.json``).

- Requests: what the instrumentation records (parameter names and value
  types) are request properties of each endpoint, and the request the
  replay executor sends from a record is valid against the endpoint's
  request schema, read off the wire of the installed SDK.
- Responses: example bodies generated from the response schemas are parsed
  by the installed SDK and turned into records; every field the record
  takes (model, ids, finish reason, each usage counter) must come out equal
  to the example, and every integer under ``usage`` must be recorded or be
  listed below with the reason it is not.
- Streams: chat chunks and Responses events generated from the stream
  schemas.
"""

import pytest

openai = pytest.importorskip("openai")

from briefcase.integrations.providers import openai as prov  # noqa: E402
from briefcase.replay.recorded import OpenAIExecutor, Overrides, analyze  # noqa: E402

from tests.contracts import _specs  # noqa: E402
from tests.contracts._examples import JsonSchemaExamples, int_leaves  # noqa: E402
from tests.contracts._harness import json_body, openai_client, sse_body  # noqa: E402

MODEL = "gpt-contract-1"

SURFACES = {
    "chat": {
        "path": "/chat/completions",
        "call": lambda client, **kw: client.chat.completions.create(**kw),
        "base": {"messages": [{"role": "system", "content": "Be terse."},
                              {"role": "user", "content": "Say hi"}]},
        "decision_type": "llm.openai.chat",
    },
    "responses": {
        "path": "/responses",
        "call": lambda client, **kw: client.responses.create(**kw),
        "base": {"instructions": "Be terse.",
                 "input": [{"role": "user", "content": "Say hi"}]},
        "decision_type": "llm.openai.responses",
    },
    "completions": {
        "path": "/completions",
        "call": lambda client, **kw: client.completions.create(**kw),
        "base": {"prompt": "Say hi"},
        "decision_type": "llm.openai.completions",
    },
    "embeddings": {
        "path": "/embeddings",
        "call": lambda client, **kw: client.embeddings.create(**kw),
        "base": {"input": "Say hi"},
        "decision_type": "llm.openai.embeddings",
    },
}
REPLAYABLE = ("chat", "responses", "completions")

#: Integer usage fields the spec defines that the record does not keep, and why.
UNRECORDED_USAGE = {
    ("prompt_tokens_details", "text_tokens"): "modality split of prompt_tokens; totals are recorded",
    ("prompt_tokens_details", "image_tokens"): "modality split of prompt_tokens; totals are recorded",
    ("completion_tokens_details", "text_tokens"): "modality split of completion_tokens",
    ("input_tokens_details", "cache_write_tokens_details"): "TTL split of cache_write_tokens",
}


def spec():
    return _specs.openai_spec()


def op(name):
    return _specs.openai_operation(SURFACES[name]["path"])


def example(pointer, *prefer, start=1001):
    return JsonSchemaExamples(spec().doc, prefer=prefer, start=start).generate({"$ref": "#" + pointer})


def param_value(name, param):
    """A value for one request parameter, generated from its schema."""
    schema = spec().properties(op(name)["request"])[param]
    return JsonSchemaExamples(spec().doc, prefer=("function",)).generate(schema, (param,))


def full_request(name):
    """The base request plus every parameter and input the instrumentation
    records, with spec-generated values."""
    surface = prov._SURFACES[name]
    request = {"model": MODEL, **SURFACES[name]["base"]}
    for param in surface["params"]:
        request[param] = param_value(name, param)
    if "tools" in surface["input_keys"]:
        request["tools"] = param_value(name, "tools")
    return request


def assert_valid_request(name, body):
    errors = spec().errors(op(name)["request"], body, strict=True)
    assert not errors, f"{name} request is not valid against the spec:\n" + "\n".join(errors)


def assert_usage_leaves(usage_example, token_usage):
    recorded = set((token_usage or {}).values())
    missing = [
        (path, value) for path, value in int_leaves(usage_example)
        if value not in recorded and tuple(p for p in path if not p.isdigit())[:2] not in UNRECORDED_USAGE
    ]
    assert not missing, (
        "usage counters defined by the spec are not recorded (add them, or list them in "
        f"UNRECORDED_USAGE with the reason): {missing}")


# -- requests ------------------------------------------------------------------

@pytest.mark.parametrize("name", list(SURFACES))
def test_recorded_parameter_names_are_request_properties(name):
    surface = prov._SURFACES[name]
    declared = set(spec().properties(op(name)["request"]))
    recorded = set(surface["params"]) | set(surface["input_keys"]) | {"model"}
    assert recorded <= declared, sorted(recorded - declared)


@pytest.mark.parametrize("name", list(SURFACES))
def test_requests_with_every_recorded_parameter_are_valid(name, instrument, mem):
    request = full_request(name)
    assert_valid_request(name, request)
    instrument("openai")
    client, wire = openai_client(json_body(example(op(name)["json"], "message", "output_text")))
    SURFACES[name]["call"](client, **request)
    path, _, body = wire.last
    assert path.endswith(SURFACES[name]["path"])
    assert_valid_request(name, body)
    record = mem.records[-1]
    assert record["decision_type"] == SURFACES[name]["decision_type"]
    for param in prov._SURFACES[name]["params"]:
        value = request[param]
        if isinstance(value, (bool, int, float, str)):
            assert record["model_parameters"][param] == value, param


@pytest.mark.parametrize("overrides", [
    Overrides(),
    Overrides(model="gpt-contract-2", params={"temperature": 0.25}, drop={"top_p"}),
], ids=["as-recorded", "what-if"])
@pytest.mark.parametrize("name", REPLAYABLE)
def test_replay_requests_are_valid_against_the_spec(name, overrides, instrument, mem):
    body = example(op(name)["json"], "message", "output_text")
    client, _ = openai_client(json_body(body))
    instrument("openai")
    SURFACES[name]["call"](client, **full_request(name))
    call = analyze(mem.records[-1])
    assert call.replayable, call.reasons

    replay_client, wire = openai_client(json_body(body))
    executor = OpenAIExecutor(client=replay_client)
    assert executor.check(call, overrides) == []
    execution = executor.execute(call, overrides)
    path, headers, sent = wire.last
    assert path.endswith(SURFACES[name]["path"])
    assert_valid_request(name, sent)
    model, params = overrides.model or call.model, dict(call.params)
    params.update(overrides.params)
    for key in overrides.drop:
        params.pop(key, None)
        assert key not in sent
    assert sent["model"] == model
    for key, value in params.items():
        assert sent[key] == value, key
    assert execution.usage == call.usage
    assert len(mem.records) == 1  # the replay was not captured again


# -- responses -------------------------------------------------------------------

RESPONSE_FIELDS = {
    # surface: [(record field, path in the response, JSON type the code assumes)]
    "chat": [("model", ("model",), "string"), ("response_id", ("id",), "string"),
             ("finish_reason", ("choices", "[]", "finish_reason"), "string")],
    "responses": [("model", ("model",), "string"), ("response_id", ("id",), "string"),
                  ("finish_reason", ("incomplete_details", "reason"), "string"),
                  ("finish_reason", ("status",), "string")],
    "completions": [("model", ("model",), "string"), ("response_id", ("id",), "string"),
                    ("finish_reason", ("choices", "[]", "finish_reason"), "string")],
    "embeddings": [("model", ("model",), "string")],
}

USAGE_FIELDS = {
    "chat": {
        "prompt_tokens": ("prompt_tokens",), "completion_tokens": ("completion_tokens",),
        "total_tokens": ("total_tokens",),
        "cached_tokens": ("prompt_tokens_details", "cached_tokens"),
        "cache_write_tokens": ("prompt_tokens_details", "cache_write_tokens"),
        "audio_prompt_tokens": ("prompt_tokens_details", "audio_tokens"),
        "reasoning_tokens": ("completion_tokens_details", "reasoning_tokens"),
        "audio_completion_tokens": ("completion_tokens_details", "audio_tokens"),
        "accepted_prediction_tokens": ("completion_tokens_details", "accepted_prediction_tokens"),
        "rejected_prediction_tokens": ("completion_tokens_details", "rejected_prediction_tokens"),
    },
    "responses": {
        "prompt_tokens": ("input_tokens",), "completion_tokens": ("output_tokens",),
        "total_tokens": ("total_tokens",),
        "cached_tokens": ("input_tokens_details", "cached_tokens"),
        "cache_write_tokens": ("input_tokens_details", "cache_write_tokens"),
        "reasoning_tokens": ("output_tokens_details", "reasoning_tokens"),
    },
    "embeddings": {"prompt_tokens": ("prompt_tokens",), "total_tokens": ("total_tokens",)},
}
USAGE_FIELDS["completions"] = USAGE_FIELDS["chat"]


def dig(value, path):
    for key in path:
        value = value[0] if key == "[]" else value.get(key) if isinstance(value, dict) else None
    return value


@pytest.mark.parametrize("name", list(SURFACES))
def test_fields_the_records_read_exist_with_the_assumed_types(name):
    pointer = op(name)["json"]
    for _, path, kind in RESPONSE_FIELDS[name]:
        assert kind in spec().field_types(pointer, path), (name, path)
    for key, path in USAGE_FIELDS[name].items():
        assert "integer" in spec().field_types(pointer, ("usage",) + path), (name, key, path)


@pytest.mark.parametrize("name", list(SURFACES))
def test_spec_generated_responses_become_correct_records(name, instrument, mem):
    body = example(op(name)["json"], "message", "output_text")
    assert spec().errors(op(name)["json"], body) == []
    instrument("openai")
    client, _ = openai_client(json_body(body, request_id="req-spec"))
    result = SURFACES[name]["call"](client, **{"model": MODEL, **SURFACES[name]["base"]})
    record = mem.records[-1]

    assert record["status"] == "ok"
    assert record["request_id"] == "req-spec"
    assert record["model_parameters"]["model_name"] == body["model"]
    assert record["model_parameters"]["requested_model"] == MODEL
    if name != "embeddings":
        assert record["response_id"] == body["id"]
    if name in ("chat", "completions"):
        assert record["finish_reason"] == body["choices"][0]["finish_reason"]
    if name == "responses":
        assert record["finish_reason"] == (body["incomplete_details"]["reason"] or body["status"])
    usage = record["token_usage"]
    for key, path in USAGE_FIELDS[name].items():
        assert usage[key] == dig(body["usage"], path), key
    if name == "embeddings":
        assert usage["completion_tokens"] == 0
    assert_usage_leaves(body["usage"], usage)

    outputs = record["outputs"]
    if name == "chat":
        message = body["choices"][0]["message"]
        assert outputs["content"] == message["content"]
        assert outputs["refusal"] == message["refusal"]
        assert outputs["tool_calls"][0]["function"]["name"] == message["tool_calls"][0]["function"]["name"]
    elif name == "responses":
        assert outputs["content"] == result.output_text
        assert outputs["content"] in str(body["output"])
    elif name == "completions":
        assert outputs["content"] == body["choices"][0]["text"]
    else:
        assert record["embedding_count"] == len(body["data"])


@pytest.mark.parametrize("name", REPLAYABLE)
def test_replay_reads_the_spec_response_like_the_record(name, instrument, mem):
    body = example(op(name)["json"], "message", "output_text")
    client, _ = openai_client(json_body(body))
    instrument("openai")
    SURFACES[name]["call"](client, **{"model": MODEL, **SURFACES[name]["base"]})
    record = mem.records[-1]
    replay_client, _ = openai_client(json_body(body))
    execution = OpenAIExecutor(client=replay_client).execute(analyze(record), Overrides())
    assert execution.model == record["model_parameters"]["model_name"]
    assert execution.usage == record["token_usage"]
    assert execution.finish_reason == record["finish_reason"]
    assert execution.output["text"] == record["outputs"]["content"]


# -- streams ---------------------------------------------------------------------

def _chunks(name):
    """A content chunk, a finishing chunk and the usage-only chunk, each
    generated from the stream schema and checked against it."""
    pointer = op(name)["stream"]
    first = example(pointer, start=2001)
    last = example(pointer, start=3001)
    first["choices"][0]["finish_reason"] = None
    first.pop("usage", None)
    finish = example(pointer, start=4001)
    finish.pop("usage", None)
    finish["choices"][0]["finish_reason"] = last["choices"][0]["finish_reason"]
    usage_only = dict(last, choices=[])
    for chunk in (first, finish):
        chunk["choices"][0]["index"] = 0  # one choice; generated indexes are distinct numbers
    chunks = [first, finish, usage_only]
    for chunk in chunks:
        assert spec().errors(pointer, chunk) == [], chunk
    return chunks


@pytest.mark.parametrize("name", ["chat", "completions"])
def test_stream_chunks_from_the_spec(name, instrument, mem):
    chunks = _chunks(name)
    instrument("openai")
    client, wire = openai_client(sse_body([(None, c) for c in chunks], done=True))
    stream = SURFACES[name]["call"](client, model=MODEL, stream=True,
                                    stream_options={"include_usage": True},
                                    **SURFACES[name]["base"])
    list(stream)
    assert_valid_request(name, wire.last[2])
    record = mem.records[-1]
    assert record["stream"] is True and record["status"] == "ok"
    assert record["outputs"]["chunk_count"] == 3
    assert record["finish_reason"] == chunks[1]["choices"][0]["finish_reason"]
    assert record["model_parameters"]["model_name"] == chunks[-1]["model"]
    usage = chunks[-1]["usage"]
    for key, path in USAGE_FIELDS[name].items():
        assert record["token_usage"][key] == dig(usage, path), key
    first = chunks[0]["choices"][0]
    text = first["delta"]["content"] if name == "chat" else first["text"]
    assert record["outputs"]["content"].startswith(text)


def test_responses_stream_events_from_the_spec(instrument, mem):
    pointer = op("responses")["stream"]
    events = [example(pointer, kind, "message", "output_text", start=start)
              for kind, start in (("response.created", 2001), ("response.output_text.delta", 3001),
                                  ("response.completed", 4001))]
    assert [e["type"] for e in events] == ["response.created", "response.output_text.delta",
                                           "response.completed"]
    for event in events:
        assert spec().errors(pointer, event) == [], event["type"]
    instrument("openai")
    client, wire = openai_client(sse_body([(e["type"], e) for e in events]))
    list(client.responses.create(model=MODEL, stream=True, **SURFACES["responses"]["base"]))
    assert_valid_request("responses", wire.last[2])
    record = mem.records[-1]
    final = events[-1]["response"]
    assert record["status"] == "ok"
    assert record["response_id"] == final["id"]
    assert record["model_parameters"]["model_name"] == final["model"]
    for key, path in USAGE_FIELDS["responses"].items():
        assert record["token_usage"][key] == dig(final["usage"], path), key
    assert record["outputs"]["content"] == events[1]["delta"]


def test_stream_event_shapes_the_code_reads_are_in_the_spec():
    pointer = op("responses")["stream"]
    node = spec().resolve(pointer)
    variants = node.get("anyOf") or node.get("oneOf")
    kinds = {JsonSchemaExamples(spec().doc).discriminator(v) for v in variants}
    for kind in prov._ResponsesStream._TERMINAL + ("response.created", "response.output_text.delta"):
        assert kind in kinds, kind
    for name, text in (("chat", ("choices", "[]", "delta", "content")),
                       ("completions", ("choices", "[]", "text"))):
        chunk = op(name)["stream"]
        for path, kind in ((("id",), "string"), (("model",), "string"),
                           (("choices", "[]", "index"), "integer"),
                           (("choices", "[]", "finish_reason"), "string"),
                           (("usage", "prompt_tokens"), "integer"), (text, "string")):
            assert kind in spec().field_types(chunk, path), (name, path)
    assert "string" in spec().field_types(op("responses")["json"], ("error", "code"))
    assert "string" in spec().field_types(op("chat")["json"], ("service_tier",))
