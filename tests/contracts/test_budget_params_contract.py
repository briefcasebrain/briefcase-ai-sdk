"""The output-token cap a budget reserves for (``briefcase.budgets``) is read
from the request by ``briefcase.integrations.providers._requests``. These
tests check, per endpoint, that the parameter names it reads are the
spec's token-limit parameters (integers), that the default it assumes
when none is given matches the spec, and that a spec-valid request with
the cap set yields that cap.
"""

import pytest

from briefcase.integrations.providers import _requests

from tests.contracts import _specs

OPENAI = {
    # spec -> (request schema, the spec's token-limit parameters, n parameter)
    "chat": (_requests.OPENAI_CHAT, "CreateChatCompletionRequest",
             ("max_completion_tokens", "max_tokens"), "n"),
    "responses": (_requests.OPENAI_RESPONSES, "CreateResponse", ("max_output_tokens",), None),
    "completions": (_requests.OPENAI_COMPLETIONS, "CreateCompletionRequest", ("max_tokens",), "n"),
    "embeddings": (_requests.OPENAI_EMBEDDINGS, "CreateEmbeddingRequest", (), None),
}


def _types(node):
    if "anyOf" in node:
        return {t for part in node["anyOf"] for t in _types(part)}
    kind = node.get("type")
    return set(kind) if isinstance(kind, list) else {kind}


@pytest.mark.parametrize("name", list(OPENAI))
def test_openai_token_limit_parameters_match_the_spec(name):
    spec, schema, limits, n_key = OPENAI[name]
    props = _specs.openai_spec().properties(_specs.openai_schema(schema))
    assert spec.model_key in props
    assert tuple(spec.max_keys) == limits
    for key in limits:
        assert "integer" in _types(props[key]), key
    assert not [key for key in props if key.startswith("max_") and "token" in key and key not in limits], \
        "the spec has a token-limit parameter the budget does not read"
    assert spec.n_key == n_key
    if n_key:
        assert "integer" in _types(props[n_key])


def test_openai_chat_prefers_the_current_parameter_over_the_deprecated_one():
    props = _specs.openai_spec().properties(_specs.openai_schema("CreateChatCompletionRequest"))
    assert props["max_tokens"].get("deprecated") is True
    assert "deprecated" not in props["max_completion_tokens"]
    assert _requests.OPENAI_CHAT.max_output_tokens(
        {"max_completion_tokens": 40, "max_tokens": 900}) == 40


def test_openai_defaults_match_the_spec():
    props = _specs.openai_spec().properties(_specs.openai_schema("CreateCompletionRequest"))
    # Legacy completions cap at 16 tokens when max_tokens is omitted.
    assert props["max_tokens"]["default"] == _requests.OPENAI_COMPLETIONS.default_max == 16
    assert _requests.OPENAI_COMPLETIONS.max_output_tokens({"prompt": "x"}) == 16
    # Embeddings produce no output tokens.
    assert _requests.OPENAI_EMBEDDINGS.default_max == 0
    # Chat and Responses have no documented default: the budget falls back
    # to the model's maximum (None here).
    for name in ("CreateChatCompletionRequest", "CreateResponse"):
        props = _specs.openai_spec().properties(_specs.openai_schema(name))
        for key in OPENAI["chat" if "Chat" in name else "responses"][2]:
            assert "default" not in props[key], key
    assert _requests.OPENAI_CHAT.default_max is None and _requests.OPENAI_RESPONSES.default_max is None


@pytest.mark.parametrize("name,request_", [
    ("chat", {"model": "m", "messages": [{"role": "user", "content": "x"}],
              "max_completion_tokens": 40, "n": 3}),
    ("chat", {"model": "m", "messages": [{"role": "user", "content": "x"}], "max_tokens": 40}),
    ("responses", {"model": "m", "input": "x", "max_output_tokens": 40}),
    ("completions", {"model": "m", "prompt": "x", "max_tokens": 40, "n": 2}),
])
def test_openai_spec_valid_requests_yield_their_cap(name, request_):
    spec, schema, _, _ = OPENAI[name]
    assert _specs.openai_spec().errors(_specs.openai_schema(schema), request_, strict=True) == []
    assert spec.max_output_tokens(request_) == 40 * request_.get("n", 1)


def test_anthropic_max_tokens_is_the_required_integer_cap():
    doc = _specs.anthropic_spec()
    for pointer in ("/$defs/MessageCreateParamsNonStreaming", "/$defs/MessageCreateParamsStreaming"):
        schema = doc.resolve(pointer)
        assert "max_tokens" in schema["required"]
        assert schema["properties"]["max_tokens"]["type"] == "integer"
        assert not [k for k in schema["properties"] if k.startswith("max_") and k != "max_tokens"]
    for spec in (_requests.ANTHROPIC_MESSAGES, _requests.ANTHROPIC_STREAM):
        assert spec.max_keys == ("max_tokens",) and spec.model_key == "model"
    request = {"model": "m", "max_tokens": 40, "messages": [{"role": "user", "content": "x"}]}
    assert doc.errors("/$defs/MessageCreateParamsNonStreaming", request, strict=True) == []
    assert _requests.ANTHROPIC_MESSAGES.max_output_tokens(request) == 40


def test_google_cap_is_generation_config_max_output_tokens():
    genai = pytest.importorskip("google.genai")
    from google.genai import models, types

    # The SDK fields the budget reads are what the SDK sends as
    # generationConfig.maxOutputTokens / candidateCount ...
    client = genai.Client(api_key="k")
    config = types.GenerateContentConfig(max_output_tokens=40, candidate_count=2)
    body = models._GenerateContentParameters_to_mldev(
        client._api_client, {"model": "m", "contents": "x", "config": config})
    assert body["generationConfig"] == {"maxOutputTokens": 40, "candidateCount": 2}
    for spec in (_requests.GOOGLE_GENERATE, _requests.GOOGLE_GENERATE_STREAM):
        assert spec.max_output_tokens({"config": config}) == 80
        assert spec.max_output_tokens({"config": {"max_output_tokens": 40}}) == 40
        assert spec.model_key == "model"
    # ... and those are the API's integer token-limit fields.
    discovery = _specs.google_spec(strict=True)
    props = discovery.properties("/$defs/GenerationConfig")
    assert props["maxOutputTokens"]["type"] == "integer"
    assert props["candidateCount"]["type"] == "integer"
    assert discovery.errors("/$defs/GenerateContentRequest", {
        "contents": [{"role": "user", "parts": [{"text": "x"}]}], "generationConfig": body["generationConfig"],
    }, strict=True) == []


def test_bedrock_converse_cap_is_inference_config_max_tokens():
    service = _specs.bedrock_service()
    for operation, spec in (("Converse", _requests.BEDROCK["Converse"]),
                            ("ConverseStream", _requests.BEDROCK["ConverseStream"])):
        shape = service.operation_model(operation).input_shape
        assert spec.model_key in shape.members
        config = shape.members["inferenceConfig"]
        assert config.members["maxTokens"].type_name == "integer"
        assert [m for m in config.members if "max" in m.lower()] == ["maxTokens"]
        request = {"modelId": "m", "messages": [{"role": "user", "content": [{"text": "x"}]}],
                   "inferenceConfig": {"maxTokens": 40}}
        assert _specs.bedrock_errors(operation, request) == []
        assert spec.max_output_tokens(request) == 40


def test_bedrock_invoke_model_cap_lives_in_a_model_specific_body():
    """InvokeModel's body is an opaque blob in the service model: the cap
    keys ``_requests`` reads there (``max_tokens``, ``max_gen_len``,
    ``textGenerationConfig.maxTokenCount``, ...) are each model family's
    own request format, which the AWS spec does not describe. Only their
    location is checked here."""
    service = _specs.bedrock_service()
    for operation in ("InvokeModel", "InvokeModelWithResponseStream"):
        shape = service.operation_model(operation).input_shape
        assert shape.members["body"].type_name == "blob"
        assert _requests.BEDROCK[operation].model_key in shape.members
    spec = _requests.BEDROCK["InvokeModel"]
    assert spec.max_output_tokens({"body": b'{"max_tokens": 40}'}) == 40
    assert spec.max_output_tokens({"body": '{"textGenerationConfig": {"maxTokenCount": 40}}'}) == 40
    assert spec.max_output_tokens({"body": '{"inferenceConfig": {"maxTokens": 40}}'}) == 40
