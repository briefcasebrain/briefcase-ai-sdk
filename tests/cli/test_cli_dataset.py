"""`briefcase dataset` (versioned datasets from records), `eval run` and `prompt`.

The commands are registered the way the CLI's auto-discovery does it: the
built-in parser first, then each module's ``register(subparsers)``.
"""
import argparse
import json
import os
import stat
import sys
import textwrap

import pytest

from briefcase.cli import __main__ as cli_main
from briefcase.cli import dataset_versions, eval_commands, prompt_commands
from briefcase.cli.state import Store
from briefcase.datasets import DatasetStore

MODULES = (eval_commands, prompt_commands)
_BUILTIN_PARSER = cli_main.build_parser


def _subparsers(parser):
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def _extended_parser():
    """The built-in parser plus what this package adds: the one-line
    `extend_dataset_parser(ds)` call in the dataset command's registration and
    the discovered `eval` / `prompt` modules."""
    parser = _BUILTIN_PARSER()
    assert dataset_versions.extend_from_top_level(_subparsers(parser))
    for module in MODULES:
        module.register(_subparsers(parser))
    return parser


def run(argv, home, monkeypatch, capsys):
    monkeypatch.setattr(cli_main, "build_parser", _extended_parser)
    rc = cli_main.main(argv, store=Store(home=home))
    out = capsys.readouterr()
    return rc, out.out, out.err


# -- parser snapshot: the pre-existing command surface is untouched ------------

def _snapshot(parser, prefix=()):
    """{command path: (sorted action signatures, handler)} for every (sub)parser."""
    snap = {}
    rows = []
    sub = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub = action
            continue
        if isinstance(action, argparse._HelpAction):
            continue
        rows.append((tuple(action.option_strings), action.dest, str(action.nargs), bool(action.required),
                     repr(action.default), repr(action.choices), getattr(action.type, "__name__", None)))
    snap[prefix] = (sorted(rows), parser.get_default("func"))
    if sub is not None:
        for name, child in sub.choices.items():
            snap.update(_snapshot(child, prefix + (name,)))
    return snap


def test_existing_parser_surface_is_preserved():
    before = _snapshot(_BUILTIN_PARSER())
    after = _snapshot(_extended_parser())
    assert ("dataset", "register") in before and ("dataset", "list") in before
    for path, (rows, func) in before.items():
        assert path in after, f"lost command {' '.join(path)}"
        after_rows, after_func = after[path]
        if path == ("dataset", "list"):
            # Only additive: --json, and a handler that prints the old lines first.
            assert set(rows) <= set(after_rows)
            assert {r[0] for r in set(after_rows) - set(rows)} == {("--json",)}
            assert after_func is dataset_versions.cmd_list
            continue
        assert after_rows == rows, f"flags changed on {' '.join(path)}"
        assert after_func is func, f"handler changed on {' '.join(path)}"
    new = set(after) - set(before)
    assert {("dataset", s) for s in ("from-records", "show", "diff", "export", "import")} <= new
    assert ("eval", "run") in new and ("prompt", "list") in new


def test_extension_is_idempotent_and_never_creates_dataset():
    parser = _extended_parser()
    assert dataset_versions.extend_from_top_level(_subparsers(parser))  # second time: no conflict
    for module in MODULES:
        module.register(_subparsers(parser))
    bare = argparse.ArgumentParser()
    sub = bare.add_subparsers(dest="command")
    assert dataset_versions.extend_from_top_level(sub) is False
    assert "dataset" not in sub.choices  # the job commands own `dataset`


def test_extension_on_a_fresh_dataset_subparsers():
    parser = argparse.ArgumentParser()
    ds = parser.add_subparsers(dest="command").add_parser("dataset").add_subparsers(dest="sub")
    dataset_versions.extend_dataset_parser(ds)
    args = parser.parse_args(["dataset", "from-records", "x.jsonl", "--name", "n"])
    assert args.func is dataset_versions.cmd_from_records
    assert parser.parse_args(["dataset", "list", "--json"]).func is dataset_versions.cmd_list


def test_modules_declare_review_group_and_status_hook():
    assert all(m.GROUP == "review" for m in MODULES)
    assert eval_commands.status is dataset_versions.status
    assert not hasattr(dataset_versions, "register")  # not a discovered module


def test_old_dataset_list_output_unchanged(tmp_path, monkeypatch, capsys):
    rc, out, _ = run(["dataset", "list"], tmp_path, monkeypatch, capsys)
    assert rc == 0 and out.splitlines()[0] == "no datasets registered"
    run(["dataset", "register", "xor", "--uri", "synthetic://xor"], tmp_path, monkeypatch, capsys)
    rc, out, _ = run(["dataset", "list"], tmp_path, monkeypatch, capsys)
    assert out == "xor\tsynthetic://xor\n"


# -- fixtures ------------------------------------------------------------------

def _rec(i, text, result, dtype="triage", **extra):
    rec = {"schema_version": "1.1", "decision_id": f"d-{i}", "decision_type": dtype, "function_name": dtype,
           "inputs": {"args": repr((text,))}, "outputs": {"result": repr(result)},
           "started_at": "2026-11-01T00:00:00+00:00"}
    rec.update(extra)
    return rec


@pytest.fixture
def records(tmp_path):
    rows = [
        _rec(1, "app crashes", "bug", prompt_name="triage", prompt_version="aaaaaaaaaaaa"),
        _rec(2, "how to export?", "question", prompt_name="triage", prompt_version="aaaaaaaaaaaa"),
        _rec(3, "charged twice", "bug", prompt_name="triage", prompt_version="bbbbbbbbbbbb"),
        _rec(4, "how to export?", "question"),
        _rec(5, "boom", None, error="RuntimeError: x"),
        {"decision_id": "d-6", "decision_type": "triage", "function_name": "triage",
         "inputs": {"args_sha256": "ab" * 32, "args_chars": 9},
         "outputs": {"result_sha256": "cd" * 32, "result_chars": 5, "result_type": "str"}},
        {"decision_id": "d-7", "decision_type": "llm.anthropic.messages", "status": "ok",
         "inputs": {}, "outputs": {}, "model_parameters": {"provider": "anthropic", "model_name": "m"}},
        _rec(8, "other", "x", dtype="lookup"),
    ]
    path = tmp_path / "runs.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    return path


@pytest.fixture(autouse=True)
def no_native_sanitizer(monkeypatch):
    monkeypatch.setattr(dataset_versions, "sanitizer_factory", lambda: None)


# -- from-records ----------------------------------------------------------------

def test_sanitizer_gate_requires_explicit_flag(tmp_path, records, monkeypatch, capsys):
    rc, out, err = run(["dataset", "from-records", str(records), "--name", "t"], tmp_path, monkeypatch, capsys)
    assert rc == 2
    assert "--allow-unsanitized" in err and "native" in err
    assert not (tmp_path / "datasets").exists()


def test_from_records_filters_and_explains_skips(tmp_path, records, monkeypatch, capsys):
    rc, out, _ = run(["dataset", "from-records", str(records), "--name", "t", "--where",
                      "decision_type=triage,status=ok", "--allow-unsanitized"], tmp_path, monkeypatch, capsys)
    assert rc == 0, out
    assert "Created t@v1: 4 cases" in out
    assert "sanitized  NO" in out
    assert "1 of 9 records skipped: captured without content" in out
    assert 'capture_content="full"' in out and "--capture-content full" in out
    assert "1 of 9 records skipped: not valid JSON objects" in out
    version = DatasetStore(tmp_path).get("t")
    assert version.cases[0]["input"] == {"args": ["app crashes"]}
    assert version.cases[0]["reference"] == "bug"
    assert version.cases[0]["metadata"]["prompt_version"] == "aaaaaaaaaaaa"
    assert version.header["sanitized"] is False
    assert version.header["filters"] == ["decision_type=triage", "status=ok"]
    assert "Next: briefcase eval run" in out


def test_no_content_only_produces_no_dataset(tmp_path, monkeypatch, capsys):
    path = tmp_path / "hashed.jsonl"
    path.write_text(json.dumps({"decision_type": "x", "inputs": {"args_count": 1},
                                "outputs": {"result_type": "str"}}) + "\n")
    rc, out, _ = run(["dataset", "from-records", str(path), "--name", "h", "--allow-unsanitized"],
                     tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "No dataset created" in out and "1 of 1 records skipped: captured without content" in out
    assert DatasetStore(tmp_path).names() == []


def test_sample_is_deterministic_and_dedupe(tmp_path, monkeypatch, capsys):
    path = tmp_path / "many.jsonl"
    path.write_text("\n".join(json.dumps(_rec(i, f"t{i % 30}", "x")) for i in range(60)) + "\n")
    store = DatasetStore(tmp_path)
    args = ["dataset", "from-records", str(path), "--allow-unsanitized", "--dedupe", "--sample", "10"]
    run(args + ["--name", "a", "--seed", "7"], tmp_path, monkeypatch, capsys)
    run(args + ["--name", "b", "--seed", "7"], tmp_path, monkeypatch, capsys)
    run(args + ["--name", "c", "--seed", "8"], tmp_path, monkeypatch, capsys)
    ids = {n: [c["id"] for c in store.get(n).cases] for n in "abc"}
    assert len(ids["a"]) == 10 and ids["a"] == ids["b"] and ids["a"] != ids["c"]
    assert store.get("a").hash == store.get("b").hash
    assert store.get("a").header["sample"] == {"n": 10, "seed": 7}
    assert store.get("a").header["skipped"] == {"duplicate": 30, "sampled_out": 20}


def test_sanitizer_and_redaction_applied(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dataset_versions, "sanitizer_factory", lambda: lambda s: s.replace("555-1234", "[PHONE]"))
    path = tmp_path / "pii.jsonl"
    rec = _rec(1, "call 555-1234", {"email": "a@b.c", "answer": "ok 555-1234"})
    path.write_text(json.dumps(rec) + "\n")
    rc, out, _ = run(["dataset", "from-records", str(path), "--name", "p", "--redact-fields", "email"],
                     tmp_path, monkeypatch, capsys)
    assert rc == 0 and "sanitized  yes" in out
    case = DatasetStore(tmp_path).get("p").cases[0]
    assert case["input"] == {"args": ["call [PHONE]"]}
    assert case["reference"] == {"email": "[REDACTED]", "answer": "ok [PHONE]"}
    assert DatasetStore(tmp_path).get("p").header["sanitized"] is True
    assert len(case["content_sha256"]) == 64


def test_name_registered_for_engine_is_refused(tmp_path, records, monkeypatch, capsys):
    run(["dataset", "register", "xor", "--uri", "synthetic://xor"], tmp_path, monkeypatch, capsys)
    rc, _, err = run(["dataset", "from-records", str(records), "--name", "xor", "--allow-unsanitized"],
                     tmp_path, monkeypatch, capsys)
    assert rc == 2 and "already registered" in err


# -- versions: immutability, show, list, diff ------------------------------------

def test_versions_are_immutable_and_unchanged_runs_do_not_bump(tmp_path, records, monkeypatch, capsys):
    argv = ["dataset", "from-records", str(records), "--name", "t", "--allow-unsanitized",
            "--where", "decision_type=triage"]
    run(argv, tmp_path, monkeypatch, capsys)
    rc, out, _ = run(argv, tmp_path, monkeypatch, capsys)
    assert rc == 0 and out.startswith("Unchanged: same cases as t@v1")
    path = DatasetStore(tmp_path).get("t").path
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
    os.chmod(path, 0o600)
    path.write_text(path.read_text().replace('"bug"', '"feature"', 1))
    rc, _, err = run(["dataset", "show", "t"], tmp_path, monkeypatch, capsys)
    assert rc == 2 and "modified after it was created" in err


def test_show_list_diff_and_json(tmp_path, records, monkeypatch, capsys):
    run(["dataset", "from-records", str(records), "--name", "t", "--allow-unsanitized", "--where",
         "decision_type=triage,prompt_version=aaaaaaaaaaaa"], tmp_path, monkeypatch, capsys)
    run(["dataset", "from-records", str(records), "--name", "t", "--allow-unsanitized", "--where",
         "decision_type=triage", "--dedupe"], tmp_path, monkeypatch, capsys)
    rc, out, _ = run(["dataset", "show", "t@v1"], tmp_path, monkeypatch, capsys)
    assert rc == 0 and out.startswith("t@v1") and "versions   v1, v2" in out
    rc, out, _ = run(["dataset", "list"], tmp_path, monkeypatch, capsys)
    assert "t\tlocal: v2 (3 cases, 2 versions" in out
    rc, out, _ = run(["dataset", "list", "--json"], tmp_path, monkeypatch, capsys)
    assert json.loads(out)["versioned"][0]["versions"] == ["v1", "v2"]
    rc, out, _ = run(["dataset", "diff", "t@v1", "v2"], tmp_path, monkeypatch, capsys)
    assert rc == 0 and "+1 added  -0 removed  ~0 changed  2 unchanged" in out
    rc, out, _ = run(["dataset", "diff", "t@v1", "t@v2", "--json"], tmp_path, monkeypatch, capsys)
    d = json.loads(out)
    assert [c["input"] for c in d["added"]] == [{"args": ["charged twice"]}]
    v1 = DatasetStore(tmp_path).get("t@v1")
    assert DatasetStore(tmp_path).get(f"t@{v1.hash[:8]}").tag == "v1"
    rc, _, err = run(["dataset", "show", "t@v9"], tmp_path, monkeypatch, capsys)
    assert rc == 2 and "versions: v1, v2" in err


# -- export / import -------------------------------------------------------------

def _make(tmp_path, records, monkeypatch, capsys, name="t"):
    rc, out, _ = run(["dataset", "from-records", str(records), "--name", name, "--allow-unsanitized",
                      "--where", "decision_type=triage", "--dedupe"], tmp_path, monkeypatch, capsys)
    assert rc == 0, out
    return DatasetStore(tmp_path).get(name)


def test_jsonl_export_edit_import_round_trip(tmp_path, records, monkeypatch, capsys):
    v1 = _make(tmp_path, records, monkeypatch, capsys)
    out_path = tmp_path / "edit.jsonl"
    rc, out, _ = run(["dataset", "export", "t", "--out", str(out_path)], tmp_path, monkeypatch, capsys)
    assert rc == 0 and "dataset import" in out
    rc, out, _ = run(["dataset", "import", str(out_path), "--allow-unsanitized"], tmp_path, monkeypatch, capsys)
    assert rc == 0 and out.startswith("Unchanged"), out
    rc, _, err = run(["dataset", "export", "t", "--out", str(out_path)], tmp_path, monkeypatch, capsys)
    assert rc == 1 and "--yes" in err
    lines = out_path.read_text().splitlines()
    lines[3] = lines[3].replace('"bug"', '"billing"')
    lines.append(json.dumps({"input": {"args": ["new ticket"]}, "reference": "question"}))
    out_path.write_text("\n".join(lines) + "\n")
    rc, out, _ = run(["dataset", "import", str(out_path), "--allow-unsanitized"], tmp_path, monkeypatch, capsys)
    assert rc == 0 and out.startswith("Created t@v2"), out
    d = __import__("briefcase.datasets", fromlist=["diff_versions"]).diff_versions(
        v1, DatasetStore(tmp_path).get("t@v2"))
    assert len(d["added"]) == 1 and len(d["changed"]) == 1 and d["changed"][0]["fields"] == ["reference"]
    assert DatasetStore(tmp_path).get("t@v2").header["source"]["from_hash"] == v1.hash


def test_import_reports_errors_by_line(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(textwrap.dedent("""\
        {"input": "ok", "reference": "x"}
        {"input": "a", "refernce": "x"}
        {"reference": "x"}
        {not json
        {"id": "dup", "input": "1"}
        {"id": "dup", "input": "2"}
        """))
    rc, _, err = run(["dataset", "import", str(bad), "--name", "b", "--allow-unsanitized"],
                     tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "bad.jsonl:line 2: unknown key 'refernce' (did you mean 'reference'?)" in err
    assert "line 3: missing required key 'input'" in err
    assert "line 4: not valid JSON" in err
    assert "line 6: duplicate id 'dup' (first used on line 5)" in err
    assert DatasetStore(tmp_path).names() == []


def test_inspect_export_shape_round_trips_through_sdk_parser(tmp_path, records, monkeypatch, capsys):
    from briefcase.integrations.evals import from_inspect_log

    version = _make(tmp_path, records, monkeypatch, capsys)
    rc, out, _ = run(["dataset", "export", "t", "--format", "inspect"], tmp_path, monkeypatch, capsys)
    samples = [json.loads(line) for line in out.splitlines()]
    # inspect_ai.dataset.Sample fields (checked against inspect-ai 0.3.291).
    assert all(set(s) == {"id", "input", "target", "metadata"} for s in samples)
    assert samples[0]["input"] == "app crashes" and samples[0]["target"] == "bug"
    log = tmp_path / "log.json"
    log.write_text(json.dumps({"eval": {"task": "t"}, "samples": [dict(s, scores={}) for s in samples]}))
    parsed = from_inspect_log(log)
    assert [c["case_id"] for c in parsed.cases] == [f"t/{c['id']}" for c in version.cases]
    assert [c["target"] for c in parsed.cases] == [c["reference"] for c in version.cases]


def test_lm_eval_export_writes_docs_and_task_yaml(tmp_path, records, monkeypatch, capsys):
    _make(tmp_path, records, monkeypatch, capsys)
    rc, _, err = run(["dataset", "export", "t", "--format", "lm-eval"], tmp_path, monkeypatch, capsys)
    assert rc == 2 and "--out" in err
    docs = tmp_path / "lm" / "t.jsonl"
    rc, out, _ = run(["dataset", "export", "t", "--format", "lm-eval", "--out", str(docs)],
                     tmp_path, monkeypatch, capsys)
    assert rc == 0
    rows = [json.loads(line) for line in docs.read_text().splitlines()]
    assert rows[0] == {"id": rows[0]["id"], "input": "app crashes", "target": "bug"}
    task = docs.with_suffix(".yaml").read_text()
    for line in ("task: briefcase_t_v1", "dataset_path: json", "test_split: test", "output_type: generate_until",
                 'doc_to_text: "{{input}}"', 'doc_to_target: "{{target}}"', "  - metric: exact_match"):
        assert line in task
    assert json.dumps(str(docs.resolve())) in task


def test_export_dry_run_writes_nothing(tmp_path, records, monkeypatch, capsys):
    _make(tmp_path, records, monkeypatch, capsys)
    target = tmp_path / "x.jsonl"
    rc, out, _ = run(["dataset", "export", "t", "--out", str(target), "--dry-run"], tmp_path, monkeypatch, capsys)
    assert rc == 0 and "would write" in out and not target.exists()


# -- eval run ---------------------------------------------------------------------

@pytest.fixture
def toy(tmp_path, monkeypatch):
    mod = tmp_path / "toyapp_ds.py"
    mod.write_text(textwrap.dedent("""\
        import json
        from briefcase.prompts import prompt

        OLD = prompt("triage", "old {text}", save=False)
        NEW = prompt("triage", "new {text}", save=False)

        def _label(text):
            return "bug" if "crash" in text else "question"

        def good(text):
            NEW.render(text=text)
            return {"app crashes": "bug", "how to export?": "question", "charged twice": "bug"}[text]

        def weak(text):
            OLD.render(text=text)
            return _label(text)

        def boom(text):
            raise RuntimeError("model down")

        def as_json(text):
            return json.dumps({"label": _label(text)})

        async def async_good(text):
            return good(text)

        def lenient(output, reference, case):
            return {"score": 0.5, "passed": True}
        """))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield "toyapp_ds"
    sys.modules.pop("toyapp_ds", None)


def test_eval_run_exact_and_ci_exit_codes(tmp_path, records, toy, monkeypatch, capsys):
    _make(tmp_path, records, monkeypatch, capsys)
    out_file = tmp_path / "evals.jsonl"
    rc, out, _ = run(["eval", "run", "--dataset", "t", "--target", f"{toy}:good", "--fail-under", "1.0",
                      "--to", str(out_file)], tmp_path, monkeypatch, capsys)
    assert rc == 0, out
    assert "PASS" in out and "100.0%" in out
    recs = [json.loads(line) for line in out_file.read_text().splitlines()]
    assert [r["decision_type"] for r in recs].count("eval.case") == 3
    assert recs[-1]["decision_type"] == "eval.run" and recs[-1]["outputs"]["pass_rate"] == 1.0
    assert "sha256:" in recs[0]["inputs"]["inputs"] and "crashes" not in out_file.read_text()
    rc, out, _ = run(["eval", "run", "--dataset", "t@v1", "--target", f"{toy}:weak", "--fail-under", "0.9",
                      "--to", str(out_file)], tmp_path, monkeypatch, capsys)
    assert rc == 1 and "FAIL" in out
    assert out.index("Worst failures") < out.index("expected 'bug'  got 'question'")
    rc, _, err = run(["eval", "run", "--dataset", "nope", "--target", f"{toy}:good"], tmp_path, monkeypatch, capsys)
    assert rc == 2 and "no dataset named 'nope'" in err
    rc, _, err = run(["eval", "run", "--dataset", "t", "--target", f"{toy}:missing"], tmp_path, monkeypatch, capsys)
    assert rc == 2 and "has no attribute" in err
    rc, _, err = run(["eval", "run", "--dataset", "t", "--target", f"{toy}:good", "--fail-under", "90"],
                     tmp_path, monkeypatch, capsys)
    assert rc == 2


def test_eval_run_scorers_errors_async_and_content_flag(tmp_path, records, toy, monkeypatch, capsys):
    _make(tmp_path, records, monkeypatch, capsys)
    dest = tmp_path / "e.jsonl"

    def summary(*extra):
        rc, out, _ = run(["eval", "run", "--dataset", "t", "--json", "--to", str(dest), *extra],
                         tmp_path, monkeypatch, capsys)
        return rc, json.loads(out)

    rc, s = summary("--target", f"{toy}:as_json", "--scorer", "contains")
    assert s["targets"][0]["passed"] == 2  # '"bug"' is in '{"label": "bug"}', charged-twice is not
    rc, s = summary("--target", f"{toy}:as_json", "--scorer", "json")
    assert s["targets"][0]["passed"] == 0  # references are plain labels, not JSON
    rc, s = summary("--target", f"{toy}:as_json", "--scorer", f"{toy}:lenient")
    assert s["targets"][0]["pass_rate"] == 1.0 and s["targets"][0]["groups"][0]["mean_score"] == 0.5
    rc, s = summary("--target", f"{toy}:boom", "--fail-under", "0.5")
    assert rc == 1 and s["targets"][0]["errors"] == 3 and "model down" in s["targets"][0]["worst"][0]["error"]
    rc, s = summary("--target", f"{toy}:async_good")
    assert s["targets"][0]["pass_rate"] == 1.0
    summary("--target", f"{toy}:good", "--capture-content")
    assert "app crashes" in dest.read_text()


def test_eval_run_is_deterministic_under_concurrency(tmp_path, records, toy, monkeypatch, capsys):
    _make(tmp_path, records, monkeypatch, capsys)
    outs = []
    for n in ("1", "4"):
        rc, out, _ = run(["eval", "run", "--dataset", "t", "--target", f"{toy}:weak", "--json",
                          "--concurrency", n, "--to", str(tmp_path / "c.jsonl")], tmp_path, monkeypatch, capsys)
        data = json.loads(out)
        for t in data["targets"]:
            t.pop("run_id")
        outs.append(data)
    assert outs[0] == outs[1]


def test_eval_groups_by_prompt_version_in_one_table(tmp_path, records, toy, monkeypatch, capsys):
    from briefcase.prompts import prompt_version

    _make(tmp_path, records, monkeypatch, capsys)
    rc, out, _ = run(["eval", "run", "--dataset", "t", "--target", f"{toy}:weak", "--target", f"{toy}:good",
                      "--to", str(tmp_path / "g.jsonl")], tmp_path, monkeypatch, capsys)
    assert rc == 0
    old, new = prompt_version("old {text}"), prompt_version("new {text}")
    table = out.split("Worst failures")[0]
    assert f"triage@{old}" in table and f"triage@{new}" in table
    cases = [json.loads(line) for line in (tmp_path / "g.jsonl").read_text().splitlines()]
    assert {c["metadata"].get("prompt_version") for c in cases if c["decision_type"] == "eval.case"} == {old, new}


def test_reference_none_needs_custom_scorer(tmp_path, records, toy, monkeypatch, capsys):
    run(["dataset", "from-records", str(records), "--name", "u", "--allow-unsanitized", "--reference", "none",
         "--where", "decision_type=triage"], tmp_path, monkeypatch, capsys)
    version = DatasetStore(tmp_path).get("u")
    assert all(c["reference"] is None for c in version.cases)
    assert len(version.cases) == 5  # the errored record is kept: no output needed
    rc, _, err = run(["eval", "run", "--dataset", "u", "--target", f"{toy}:good"], tmp_path, monkeypatch, capsys)
    assert rc == 2 and "--scorer module:function" in err
    rc, out, _ = run(["eval", "run", "--dataset", "u", "--target", f"{toy}:weak", "--scorer", f"{toy}:lenient",
                      "--to", str(tmp_path / "n.jsonl")], tmp_path, monkeypatch, capsys)
    assert rc == 0


# -- status hook ---------------------------------------------------------------------

def test_status_hook(tmp_path, records, toy, monkeypatch, capsys):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path))
    s = dataset_versions.status()
    assert set(s) == {"id", "state", "detail", "next"} and s["state"] == "off"
    assert s["id"] == "datasets-from-records"
    _make(tmp_path, records, monkeypatch, capsys)
    assert dataset_versions.status()["state"] == "on"
    assert "never evaluated" in dataset_versions.status()["detail"]
    run(["eval", "run", "--dataset", "t", "--target", f"{toy}:weak", "--target", f"{toy}:good",
         "--fail-under", "0.9", "--to", str(tmp_path / "s.jsonl")], tmp_path, monkeypatch, capsys)
    s = dataset_versions.status()
    assert s["state"] == "attention" and "toyapp_ds:weak" in s["detail"]
    assert s["next"].startswith("briefcase eval run --dataset t@v1")


# -- prompt commands -------------------------------------------------------------------

def test_prompt_list_show_diff(tmp_path, monkeypatch, capsys):
    from briefcase.prompts import prompt

    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path))
    a = prompt("triage", "Classify: {text}")
    b = prompt("triage", "Classify as bug or question: {text}")
    rc, out, _ = run(["prompt", "list"], tmp_path, monkeypatch, capsys)
    assert rc == 0 and "triage" in out and "2" in out
    rc, out, _ = run(["prompt", "show", f"triage@{a.version[:6]}"], tmp_path, monkeypatch, capsys)
    assert "Classify: {text}" in out and "placeholders: text" in out
    rc, out, _ = run(["prompt", "diff", f"triage@{a.version}", b.version], tmp_path, monkeypatch, capsys)
    assert "-Classify: {text}" in out and "+Classify as bug or question: {text}" in out
    rc, _, err = run(["prompt", "show", "nope"], tmp_path, monkeypatch, capsys)
    assert rc == 2 and "no prompt named" in err


def test_from_records_dry_run_writes_nothing(tmp_path, records, monkeypatch, capsys):
    rc, out, _ = run(["dataset", "from-records", str(records), "--name", "t", "--allow-unsanitized", "--dry-run"],
                     tmp_path, monkeypatch, capsys)
    assert rc == 0 and out.startswith("Dry run: would create a version of t with")
    assert DatasetStore(tmp_path).names() == []
