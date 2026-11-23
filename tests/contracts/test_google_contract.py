"""The google-genai instrumentation and replay executor against the Gemini
API's published discovery document (generativelanguage v1beta).

The discovery document is not redistributable, so it is not vendored: it is
downloaded once into the cache (see ``_specs.downloaded``) and these tests
skip when it is unavailable. Its schemas are converted to JSON Schema with
every object closed, so a request field the API does not define is an
error. Requests are validated as the installed SDK puts them on the wire
(camelCase, ``generationConfig``), responses are generated from the
``GenerateContentResponse`` schema and parsed by the installed SDK.
"""

import pytest

genai = pytest.importorskip("google.genai")

from google.genai import types  # noqa: E402

from briefcase.integrations.providers import google_genai as prov  # noqa: E402
from briefcase.replay.recorded import GoogleGenAIExecutor, Overrides, analyze  # noqa: E402

from tests.contracts import _specs  # noqa: E402
from tests.contracts._examples import JsonSchemaExamples, int_leaves  # noqa: E402
from tests.contracts._harness import google_client  # noqa: E402
from tests.providers._helpers import sse  # noqa: E402

MODEL = "gemini-contract-1"
REQUEST = "/$defs/GenerateContentRequest"
RESPONSE = "/$defs/GenerateContentResponse"
GENERATION_CONFIG = "/$defs/GenerationConfig"

#: SDK config field -> generationConfig field it is sent as.
PARAM_FIELDS = {
    "temperature": "temperature", "top_p": "topP", "top_k": "topK",
    "max_output_tokens": "maxOutputTokens", "candidate_count": "candidateCount", "seed": "seed",
    "frequency_penalty": "frequencyPenalty", "presence_penalty": "presencePenalty",
}

USAGE_FIELDS = {
    "prompt_tokens": "promptTokenCount", "completion_tokens": "candidatesTokenCount",
    "total_tokens": "totalTokenCount", "cached_content_token_count": "cachedContentTokenCount",
    "thoughts_token_count": "thoughtsTokenCount", "tool_use_prompt_token_count": "toolUsePromptTokenCount",
}
#: usageMetadata integers the record does not keep: per-modality splits.
UNRECORDED_USAGE = {"promptTokensDetails", "candidatesTokensDetails", "cacheTokensDetails",
                    "toolUsePromptTokensDetails"}


@pytest.fixture(scope="module")
def spec():
    return _specs.google_spec(strict=True)


def responder(body, stream_bodies=None):
    def respond(path, request):
        if "streamGenerateContent" in path:
            return sse([(None, b) for b in stream_bodies]), {}, True
        return body, {}, False
    return respond


def response_body(spec, start=1001):
    body = JsonSchemaExamples(spec.doc, start=start).generate({"$ref": "#" + RESPONSE})
    candidate = body["candidates"][0]
    parts = candidate["content"]["parts"]
    # A Part holds one kind of data (a oneof in the API); keep a text part
    # and a function call part, as the API returns them.
    candidate["content"]["parts"] = [{"text": parts[0]["text"]},
                                     {"functionCall": parts[0]["functionCall"]}]
    candidate["finishReason"] = "STOP"
    candidate["index"] = 0
    body["candidates"] = [candidate]
    # A blocked prompt has no candidates; and google-genai 3.6.1 does not
    # know the discovery document's BLOCK_REASON_UNSPECIFIED (it warns).
    body.pop("promptFeedback", None)
    assert spec.errors(RESPONSE, body) == []
    return body


def assert_valid_request(spec, body):
    errors = spec.errors(REQUEST, body, strict=True)
    assert not errors, "request is not valid against the discovery document:\n" + "\n".join(errors)


def full_config(spec):
    gen = JsonSchemaExamples(spec.doc)
    props = spec.properties(GENERATION_CONFIG)
    config = {name: gen.generate(props[field], (field,)) for name, field in PARAM_FIELDS.items()}
    config["candidate_count"] = 1
    return config


# -- requests ------------------------------------------------------------------

def test_recorded_parameters_are_generation_config_fields(spec):
    assert set(prov._PARAMS) == set(PARAM_FIELDS)
    declared = spec.properties(GENERATION_CONFIG)
    for name, field in PARAM_FIELDS.items():
        assert field in declared, field
        assert name in types.GenerateContentConfig.model_fields, name
    for key in ("contents", "systemInstruction", "generationConfig"):
        assert key in spec.properties(REQUEST), key


def test_recorded_parameters_reach_generation_config_on_the_wire(spec, instrument, mem):
    config = full_config(spec)
    instrument("google-genai")
    client, wire = google_client(responder(response_body(spec)))
    client.models.generate_content(
        model=MODEL, contents="Say hi",
        config=types.GenerateContentConfig(system_instruction="Be terse.", **config))
    path, _, body = wire.last
    assert path.endswith(f"/models/{MODEL}:generateContent")
    assert_valid_request(spec, body)
    for name, field in PARAM_FIELDS.items():
        assert body["generationConfig"][field] == config[name], field
        assert mem.records[-1]["model_parameters"][name] == config[name], name


@pytest.mark.parametrize("overrides", [
    Overrides(),
    Overrides(model="gemini-contract-2", params={"temperature": 0.25}, drop={"seed"}),
], ids=["as-recorded", "what-if"])
def test_replay_requests_are_valid_against_the_discovery_document(spec, overrides, instrument, mem):
    body = response_body(spec)
    client, _ = google_client(responder(body))
    instrument("google-genai")
    client.models.generate_content(
        model=MODEL,
        contents=[types.Content(role="user", parts=[types.Part(text="Say hi")]),
                  types.Content(role="model", parts=[types.Part(text="Hi.")]),
                  types.Content(role="user", parts=[types.Part(text="Again")])],
        config=types.GenerateContentConfig(system_instruction="Be terse.", **full_config(spec)))
    call = analyze(mem.records[-1])
    assert call.replayable, call.reasons

    replay_client, wire = google_client(responder(body))
    executor = GoogleGenAIExecutor(client=replay_client)
    assert executor.check(call, overrides) == []
    executor.execute(call, overrides)
    path, headers, sent = wire.last
    assert path.endswith(f"/models/{overrides.model or MODEL}:generateContent")
    assert_valid_request(spec, sent)
    assert sent["systemInstruction"]["parts"][0]["text"] == "Be terse."
    assert [c["role"] for c in sent["contents"]] == ["user", "model", "user"]
    for key in overrides.drop:
        assert PARAM_FIELDS[key] not in sent["generationConfig"]
    for key, value in overrides.params.items():
        assert sent["generationConfig"][PARAM_FIELDS[key]] == value
    assert len(mem.records) == 1


# -- responses -------------------------------------------------------------------

def test_fields_the_records_read_exist_with_the_assumed_types(spec):
    for path, kind in ((("modelVersion",), "string"), (("responseId",), "string"),
                       (("candidates", "[]", "finishReason"), "string"),
                       (("candidates", "[]", "content", "parts", "[]", "text"), "string"),
                       (("candidates", "[]", "content", "parts", "[]", "thought"), "boolean")):
        assert kind in spec.field_types(RESPONSE, path), path
    for field in USAGE_FIELDS.values():
        assert "integer" in spec.field_types(RESPONSE, ("usageMetadata", field)), field


def test_spec_generated_responses_become_correct_records(spec, instrument, mem):
    body = response_body(spec)
    instrument("google-genai")
    client, _ = google_client(responder(body))
    client.models.generate_content(model=MODEL, contents="Say hi")
    record = mem.records[-1]
    assert record["status"] == "ok"
    assert record["model_parameters"]["model_name"] == body["modelVersion"]
    assert record["model_parameters"]["requested_model"] == MODEL
    assert record["response_id"] == body["responseId"]
    assert record["finish_reason"] == body["candidates"][0]["finishReason"]
    usage = record["token_usage"]
    for key, field in USAGE_FIELDS.items():
        assert usage[key] == body["usageMetadata"][field], key
    recorded = set(usage.values())
    missing = [(p, v) for p, v in int_leaves(body["usageMetadata"])
               if v not in recorded and p[0] not in UNRECORDED_USAGE]
    assert not missing, f"usage counters not recorded: {missing}"
    assert record["outputs"]["content"] == body["candidates"][0]["content"]["parts"][0]["text"]

    replay_client, _ = google_client(responder(body))
    execution = GoogleGenAIExecutor(client=replay_client).execute(analyze(record), Overrides())
    assert execution.usage == usage
    assert execution.model == body["modelVersion"]
    assert execution.output["tool_calls"][0]["name"] == \
        body["candidates"][0]["content"]["parts"][1]["functionCall"]["name"]


def test_stream_chunks_from_the_discovery_document(spec, instrument, mem):
    first = response_body(spec, start=2001)
    first["candidates"][0].pop("finishReason")
    first["candidates"][0]["content"]["parts"] = first["candidates"][0]["content"]["parts"][:1]
    first.pop("usageMetadata")
    last = response_body(spec, start=3001)
    last["candidates"][0]["content"]["parts"] = last["candidates"][0]["content"]["parts"][:1]
    for chunk in (first, last):
        assert spec.errors(RESPONSE, chunk) == []
    instrument("google-genai")
    client, wire = google_client(responder(None, [first, last]))
    list(client.models.generate_content_stream(model=MODEL, contents="Say hi"))
    path, _, sent = wire.last
    assert ":streamGenerateContent" in path and "alt=sse" in path
    assert_valid_request(spec, sent)
    record = mem.records[-1]
    assert record["status"] == "ok" and record["stream"] is True
    assert record["finish_reason"] == "STOP"
    for key, field in USAGE_FIELDS.items():
        assert record["token_usage"][key] == last["usageMetadata"][field], key
    texts = [c["candidates"][0]["content"]["parts"][0]["text"] for c in (first, last)]
    assert record["outputs"]["content"] == "".join(texts)


def test_discovery_document_is_the_pinned_api(spec):
    discovery = _specs.require_downloaded("gemini-discovery-v1beta")
    assert discovery["version"] == "v1beta"
    methods = discovery["resources"]["models"]["methods"]
    for name in ("generateContent", "streamGenerateContent"):
        assert methods[name]["request"] == {"$ref": "GenerateContentRequest"}
        assert methods[name]["response"] == {"$ref": "GenerateContentResponse"}
    if _specs.canonical_sha256(discovery) != _specs.entry("gemini-discovery-v1beta")["sha256"]:
        import warnings

        warnings.warn(
            f"the cached discovery document (revision {discovery.get('revision')}) differs from "
            f"the pinned one ({_specs.entry('gemini-discovery-v1beta')['version']}); these tests "
            "ran against the cached copy. See scripts/refresh_provider_specs.py", UserWarning)
