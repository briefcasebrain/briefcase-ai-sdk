"""briefcase.prompts: stable versions, record tagging through @capture, local registry."""
import asyncio
from unittest.mock import MagicMock

import pytest

from briefcase.decorators import capture
from briefcase.prompts import Prompt, current_prompt, load_saved, prompt, prompt_version


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path))
    return tmp_path


def _exporter():
    exp = MagicMock()
    exp.export = MagicMock(return_value=None)
    return exp


def _records(exp):
    return [c.args[0] for c in exp.export.call_args_list]


def test_version_is_a_stable_short_sha256():
    # Pinned value: changing the hashing would silently split every prompt's history.
    assert prompt_version("Hello {name}") == "833583c57413"
    assert Prompt("greet", "Hello {name}").version == "833583c57413"
    assert prompt_version("Hello {name} ") != "833583c57413"


def test_render_and_validation():
    p = Prompt("greet", "Hello {name}, re: {topic}")
    assert p.render(name="Ada", topic="x") == "Hello Ada, re: x"
    assert p.placeholders == ["name", "topic"]
    assert p.ref == "greet@833583c57413".replace("833583c57413", p.version)
    with pytest.raises(ValueError):
        Prompt("bad name", "x")
    with pytest.raises(TypeError):
        Prompt("ok", 3)


def test_render_inside_capture_tags_the_record():
    tpl = Prompt("triage", "Classify: {text}")
    exp = _exporter()

    @capture(exporter=exp, async_capture=False)
    def classify(text):
        tpl.render(text=text)
        return "bug"

    @capture(exporter=exp, async_capture=False)
    def unrelated():
        return 1

    classify("crash")
    unrelated()
    tagged, plain = _records(exp)
    assert tagged["prompt_name"] == "triage" and tagged["prompt_version"] == tpl.version
    assert "prompt_name" not in plain  # no leak into the next call


def test_with_block_tags_nested_records_and_resets():
    tpl = Prompt("summarize", "Summarize {doc}")
    exp = _exporter()

    @capture(exporter=exp, async_capture=False)
    def inner():
        return "x"

    @capture(exporter=exp, async_capture=False)
    def outer():
        with tpl:
            assert current_prompt() is tpl
            return inner()

    outer()
    assert current_prompt() is None
    inner_rec, outer_rec = _records(exp)
    assert inner_rec["prompt_version"] == tpl.version  # tagged at start (active)
    assert outer_rec["prompt_version"] == tpl.version  # tagged at end (used during the call)


def test_async_capture_is_tagged():
    tpl = Prompt("triage", "Async: {text}")
    exp = _exporter()

    @capture(exporter=exp, async_capture=False)
    async def classify(text):
        tpl.render(text=text)
        return "ok"

    asyncio.run(classify("a"))
    assert _records(exp)[0]["prompt_version"] == tpl.version


def test_registry_saves_each_version_once(home, monkeypatch):
    prompt("triage", "v1 {text}")
    prompt("triage", "v1 {text}")
    prompt("triage", "v2 {text}")
    saved = load_saved()
    assert [e["version"] for e in saved["triage"]] == [prompt_version("v1 {text}"), prompt_version("v2 {text}")]
    path = home / "prompts" / "triage" / f"{prompt_version('v1 {text}')}.json"
    path.write_text(path.read_text().replace("v1 {text}", "edited"))
    assert any(e["tampered"] for e in load_saved()["triage"])
    monkeypatch.setenv("BRIEFCASE_PROMPT_REGISTRY", "off")
    prompt("other", "not saved")
    assert "other" not in load_saved()
