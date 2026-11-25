"""The feature manifests this package ships: required keys, the docs page exists, and the first
command parses with the real, auto-discovered parser."""

import json
import shlex
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REQUIRED = ("id", "title", "verb", "one_line", "command", "docs_slug", "status")


@pytest.mark.parametrize("name", ["policy-hooks", "managed-rollout"])
def test_manifest(name):
    from briefcase.cli import policy_commands
    from briefcase.cli.__main__ import build_parser

    data = json.loads((REPO / "briefcase" / "_features" / f"{name}.json").read_text(encoding="utf-8"))
    assert all(isinstance(data.get(key), str) and data[key] for key in REQUIRED)
    assert data["id"] == name and data["docs_slug"] == f"{data['verb']}/{name}"
    assert data["verb"] in ("connect", "capture", "enforce", "verify", "review")
    assert data["status"] == "experimental" and data["verified_against"]
    assert len(data["one_line"]) < 80 and data["one_line"].endswith(".")
    assert (REPO / data.get("docs_path", f"docs/features/{name}.md")).exists()
    parser = build_parser()
    words = shlex.split(data["command"])
    assert words[0] == "briefcase-ai"
    parser.parse_args(words[1:])
    if data.get("status_id"):
        assert data["status_id"] == policy_commands.FEATURE_ID
