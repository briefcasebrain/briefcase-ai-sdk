"""Every request setting each spec defines is accounted for by replay.

A replay re-sends what the record holds. A setting that changes the answer
must therefore be recorded, or named in ``UNRECORDED_SETTINGS`` so
``replay explain`` warns that the API default is sent instead (the user can
supply it with ``--param``). The remaining settings are listed here, each
with the reason it cannot change the answer that replay compares (the text
and tool calls). A setting a new spec version adds fails this test until it
is classified.
"""

import re

import pytest

from briefcase.integrations.providers import anthropic as anthropic_prov
from briefcase.integrations.providers import bedrock as bedrock_prov
from briefcase.integrations.providers import google_genai as google_prov
from briefcase.integrations.providers import openai as openai_prov
from briefcase.replay._recorded.records import UNRECORDED_SETTINGS

from tests.contracts import _specs

NOT_ANSWER = {
    "metadata": "stored with the request, not seen by the model",
    "user": "end-user id for abuse monitoring",
    "safety_identifier": "end-user id for abuse monitoring",
    "store": "whether the provider stores the output",
    "prompt_cache_key": "cache routing", "prompt_cache_options": "cache routing",
    "prompt_cache_retention": "cache lifetime", "cache_control": "cache breakpoints",
    "stream": "transport", "stream_options": "transport",
    "logprobs": "adds token probabilities, not compared",
    "top_logprobs": "adds token probabilities, not compared",
    "background": "execution mode", "background_options": "execution mode",
    "access_programs": "account access programs",
    "service_tier": "recorded where scalar; latency/price tier",
    "user_profile_id": "account attribution", "workspace_id": "account attribution",
    "diagnostics": "adds diagnostics to the response",
    "performanceConfig": "latency setting", "requestMetadata": "request tags",
    "serviceTier": "latency/price tier", "projectIdentifier": "account attribution",
    "additionalModelResponseFieldPaths": "adds response fields",
    "http_options": "transport", "should_return_http_response": "transport",
    "labels": "billing labels", "response_logprobs": "adds token probabilities, not compared",
    "audio_timestamp": "audio input metadata", "audio_transcription_config": "audio transcription",
    "automatic_continuation": "SDK paging", "continuation_token": "SDK paging",
    "enable_enhanced_civic_answers": "regional feature flag",
    "media_resolution": "media input sampling; media inputs are not replayable",
    "speech_config": "audio output only", "image_config": "image output only",
    "generation_output_config": "output transport options",
    "model_armor_config": "safety screening of the request",
}


def _listed(surface):
    return {re.sub(r"\s*\(.*\)$", "", item) for item in UNRECORDED_SETTINGS[surface]}


def _unaccounted(declared, recorded, listed):
    return sorted(set(declared) - set(recorded) - set(listed) - set(NOT_ANSWER))


@pytest.mark.parametrize("surface,schema,key", [
    ("openai.chat", "CreateChatCompletionRequest", "chat"),
    ("openai.responses", "CreateResponse", "responses"),
    ("openai.completions", "CreateCompletionRequest", "completions"),
])
def test_openai_settings_are_recorded_or_listed(surface, schema, key):
    declared = _specs.openai_spec().properties(_specs.openai_schema(schema))
    meta = openai_prov._SURFACES[key]
    recorded = set(meta["params"]) | set(meta["input_keys"]) | {"model"}
    assert _unaccounted(declared, recorded, _listed(surface)) == []


def test_anthropic_settings_are_recorded_or_listed():
    declared = _specs.anthropic_spec().properties("/$defs/MessageCreateParamsNonStreaming")
    recorded = set(anthropic_prov._PARAMS) | set(anthropic_prov._INPUT_KEYS) | {"model"}
    assert _unaccounted(declared, recorded, _listed("anthropic.messages")) == []


def test_bedrock_settings_are_recorded_or_listed():
    converse = _specs.bedrock_service().operation_model("Converse").input_shape
    listed = _listed("bedrock.converse")
    assert _unaccounted(converse.members, {"modelId", "messages", "system", "inferenceConfig"},
                        {name.split(".")[0] for name in listed}) == []
    inference = converse.members["inferenceConfig"].members
    assert _unaccounted(inference, bedrock_prov._INFERENCE_PARAMS,
                        {name.split(".", 1)[1] for name in listed if "." in name}) == []


def test_google_settings_are_recorded_or_listed():
    types = pytest.importorskip("google.genai.types")
    listed = {name.split(".", 1)[1] for name in _listed("google_genai.generate_content")}
    assert _unaccounted(types.GenerateContentConfig.model_fields,
                        set(google_prov._PARAMS) | {"system_instruction"}, listed) == []
