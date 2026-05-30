"""Tests for stock exporters and the briefcase.observe() one-liner."""

import asyncio
import io
import json

import pytest

import briefcase
from briefcase import BriefcaseConfig
from briefcase.exporters import (
    BaseExporter,
    ConsoleExporter,
    JSONLFileExporter,
    MemoryExporter,
)


@pytest.fixture(autouse=True)
def _reset_config():
    BriefcaseConfig.reset()
    yield
    BriefcaseConfig.reset()


def test_memory_exporter_collects_and_clears():
    mem = MemoryExporter()
    assert asyncio.run(mem.export({"a": 1})) is True
    asyncio.run(mem.export({"b": 2}))
    assert mem.records == [{"a": 1}, {"b": 2}]
    mem.clear()
    assert mem.records == []


def test_jsonl_exporter_writes_valid_lines(tmp_path):
    path = tmp_path / "nested" / "runs.jsonl"  # parent created on demand
    fx = JSONLFileExporter(path)
    asyncio.run(fx.export({"a": 1}))
    asyncio.run(fx.export({"b": 2}))
    asyncio.run(fx.close())
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == [{"a": 1}, {"b": 2}]


def test_console_exporter_writes_json(capsys):
    stream = io.StringIO()
    exp = ConsoleExporter(stream=stream)
    asyncio.run(exp.export({"x": "y"}))
    assert json.loads(stream.getvalue().strip()) == {"x": "y"}


def test_stock_exporters_are_base_exporters():
    for exp in (ConsoleExporter(), MemoryExporter(), JSONLFileExporter("/tmp/x.jsonl")):
        assert isinstance(exp, BaseExporter)


def test_observe_memory_then_capture_end_to_end():
    mem = briefcase.observe("memory")
    assert isinstance(mem, MemoryExporter)

    @briefcase.capture(async_capture=False)
    def classify(text):
        return text.upper()

    assert classify("hello") == "HELLO"
    assert len(mem.records) == 1
    rec = mem.records[0]
    assert rec["function_name"] == "classify"
    assert "decision_id" in rec and "execution_time_ms" in rec


def test_observe_jsonl_shorthand(tmp_path):
    path = tmp_path / "runs.jsonl"
    exp = briefcase.observe(str(path))
    assert isinstance(exp, JSONLFileExporter)

    @briefcase.capture(async_capture=False)
    def fn(x):
        return x

    fn(3)
    asyncio.run(exp.close())
    assert path.exists()
    assert len(path.read_text().splitlines()) == 1


def test_observe_default_is_console():
    exp = briefcase.observe()
    assert isinstance(exp, ConsoleExporter)


def test_observe_unknown_shorthand_raises():
    with pytest.raises(ValueError):
        briefcase.observe("not-a-real-exporter")
