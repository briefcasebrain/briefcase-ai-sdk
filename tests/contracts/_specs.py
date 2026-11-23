"""Load the pinned provider API specifications and validate against them.

Every spec is described in ``specs/manifest.json`` (source URL, retrieval
date, version, SHA-256, licence, whether it is vendored). Vendored files are
checked against their pinned SHA-256 on load, so a test can never validate
against a spec other than the one the manifest names.

Specs whose licence does not allow redistribution (the Gemini API discovery
document) are downloaded at test time into a cache directory
(``$BRIEFCASE_CONTRACTS_CACHE``, default ``~/.cache/briefcase-contracts``)
and the tests that need them skip when the cache is empty and the network is
unavailable (or ``BRIEFCASE_CONTRACTS_OFFLINE=1``).

Validation is JSON Schema 2020-12 (``jsonschema`` + ``referencing``), with
``$ref`` resolved inside each document. Bedrock is validated with botocore's
own ``ParamValidator`` against the pinned service model, which is how the
AWS SDK itself checks a request.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

import pytest

SPEC_DIR = Path(__file__).parent / "specs"
MANIFEST_PATH = SPEC_DIR / "manifest.json"
ENV_CACHE = "BRIEFCASE_CONTRACTS_CACHE"
ENV_OFFLINE = "BRIEFCASE_CONTRACTS_OFFLINE"


@functools.lru_cache(maxsize=None)
def manifest() -> Dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def entry(name: str) -> Dict[str, Any]:
    return manifest()["specs"][name]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(doc: Any) -> str:
    """SHA-256 of a document's canonical JSON (the pin of a spec that is
    downloaded, not vendored: its server varies the key order)."""
    return sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())


def vendored_bytes(name: str) -> bytes:
    """The uncompressed bytes of a vendored spec, checked against the
    manifest's SHA-256."""
    spec = entry(name)
    assert spec["vendored"], f"{name} is not vendored"
    raw = (SPEC_DIR / spec["file"]).read_bytes()
    data = gzip.decompress(raw) if spec["file"].endswith(".gz") else raw
    digest = sha256(data)
    assert digest == spec["sha256"], (
        f"{spec['file']} has sha256 {digest}, the manifest pins {spec['sha256']}; "
        "refresh both with scripts/refresh_provider_specs.py --write"
    )
    return data


@functools.lru_cache(maxsize=None)
def load(name: str) -> Any:
    return json.loads(vendored_bytes(name))


# -- downloaded (not vendored) specs ------------------------------------------

def cache_dir() -> Path:
    return Path(os.environ.get(ENV_CACHE) or Path.home() / ".cache" / "briefcase-contracts")


def fetch(url: str, timeout: float = 20.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "briefcase-contract-tests"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URLs
        return response.read()


@functools.lru_cache(maxsize=None)
def downloaded(name: str) -> Optional[Any]:
    """A non-vendored spec from the cache, downloading it once when allowed.
    ``None`` when it is not cached and cannot be fetched."""
    spec = entry(name)
    path = cache_dir() / spec["cache_file"]
    if not path.exists():
        if os.environ.get(ENV_OFFLINE):
            return None
        try:
            data = fetch(spec["url"])
            json.loads(data)
        except Exception:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    try:
        return json.loads(path.read_bytes())
    except ValueError:
        return None


def require_downloaded(name: str) -> Any:
    doc = downloaded(name)
    if doc is None:
        pytest.skip(
            f"{name} is not vendored (licence: {entry(name)['licence']}) and is not in the cache "
            f"({cache_dir()}); run with network access once, or scripts/refresh_provider_specs.py"
        )
    return doc


# -- JSON Schema validation -----------------------------------------------------

def _registry(uri: str, doc: Any) -> Any:
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    return Registry().with_resource(uri, Resource.from_contents(doc, default_specification=DRAFT202012))


class SchemaDoc:
    """One JSON Schema (or OpenAPI 3.1) document and validators for the
    schemas inside it, addressed by JSON pointer."""

    def __init__(self, uri: str, doc: Any) -> None:
        self.uri = uri
        self.doc = doc
        self.registry = _registry(uri, doc)

    def resolve(self, pointer: str) -> Any:
        node = self.doc
        for part in pointer.lstrip("#/").split("/"):
            if part:
                node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def errors(self, pointer: str, value: Any, *, strict: bool = False) -> List[str]:
        """Validation errors of ``value`` against the schema at ``pointer``.
        ``strict`` also rejects top-level properties the schema does not
        declare (``unevaluatedProperties: false``), which is how a misnamed
        request parameter is caught: most API schemas leave
        ``additionalProperties`` open."""
        from jsonschema import Draft202012Validator

        schema: Dict[str, Any] = {"$ref": f"{self.uri}#{pointer}"}
        if strict:
            schema["unevaluatedProperties"] = False
        validator = Draft202012Validator(schema, registry=self.registry)
        return sorted(f"{e.json_path}: {e.message[:300]}" for e in validator.iter_errors(value))

    def _expand(self, node: Any, depth: int = 0) -> List[Dict[str, Any]]:
        """The concrete schemas a node stands for: ``$ref`` followed,
        ``anyOf`` / ``oneOf`` / ``allOf`` flattened."""
        if not isinstance(node, dict) or depth > 30:
            return []
        if "$ref" in node:
            return self._expand(self.resolve(node["$ref"].split("#", 1)[1]), depth + 1)
        out = [node]
        for key in ("anyOf", "oneOf", "allOf"):
            for part in node.get(key, ()):
                out.extend(self._expand(part, depth + 1))
        return out

    def field_types(self, pointer: str, path: Sequence[str]) -> Set[str]:
        """JSON types a value at ``path`` (property names; ``"[]"`` for an
        array item) may have under the schema at ``pointer``. Empty when
        the spec does not define the field."""
        nodes = self._expand(self.resolve(pointer))
        for key in path:
            nxt: List[Dict[str, Any]] = []
            for node in nodes:
                if key == "[]":
                    if "items" in node:
                        nxt.extend(self._expand(node["items"]))
                elif key in (node.get("properties") or {}):
                    nxt.extend(self._expand(node["properties"][key]))
            nodes = nxt
        types: Set[str] = set()
        for node in nodes:
            kind = node.get("type")
            if isinstance(kind, str):
                types.add(kind)
            elif isinstance(kind, list):
                types.update(kind)
            for value in node.get("enum", ()) or ():
                types.add({str: "string", int: "integer", float: "number", bool: "boolean",
                           type(None): "null"}.get(type(value), "string"))
            if "const" in node:
                types.add("string" if isinstance(node["const"], str) else "other")
        return types

    def properties(self, pointer: str) -> Dict[str, Any]:
        """The declared properties of an object schema, following ``$ref``
        and ``allOf``."""
        out: Dict[str, Any] = {}

        def walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            if "$ref" in node:
                walk(self.resolve(node["$ref"].split("#", 1)[1]))
            for part in node.get("allOf", ()):
                walk(part)
            out.update(node.get("properties", {}))

        walk(self.resolve(pointer))
        return out


@functools.lru_cache(maxsize=None)
def openai_spec() -> SchemaDoc:
    return SchemaDoc("urn:openai-openapi", load("openai-openapi"))


def openai_schema(name: str) -> str:
    return f"/components/schemas/{name}"


def openai_operation(path: str) -> Dict[str, str]:
    """``{"request", "json", "stream"}`` schema pointers of a POST path."""
    op = openai_spec().doc["paths"][path]["post"]
    request = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    responses = op["responses"]["200"]["content"]
    out = {"request": request.split("#", 1)[1],
           "json": responses["application/json"]["schema"]["$ref"].split("#", 1)[1]}
    if "text/event-stream" in responses:
        out["stream"] = responses["text/event-stream"]["schema"]["$ref"].split("#", 1)[1]
    return out


# -- Anthropic: JSON Schema generated from the installed SDK's types ------------

#: name -> dotted path of the SDK type the schema is generated from.
ANTHROPIC_TYPES = {
    "MessageCreateParamsNonStreaming":
        "anthropic.types.message_create_params:MessageCreateParamsNonStreaming",
    "MessageCreateParamsStreaming":
        "anthropic.types.message_create_params:MessageCreateParamsStreaming",
    "Message": "anthropic.types:Message",
    "RawMessageStreamEvent": "anthropic.types:RawMessageStreamEvent",
}


def generate_anthropic_schemas() -> Dict[str, Any]:
    """One document with a ``$defs`` entry per type in ``ANTHROPIC_TYPES``,
    as pydantic renders the installed ``anthropic`` package's types."""
    import importlib

    import anthropic
    from pydantic import TypeAdapter

    defs: Dict[str, Any] = {}
    roots: Dict[str, Any] = {}
    for name, dotted in ANTHROPIC_TYPES.items():
        module, attr = dotted.split(":")
        tp = getattr(importlib.import_module(module), attr)
        schema = TypeAdapter(tp).json_schema(ref_template="#/$defs/{model}")
        defs.update(schema.pop("$defs", {}))
        roots[name] = schema
    for name, schema in roots.items():
        defs[name] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"anthropic {anthropic.__version__} request/response types",
        "$defs": dict(sorted(defs.items())),
    }


def strict_objects(node: Any) -> Any:
    """Copy of a pydantic-generated schema with ``additionalProperties:
    false`` on every object that declares properties and says nothing
    else. Safe there: pydantic expresses unions with ``anyOf`` /
    ``oneOf`` of whole objects, never with ``allOf`` fragments."""
    if isinstance(node, list):
        return [strict_objects(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: strict_objects(value) for key, value in node.items()}
    if "properties" in out and out.get("additionalProperties") in (None, True):
        out["additionalProperties"] = False
    return out


@functools.lru_cache(maxsize=None)
def anthropic_spec(strict: bool = False) -> SchemaDoc:
    doc = load("anthropic-types")
    return SchemaDoc("urn:anthropic-types" + (":strict" if strict else ""),
                     strict_objects(doc) if strict else doc)


# -- Google: discovery document -> JSON Schema ------------------------------------

def discovery_to_jsonschema(discovery: Dict[str, Any], strict: bool = True) -> Dict[str, Any]:
    """The ``schemas`` of a Google API discovery document as JSON Schema
    ``$defs``. Discovery ``$ref`` values are schema ids; ``int64`` /
    ``uint64`` are JSON strings in proto3 JSON but numbers are accepted on
    input, so both are allowed. With ``strict`` every object with declared
    properties rejects others (discovery has no ``allOf``, so this is safe),
    which catches a field name the API does not define."""

    def convert(node: Dict[str, Any]) -> Dict[str, Any]:
        if "$ref" in node:
            return {"$ref": f"#/$defs/{node['$ref']}"}
        kind = node.get("type")
        if kind == "object" or "properties" in node:
            out: Dict[str, Any] = {"type": "object"}
            props = {k: convert(v) for k, v in node.get("properties", {}).items()}
            if props:
                out["properties"] = props
            if "additionalProperties" in node:
                out["additionalProperties"] = convert(node["additionalProperties"])
            elif strict and props:
                out["additionalProperties"] = False
            return out
        if kind == "array":
            return {"type": "array", "items": convert(node.get("items", {}))}
        if kind == "string":
            if node.get("format") in ("int64", "uint64"):
                return {"type": ["string", "integer"]}
            out = {"type": "string"}
            if "enum" in node:
                out["enum"] = list(node["enum"])
            if node.get("format"):
                out["format"] = node["format"]
            return out
        if kind in ("integer", "number", "boolean"):
            return {"type": kind}
        return {}

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {name: convert(schema) for name, schema in discovery["schemas"].items()},
    }


@functools.lru_cache(maxsize=None)
def google_spec(strict: bool = True) -> SchemaDoc:
    discovery = require_downloaded("gemini-discovery-v1beta")
    return SchemaDoc("urn:gemini-discovery" + (":strict" if strict else ""),
                     discovery_to_jsonschema(discovery, strict=strict))


# -- Bedrock: botocore service model ------------------------------------------------

@functools.lru_cache(maxsize=None)
def bedrock_service() -> Any:
    from botocore.model import ServiceModel

    return ServiceModel(load("botocore-bedrock-runtime"), service_name="bedrock-runtime")


def bedrock_errors(operation: str, params: Dict[str, Any]) -> List[str]:
    """botocore's own request validation against the pinned service model."""
    from botocore.validate import ParamValidator

    shape = bedrock_service().operation_model(operation).input_shape
    report = ParamValidator().validate(params, shape)
    return [report.generate_report()] if report.has_errors() else []
