"""Tests for stock exporters and the briefcase.observe() one-liner."""

import asyncio
import io
import json
import stat
import sys

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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_jsonl_exporter_creates_private_dirs_and_file(tmp_path):
    path = tmp_path / "a" / "b" / "runs.jsonl"
    fx = JSONLFileExporter(path)
    asyncio.run(fx.export({"a": 1}))
    asyncio.run(fx.close())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_jsonl_exporter_tightens_existing_file_mode(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text('{"old": 1}\n')
    path.chmod(0o644)
    fx = JSONLFileExporter(path)
    asyncio.run(fx.export({"new": 2}))
    asyncio.run(fx.close())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == [{"old": 1}, {"new": 2}]


def test_memory_exporter_max_records_keeps_the_newest():
    mem = MemoryExporter(max_records=3)
    for i in range(5):
        asyncio.run(mem.export({"i": i}))
    assert [r["i"] for r in mem.records] == [2, 3, 4]
    assert mem.dropped == 2
    assert mem.records[0] == {"i": 2}
    mem.clear()
    assert len(mem.records) == 0


def test_memory_exporter_is_unbounded_by_default():
    mem = MemoryExporter()
    assert mem.max_records is None
    assert isinstance(mem.records, list)
    with pytest.raises(ValueError):
        MemoryExporter(max_records=0)


def test_observe_http_url_builds_an_http_exporter():
    exporter = briefcase.observe("https://collector.example.com/v1/decisions")
    assert isinstance(exporter, briefcase.HTTPExporter)
    assert BriefcaseConfig.get().exporter is exporter
    with pytest.raises(ValueError, match="cleartext"):
        briefcase.observe("http://collector.example.com/v1/decisions")


def test_exporter_classes_and_auto_are_exported_at_top_level():
    import briefcase.auto as auto_module

    for name in ("BaseExporter", "ConsoleExporter", "JSONLFileExporter",
                 "MemoryExporter", "HTTPExporter", "OTelExporter",
                 "GCPCloudLoggingExporter"):
        assert getattr(briefcase, name) is getattr(briefcase.exporters, name)
    # briefcase.auto stays the submodule and is also callable.
    assert briefcase.auto is auto_module
    assert callable(briefcase.auto) and callable(briefcase.auto.undo)
    with pytest.raises(ValueError):
        briefcase.auto("no-such-framework")
