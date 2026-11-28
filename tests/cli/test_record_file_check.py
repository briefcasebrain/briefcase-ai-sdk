"""Explicit local verification reads only the selected export and never renders its contents."""
import io
import json
import os
from pathlib import Path

import pytest

from briefcase.cli import _record_file
from briefcase.cli.__main__ import main
from briefcase.cli.record_help_commands import _TABLE


SPEC = json.loads(_TABLE.read_text())["localFileCheck"]
CASES = json.loads((Path(__file__).parent / "fixtures/record_file_check_v1.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_selected_file_cli(case, tmp_path, monkeypatch, capsys):
    selected = tmp_path / "private-selection"
    selected.write_bytes(bytes.fromhex(case["bytesHex"]) if "bytesHex" in case else case["body"].encode())
    monkeypatch.setattr("sys.stdin", io.StringIO(case["id"] + "\n"))
    assert main(["record-help", "--verify-jsonl", str(selected)]) == int(case["expected"] == "file_check_incomplete")
    output = capsys.readouterr()
    assert output.out == SPEC["actions"][case["expected"]] + "\n"
    assert not output.err


@pytest.mark.parametrize("change", ["append", "truncate", "replace"])
def test_mutation_vetoes_even_observed_match(change, tmp_path, monkeypatch):
    selected = tmp_path / "chosen"
    data = b'{"decision_id":"one"}\n'
    selected.write_bytes(data)
    scan = _record_file._scan

    def modified(file, size, target, limits):
        result = scan(file, size, target, limits)
        if change == "replace":
            new = tmp_path / "replacement"
            new.write_bytes(data)
            new.replace(selected)
        else:
            selected.write_bytes(data * 2 if change == "append" else b"")
        return result

    monkeypatch.setattr(_record_file, "_scan", modified)
    assert _record_file.verify(selected, "one", SPEC["limits"]) == "file_check_incomplete"


def test_size_row_and_line_bounds(tmp_path):
    path = tmp_path / "selected"
    data = b'{"decision_id":"one"}'
    path.write_bytes(data + b"\r\n")
    limits = dict(lineBytes=len(data), fileBytes=len(data) + 2, rows=1, depth=1)
    assert _record_file.verify(path, "one", limits) == "file_check_one"
    for field in limits:
        assert _record_file.verify(path, "one", {**limits, field: limits[field] - 1}) == "file_check_incomplete"


@pytest.mark.parametrize("kind", ["directory", "missing", "symlink", "fifo"])
def test_only_regular_files(kind, tmp_path):
    path = tmp_path / "selected"
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        original = tmp_path / "original"
        original.write_text('{"decision_id":"one"}')
        try:
            path.symlink_to(original)
        except OSError:
            pytest.skip("Symlinks unavailable")
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("Named pipes unavailable")
        os.mkfifo(path)
    assert _record_file.verify(path, "one", SPEC["limits"]) == "file_check_incomplete"


@pytest.mark.parametrize("answer", ["", "\n", "one\ntwo\n", "one\n\n"])
def test_invalid_id_input_does_not_open_selection(answer, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(answer))
    monkeypatch.setattr(_record_file, "verify", lambda *_: pytest.fail("Unexpected read"))
    assert main(["record-help", "--verify-jsonl", "secret-file"]) == 1
    assert capsys.readouterr().out.strip() == SPEC["actions"]["file_check_incomplete"]


@pytest.mark.parametrize("flags", [
    ["secret-question"], ["--static"], ["--answer", "secret-answer"], ["--source", "jsonl"],
    ["--verify-jsonl", "second-secret-file"], ["--evidence-reply", "yes"],
])
def test_incompatible_modes_never_open_or_echo(flags, monkeypatch, capsys):
    monkeypatch.setattr(_record_file, "verify", lambda *_: pytest.fail("Unexpected read"))
    monkeypatch.setattr("sys.stdin", io.StringIO("private-id\n"))
    assert main(["record-help", "--verify-jsonl", "secret-file", *flags]) == 2
    output = capsys.readouterr()
    assert not output.out and "secret" not in output.err and "private-id" not in output.err


def test_prose_does_not_open_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_record_file, "verify", lambda *_: pytest.fail("Unexpected read"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["record-help", f"The original exporter wrote {tmp_path}/secret.jsonl. Where is its record?"]) == 0
    out = capsys.readouterr().out
    assert "secret.jsonl" not in out and SPEC["cliDiscovery"] in out
