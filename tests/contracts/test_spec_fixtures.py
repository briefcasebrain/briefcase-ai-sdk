"""Provenance of the pinned specs, and drift between them and the installed SDKs.

A vendored spec is only used when its bytes hash to the SHA-256 the
manifest pins (``_specs.vendored_bytes``). The drift checks compare the
pinned copy with what the installed SDK carries; they skip when the
installed SDK is a different version from the one pinned, since a newer
SDK legitimately differs (refresh with ``scripts/refresh_provider_specs.py``).
"""

import importlib.metadata
import re

import pytest

from briefcase.replay._recorded.records import UNRECORDED_SETTINGS

from tests.contracts import _specs


def test_manifest_names_every_file_and_licence():
    specs = _specs.manifest()["specs"]
    assert set(specs) == {"openai-openapi", "anthropic-types", "gemini-discovery-v1beta",
                          "botocore-bedrock-runtime"}
    for name, spec in specs.items():
        for key in ("title", "url", "version", "retrieved", "sha256", "licence", "vendored"):
            assert spec.get(key) not in (None, ""), (name, key)
        assert re.fullmatch(r"[0-9a-f]{64}", spec["sha256"]), name
        if spec["vendored"]:
            assert (_specs.SPEC_DIR / spec["file"]).is_file(), name
            _specs.vendored_bytes(name)  # hash checked
        else:
            assert "cache_file" in spec and "file" not in spec, name
        if spec.get("licence_file"):
            assert (_specs.SPEC_DIR / spec["licence_file"]).is_file(), name


def test_pinned_versions_match_the_documents():
    openai_doc = _specs.load("openai-openapi")
    assert openai_doc["openapi"].startswith("3.1")
    assert openai_doc["info"]["version"] in _specs.entry("openai-openapi")["version"]
    assert openai_doc["info"]["license"]["identifier"] == "MIT"
    model = _specs.load("botocore-bedrock-runtime")
    assert model["metadata"]["apiVersion"] in _specs.entry("botocore-bedrock-runtime")["version"]
    anthropic_doc = _specs.load("anthropic-types")
    assert _specs.entry("anthropic-types")["sdk_version"] in anthropic_doc["title"]


def _installed(dist):
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def test_anthropic_types_snapshot_matches_the_installed_sdk():
    pytest.importorskip("anthropic")
    pinned = _specs.entry("anthropic-types")["sdk_version"]
    if _installed("anthropic") != pinned:
        pytest.skip(f"anthropic {_installed('anthropic')} installed, snapshot is of {pinned}")
    fresh = _specs.generate_anthropic_schemas()["$defs"]
    pinned_defs = _specs.load("anthropic-types")["$defs"]
    # Compare what the contract is made of (definitions and their property
    # names), not pydantic's rendering details, which vary by version.
    assert set(fresh) == set(pinned_defs)
    for name, schema in fresh.items():
        assert set(schema.get("properties", {})) == set(pinned_defs[name].get("properties", {})), name


def test_botocore_model_matches_the_installed_botocore():
    botocore = pytest.importorskip("botocore")
    version = _specs.entry("botocore-bedrock-runtime")["version"]
    if botocore.__version__ not in version:
        pytest.skip(f"botocore {botocore.__version__} installed, the pinned model is from {version}")
    from botocore.loaders import Loader

    installed = Loader().load_service_model("bedrock-runtime", "service-2")
    assert installed == _specs.load("botocore-bedrock-runtime")


# -- names the replay documentation gives for settings capture does not record ---

def _names(surface):
    return [re.sub(r"\s*\(.*\)$", "", item) for item in UNRECORDED_SETTINGS[surface]]


def test_unrecorded_settings_named_by_replay_exist_in_the_specs():
    openai = _specs.openai_spec()
    for surface, schema in (("openai.chat", "CreateChatCompletionRequest"),
                            ("openai.responses", "CreateResponse"),
                            ("openai.completions", "CreateCompletionRequest")):
        declared = set(openai.properties(_specs.openai_schema(schema)))
        assert set(_names(surface)) <= declared, (surface, set(_names(surface)) - declared)
    declared = set(_specs.anthropic_spec().properties("/$defs/MessageCreateParamsNonStreaming"))
    assert set(_names("anthropic.messages")) <= declared
    converse = _specs.bedrock_service().operation_model("Converse").input_shape
    for name in _names("bedrock.converse"):
        head, _, tail = name.partition(".")
        assert head in converse.members, name
        if tail:
            assert tail in converse.members[head].members, name
    types = pytest.importorskip("google.genai.types")
    for name in _names("google_genai.generate_content"):
        assert name.split(".", 1)[1] in types.GenerateContentConfig.model_fields, name
