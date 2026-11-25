#!/usr/bin/env python3
"""Re-download the provider API specs the contract tests pin and report drift.

    python scripts/refresh_provider_specs.py            # report; exit 1 on drift
    python scripts/refresh_provider_specs.py --json     # the same as JSON
    python scripts/refresh_provider_specs.py --write    # update tests/contracts/specs + manifest

For each spec in ``tests/contracts/specs/manifest.json``:

- ``openai-openapi``: the latest ``openapi.json`` of github.com/openai/openai-openapi.
- ``gemini-discovery-v1beta``: the live discovery document (never vendored;
  ``--write`` refreshes the pin and the local cache only).
- ``botocore-bedrock-runtime``: the service model on botocore's ``develop`` branch.
- ``anthropic-types``: regenerated from the *installed* ``anthropic`` package
  (Anthropic publishes no machine-readable spec); install the release to check
  first, e.g. ``pip install -U anthropic``.

Drift is reported as the SHA-256 change plus, for the schemas the
instrumentation and replay use, the request / response / usage field names
that appeared or disappeared, so a reviewer sees what moved. After
``--write``, run ``pytest tests/contracts`` to see whether the code still
matches the new specs.

Exit codes: 0 no drift, 1 drift found, 2 a spec could not be fetched.
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO = Path(__file__).resolve().parent.parent
SPEC_DIR = REPO / "tests" / "contracts" / "specs"
MANIFEST = SPEC_DIR / "manifest.json"

OPENAI_SCHEMAS = ("CreateChatCompletionRequest", "CreateChatCompletionResponse",
                  "CreateChatCompletionStreamResponse", "CompletionUsage", "CreateResponse",
                  "Response", "ResponseUsage", "CreateCompletionRequest", "CreateCompletionResponse",
                  "CreateEmbeddingRequest", "CreateEmbeddingResponse")
GEMINI_SCHEMAS = ("GenerateContentRequest", "GenerationConfig", "GenerateContentResponse",
                  "UsageMetadata", "Candidate", "Part", "Content")
BEDROCK_SHAPES = ("ConverseRequest", "ConverseResponse", "InferenceConfiguration", "TokenUsage",
                  "ConverseStreamOutput", "ConverseStreamMetadataEvent", "InvokeModelRequest")
ANTHROPIC_DEFS = ("MessageCreateParamsNonStreaming", "Message", "Usage", "MessageDeltaUsage",
                  "RawMessageStreamEvent")


def fetch(url: str, timeout: float = 60.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "briefcase-spec-refresh"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URLs
        return response.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(data: bytes) -> str:
    """SHA-256 of the canonical JSON (sorted keys, compact): used for specs
    that are not vendored, whose server returns the same document with a
    varying key order."""
    doc = json.loads(data)
    return sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())


def pinned_bytes(spec: Dict[str, Any]) -> Optional[bytes]:
    if not spec.get("vendored"):
        return None
    raw = (SPEC_DIR / spec["file"]).read_bytes()
    return gzip.decompress(raw) if spec["file"].endswith(".gz") else raw


# -- field names per schema, for a readable drift report -------------------------

def _openapi_fields(doc: Any) -> Dict[str, Set[str]]:
    schemas = doc.get("components", {}).get("schemas", {})

    def props(name: str, seen: Optional[Set[str]] = None) -> Set[str]:
        seen = seen or set()
        if name in seen or name not in schemas:
            return set()
        seen.add(name)
        out: Set[str] = set()
        stack = [schemas[name]]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            if "$ref" in node:
                out |= props(node["$ref"].rsplit("/", 1)[-1], seen)
            stack.extend(node.get("allOf", ()))
            out |= set(node.get("properties", {}))
        return out

    return {name: props(name) for name in OPENAI_SCHEMAS}


def _discovery_fields(doc: Any) -> Dict[str, Set[str]]:
    schemas = doc.get("schemas", {})
    return {name: set(schemas.get(name, {}).get("properties", {})) for name in GEMINI_SCHEMAS}


def _botocore_fields(doc: Any) -> Dict[str, Set[str]]:
    shapes = doc.get("shapes", {})
    return {name: set(shapes.get(name, {}).get("members", {})) for name in BEDROCK_SHAPES}


def _anthropic_fields(doc: Any) -> Dict[str, Set[str]]:
    defs = doc.get("$defs", {})
    return {name: set(defs.get(name, {}).get("properties", {})) for name in ANTHROPIC_DEFS}


FIELDS = {
    "openai-openapi": _openapi_fields,
    "gemini-discovery-v1beta": _discovery_fields,
    "botocore-bedrock-runtime": _botocore_fields,
    "anthropic-types": _anthropic_fields,
}


def field_diff(name: str, old: Any, new: Any) -> Dict[str, Dict[str, List[str]]]:
    before, after = FIELDS[name](old), FIELDS[name](new)
    out: Dict[str, Dict[str, List[str]]] = {}
    for key in sorted(set(before) | set(after)):
        added = sorted(after.get(key, set()) - before.get(key, set()))
        removed = sorted(before.get(key, set()) - after.get(key, set()))
        if added or removed:
            out[key] = {"added": added, "removed": removed}
    return out


# -- latest copies ----------------------------------------------------------------

def latest(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """``{"data": bytes, "version": str, "url": str}`` of the newest copy."""
    if name == "anthropic-types":
        sys.path.insert(0, str(REPO))
        from tests.contracts._specs import generate_anthropic_schemas

        doc = generate_anthropic_schemas()
        import anthropic

        data = json.dumps(doc, indent=1, sort_keys=True).encode()
        return {"data": data, "version": f"anthropic {anthropic.__version__}",
                "url": f"https://pypi.org/project/anthropic/{anthropic.__version__}/",
                "sdk_version": anthropic.__version__}
    data = fetch(spec["latest_url"])
    doc = json.loads(data)
    if name == "openai-openapi":
        commit = _git_head(spec.get("git"))
        url = spec["latest_url"]
        if commit:
            url = f"https://raw.githubusercontent.com/openai/openai-openapi/{commit}/openapi.json"
        return {"data": data, "url": url, "commit": commit,
                "version": f"info.version {doc['info']['version']} (OpenAPI {doc['openapi']})"}
    if name == "gemini-discovery-v1beta":
        return {"data": data, "url": spec["latest_url"], "version": f"revision {doc.get('revision')}"}
    import re

    botocore_version = None
    try:
        init = fetch("https://raw.githubusercontent.com/boto/botocore/develop/botocore/__init__.py")
        match = re.search(rb"__version__ = '([^']+)'", init)
        botocore_version = match.group(1).decode() if match else None
    except Exception:
        pass
    version = f"apiVersion {doc['metadata']['apiVersion']}, botocore {botocore_version or 'develop'}"
    url = spec["latest_url"]
    if botocore_version:
        url = url.replace("/develop/", f"/{botocore_version}/")
    return {"data": data, "url": url, "version": version}


def _git_head(remote: Optional[str]) -> Optional[str]:
    if not remote:
        return None
    try:
        out = subprocess.run(["git", "ls-remote", remote, "HEAD"], capture_output=True, text=True,
                             timeout=60, check=True).stdout
        return out.split()[0] if out.strip() else None
    except Exception:
        return None


def cache_dir() -> Path:
    import os

    return Path(os.environ.get("BRIEFCASE_CONTRACTS_CACHE") or Path.home() / ".cache" / "briefcase-contracts")


def check(write: bool) -> Dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    today = datetime.date.today().isoformat()
    report: Dict[str, Any] = {"checked": today, "specs": {}}
    for name, spec in manifest["specs"].items():
        row: Dict[str, Any] = {"pinned_sha256": spec["sha256"], "pinned_version": spec["version"]}
        report["specs"][name] = row
        try:
            new = latest(name, spec)
        except Exception as exc:  # network, missing SDK
            row["error"] = f"{type(exc).__name__}: {exc}"
            continue
        digest = sha256(new["data"]) if spec.get("vendored") else canonical_sha256(new["data"])
        row.update(latest_sha256=digest, latest_version=new["version"], drift=digest != spec["sha256"])
        if row["drift"]:
            old_bytes = pinned_bytes(spec)
            if old_bytes is None:
                cached = cache_dir() / spec["cache_file"]
                old_bytes = cached.read_bytes() if cached.exists() else None
            if old_bytes is not None:
                row["fields"] = field_diff(name, json.loads(old_bytes), json.loads(new["data"]))
        if write and row["drift"]:
            if spec.get("vendored"):
                (SPEC_DIR / spec["file"]).write_bytes(gzip.compress(new["data"], 9, mtime=0))
            else:
                path = cache_dir() / spec["cache_file"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(new["data"])
            spec.update(sha256=digest, version=new["version"], url=new["url"], retrieved=today)
            if "sdk_version" in new:
                spec["sdk_version"] = new["sdk_version"]
    if write:
        manifest["retrieved"] = today
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return report


def render(report: Dict[str, Any]) -> List[str]:
    lines = [f"provider spec drift check, {report['checked']}"]
    for name, row in report["specs"].items():
        if "error" in row:
            lines.append(f"  {name}: could not check ({row['error']})")
            continue
        if not row["drift"]:
            lines.append(f"  {name}: unchanged ({row['pinned_version']})")
            continue
        lines.append(f"  {name}: CHANGED {row['pinned_version']} -> {row['latest_version']} "
                     f"(sha256 {row['pinned_sha256'][:12]} -> {row['latest_sha256'][:12]})")
        for schema, diff in (row.get("fields") or {}).items():
            if diff["added"]:
                lines.append(f"      {schema}: + {', '.join(diff['added'])}")
            if diff["removed"]:
                lines.append(f"      {schema}: - {', '.join(diff['removed'])}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true",
                        help="update the vendored specs, the cache and the manifest")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    report = check(args.write)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("\n".join(render(report)))
        if args.write:
            print("Specs updated. Next: pytest tests/contracts")
    rows = report["specs"].values()
    if any("error" in row for row in rows):
        return 2
    return 1 if any(row.get("drift") for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
