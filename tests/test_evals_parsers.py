"""Tests for the stdlib-only eval log parsers and the replay helper."""

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from briefcase.exporters.memory import MemoryExporter
from briefcase.integrations.evals import parsers as parsers_module
from briefcase.integrations.evals.parsers import (
    ParsedEvalLog,
    from_inspect_log,
    from_lm_eval_results,
    replay,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _inspect_log():
    return {
        "version": 2,
        "status": "success",
        "eval": {"run_id": "abc123", "task": "gsm8k", "model": "anthropic/claude-opus-5"},
        "results": {
            "scores": [
                {
                    "name": "match",
                    "metrics": {
                        "accuracy": {"name": "accuracy", "value": 0.5},
                        "stderr": {"name": "stderr", "value": 0.35},
                    },
                }
            ]
        },
        "samples": [
            {
                "id": 1,
                "epoch": 1,
                "input": "What is 2+2?",
                "target": "4",
                "output": {"choices": [{"message": {"role": "assistant", "content": "4"}}]},
                "scores": {"match": {"value": "C", "answer": "4"}},
            },
            {
                "id": 2,
                "epoch": 1,
                "input": [
                    {"role": "system", "content": "Be terse."},
                    {"role": "user", "content": "What is 3+3?"},
                ],
                "target": "6",
                "output": {"choices": [{"message": {"role": "assistant", "content": "7"}}]},
                "scores": {"match": {"value": "I"}},
            },
        ],
    }


def _write_inspect_json(tmp_path, payload=None):
    path = tmp_path / "log.json"
    path.write_text(json.dumps(payload if payload is not None else _inspect_log()))
    return path


class TestInspectJson:
    def test_parses_header(self, tmp_path):
        parsed = from_inspect_log(_write_inspect_json(tmp_path))
        assert isinstance(parsed, ParsedEvalLog)
        assert parsed.source == "inspect-ai"
        assert parsed.name == "gsm8k"
        assert parsed.model == "anthropic/claude-opus-5"

    def test_aggregate_metrics(self, tmp_path):
        parsed = from_inspect_log(_write_inspect_json(tmp_path))
        assert parsed.metrics == {"match/accuracy": 0.5, "match/stderr": 0.35}

    def test_case_fields(self, tmp_path):
        parsed = from_inspect_log(_write_inspect_json(tmp_path))
        first = parsed.cases[0]
        assert first["case_id"] == "gsm8k/1"
        assert first["inputs"] == "What is 2+2?"
        assert first["outputs"] == "4"
        assert first["target"] == "4"
        assert first["scores"] == {"match": 1.0}
        assert first["passed"] is True

    def test_letter_grades_map_to_numbers(self, tmp_path):
        payload = _inspect_log()
        payload["samples"][0]["scores"]["match"]["value"] = "P"
        payload["samples"][1]["scores"]["match"]["value"] = "N"
        parsed = from_inspect_log(_write_inspect_json(tmp_path, payload))
        assert parsed.cases[0]["scores"] == {"match": 0.5}
        assert parsed.cases[0]["passed"] is False
        assert parsed.cases[1]["scores"] == {"match": 0.0}

    def test_numeric_and_bool_score_values(self, tmp_path):
        payload = _inspect_log()
        payload["samples"][0]["scores"]["match"]["value"] = 0.75
        payload["samples"][1]["scores"]["match"] = {"value": True}
        parsed = from_inspect_log(_write_inspect_json(tmp_path, payload))
        assert parsed.cases[0]["scores"] == {"match": 0.75}
        assert parsed.cases[1]["scores"] == {"match": 1.0}

    def test_chat_message_input_is_flattened(self, tmp_path):
        parsed = from_inspect_log(_write_inspect_json(tmp_path))
        assert parsed.cases[1]["inputs"] == "system: Be terse.\nuser: What is 3+3?"

    def test_failed_case_passed_is_false(self, tmp_path):
        parsed = from_inspect_log(_write_inspect_json(tmp_path))
        assert parsed.cases[1]["passed"] is False

    def test_missing_scores_leave_passed_none(self, tmp_path):
        payload = _inspect_log()
        payload["samples"][0].pop("scores")
        parsed = from_inspect_log(_write_inspect_json(tmp_path, payload))
        assert parsed.cases[0]["scores"] == {}
        assert parsed.cases[0]["passed"] is None

    def test_rejects_unrecognized_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"hello": "world"}))
        with pytest.raises(ValueError, match="inspect-ai"):
            from_inspect_log(path)

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            from_inspect_log(tmp_path / "nope.json")


class TestInspectEvalZip:
    def _write_eval_zip(self, tmp_path):
        payload = _inspect_log()
        samples = payload.pop("samples")
        path = tmp_path / "log.eval"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("header.json", json.dumps(payload))
            for sample in samples:
                archive.writestr(
                    f"samples/{sample['id']}_{sample['epoch']}.json", json.dumps(sample)
                )
        return path

    def test_reads_header_and_samples_from_zip(self, tmp_path):
        parsed = from_inspect_log(self._write_eval_zip(tmp_path))
        assert parsed.name == "gsm8k"
        assert parsed.model == "anthropic/claude-opus-5"
        assert parsed.metrics == {"match/accuracy": 0.5, "match/stderr": 0.35}
        assert [c["case_id"] for c in parsed.cases] == ["gsm8k/1", "gsm8k/2"]

    def test_rejects_zip_without_header(self, tmp_path):
        path = tmp_path / "empty.eval"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("unrelated.txt", "nothing")
        with pytest.raises(ValueError, match="inspect-ai"):
            from_inspect_log(path)

    def test_journal_entries_are_not_read_as_samples(self, tmp_path):
        payload = _inspect_log()
        samples = payload.pop("samples")
        path = tmp_path / "log.eval"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("header.json", json.dumps(payload))
            archive.writestr("summaries.json", json.dumps([]))
            archive.writestr("_journal/start.json", json.dumps({}))
            archive.writestr("_journal/summaries/1.json", json.dumps({}))
            archive.writestr("samples/1_epoch_1.json", json.dumps(samples[0]))
        assert [c["case_id"] for c in from_inspect_log(path).cases] == ["gsm8k/1"]

    def test_samples_are_ordered_numerically(self, tmp_path):
        payload = _inspect_log()
        template = payload.pop("samples")[0]
        path = tmp_path / "log.eval"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("header.json", json.dumps(payload))
            for sample_id in (10, 2, 1):
                sample = dict(template, id=sample_id)
                archive.writestr(f"samples/{sample_id}_epoch_1.json", json.dumps(sample))
        assert [c["case_id"] for c in from_inspect_log(path).cases] == [
            "gsm8k/1", "gsm8k/2", "gsm8k/10",
        ]


class TestRealInspectArchive:
    """Parses an artifact inspect-ai 0.3.257 actually wrote.

    Real .eval archives store their entries zstd-compressed (ZIP method 93),
    which stdlib zipfile cannot decode before Python 3.14.
    """

    FIXTURE = FIXTURES / "inspect_arithmetic.eval"

    def test_entries_are_zstd_compressed(self):
        with zipfile.ZipFile(self.FIXTURE) as archive:
            assert {i.compress_type for i in archive.infolist()} == {93}

    @pytest.mark.skipif(
        parsers_module._zstd_decompressor() is None,
        reason="no zstd backend (Python < 3.14 without zstandard/pyzstd)",
    )
    def test_parses_real_archive(self):
        parsed = from_inspect_log(self.FIXTURE)
        assert parsed.name == "arithmetic"
        assert parsed.model == "mockllm/model"
        assert parsed.metrics == {"match/accuracy": 0.5, "match/stderr": 0.5}
        assert [c["case_id"] for c in parsed.cases] == ["arithmetic/1", "arithmetic/2"]
        assert parsed.cases[0]["inputs"] == "What is 2+2?"
        assert parsed.cases[0]["outputs"] == "Default output from mockllm/model"
        assert parsed.cases[0]["scores"] == {"match": 1.0}
        assert parsed.cases[0]["passed"] is True
        assert parsed.cases[1]["passed"] is False

    def test_missing_zstd_backend_raises_an_actionable_error(self, monkeypatch):
        monkeypatch.setattr(parsers_module, "_zstd_decompressor", lambda: None)
        with pytest.raises(ValueError, match="zstandard"):
            from_inspect_log(self.FIXTURE)


def _lm_eval_results():
    return {
        "results": {
            "gsm8k": {
                "alias": "gsm8k",
                "exact_match,strict-match": 0.5,
                "exact_match_stderr,strict-match": 0.25,
            }
        },
        "config": {"model": "hf", "model_args": "pretrained=meta-llama/Llama-3-8B"},
    }


def _write_lm_eval(tmp_path, payload=None):
    path = tmp_path / "results.json"
    path.write_text(json.dumps(payload if payload is not None else _lm_eval_results()))
    return path


class TestLmEvalResults:
    def test_parses_header_and_metrics(self, tmp_path):
        parsed = from_lm_eval_results(_write_lm_eval(tmp_path))
        assert parsed.source == "lm-eval-harness"
        assert parsed.name == "gsm8k"
        assert parsed.model == "hf"
        assert parsed.metrics == {"exact_match": 0.5}

    def test_multiple_tasks_join_into_name(self, tmp_path):
        payload = _lm_eval_results()
        payload["results"]["arc_easy"] = {"acc,none": 0.8}
        parsed = from_lm_eval_results(_write_lm_eval(tmp_path, payload))
        assert parsed.name == "arc_easy+gsm8k"
        assert parsed.metrics == {"arc_easy/acc": 0.8, "gsm8k/exact_match": 0.5}

    def test_task_filter_selects_one_task(self, tmp_path):
        payload = _lm_eval_results()
        payload["results"]["arc_easy"] = {"acc,none": 0.8}
        parsed = from_lm_eval_results(_write_lm_eval(tmp_path, payload), task="arc_easy")
        assert parsed.name == "arc_easy"
        assert parsed.metrics == {"acc": 0.8}

    def test_unknown_task_filter_raises(self, tmp_path):
        with pytest.raises(ValueError, match="arc_easy"):
            from_lm_eval_results(_write_lm_eval(tmp_path), task="arc_easy")

    def test_no_samples_means_no_cases(self, tmp_path):
        assert from_lm_eval_results(_write_lm_eval(tmp_path)).cases == []

    def test_samples_jsonl_produces_cases(self, tmp_path):
        samples = tmp_path / "samples_gsm8k.jsonl"
        samples.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {
                        "doc_id": 0,
                        "task": "gsm8k",
                        "arguments": [["What is 2+2?", ""]],
                        "target": "4",
                        "filtered_resps": ["4"],
                        "exact_match": 1.0,
                    },
                    {
                        "doc_id": 1,
                        "task": "gsm8k",
                        "arguments": [["What is 3+3?", ""]],
                        "target": "6",
                        "resps": [["seven"]],
                        "exact_match": 0.0,
                    },
                ]
            )
        )
        parsed = from_lm_eval_results(_write_lm_eval(tmp_path), samples)
        assert [c["case_id"] for c in parsed.cases] == ["gsm8k/0", "gsm8k/1"]
        assert parsed.cases[0]["inputs"] == "What is 2+2?"
        assert parsed.cases[0]["outputs"] == "4"
        assert parsed.cases[0]["target"] == "4"
        assert parsed.cases[0]["scores"] == {"exact_match": 1.0}
        assert parsed.cases[0]["passed"] is True
        assert parsed.cases[1]["outputs"] == "seven"
        assert parsed.cases[1]["passed"] is False

    def test_blank_lines_in_samples_are_skipped(self, tmp_path):
        samples = tmp_path / "samples.jsonl"
        samples.write_text('{"doc_id": 0, "task": "t", "acc": 1.0}\n\n')
        parsed = from_lm_eval_results(_write_lm_eval(tmp_path), samples)
        assert len(parsed.cases) == 1

    def test_rejects_unrecognized_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"samples": []}))
        with pytest.raises(ValueError, match="lm-eval"):
            from_lm_eval_results(path)


class TestRealLmEvalOutput:
    """Parses artifacts lm-eval-harness 0.4.12 actually wrote (`--model dummy
    --tasks arc_easy --limit 4 --log_samples`), verbatim except that one
    absolute path under the unused `configs` key was made relative.
    """

    RESULTS = FIXTURES / "lm_eval_results.json"
    SAMPLES = FIXTURES / "lm_eval_samples_arc_easy.jsonl"

    def test_header_and_metrics(self):
        parsed = from_lm_eval_results(self.RESULTS)
        assert parsed.name == "arc_easy"
        assert parsed.model == "dummy"
        assert parsed.metrics == {"acc": 0.25, "acc_norm": 0.25}

    def test_case_ids_use_the_task_name(self):
        parsed = from_lm_eval_results(self.RESULTS, self.SAMPLES)
        assert [c["case_id"] for c in parsed.cases] == [
            "arc_easy/0", "arc_easy/1", "arc_easy/2", "arc_easy/3",
        ]

    def test_prompt_comes_from_the_gen_args_mapping(self):
        parsed = from_lm_eval_results(self.RESULTS, self.SAMPLES)
        assert parsed.cases[0]["inputs"].startswith(
            "Question: Which statement best explains why photosynthesis"
        )

    def test_loglikelihood_responses_keep_their_shape(self):
        parsed = from_lm_eval_results(self.RESULTS, self.SAMPLES)
        outputs = parsed.cases[0]["outputs"]
        assert json.loads(outputs)[0] == ["-0.2604923103919594", "False"]

    def test_per_case_scores_match_the_aggregate(self):
        parsed = from_lm_eval_results(self.RESULTS, self.SAMPLES)
        assert [c["scores"] for c in parsed.cases] == [
            {"acc": 0.0, "acc_norm": 0.0},
            {"acc": 1.0, "acc_norm": 1.0},
            {"acc": 0.0, "acc_norm": 0.0},
            {"acc": 0.0, "acc_norm": 0.0},
        ]
        assert [c["passed"] for c in parsed.cases] == [False, True, False, False]

    def test_replayed_pass_rate_matches_reported_accuracy(self):
        mem = MemoryExporter()
        replay(from_lm_eval_results(self.RESULTS, self.SAMPLES), exporter=mem)
        assert mem.records[-1]["outputs"]["pass_rate"] == 0.25
        assert mem.records[-1]["outputs"]["metadata"]["metrics"]["acc"] == 0.25

    def test_task_filter_keeps_rows_that_omit_the_task_key(self):
        parsed = from_lm_eval_results(self.RESULTS, self.SAMPLES, task="arc_easy")
        assert len(parsed.cases) == 4


class TestEncoding:
    """Eval logs are UTF-8 regardless of the machine's locale encoding."""

    def test_parsers_never_rely_on_the_default_encoding(self, tmp_path):
        # -X warn_default_encoding turns every encoding-less open() into an
        # EncodingWarning; -W error makes one fail the subprocess.
        inspect_json = tmp_path / "log.json"
        inspect_json.write_text(json.dumps(_inspect_log()), encoding="utf-8")

        script = tmp_path / "parse.py"
        script.write_text(
            "from briefcase.integrations.evals import "
            "from_inspect_log, from_lm_eval_results\n"
            f"from_inspect_log({str(FIXTURES / 'inspect_arithmetic.eval')!r})\n"
            f"from_inspect_log({str(inspect_json)!r})\n"
            f"from_lm_eval_results({str(FIXTURES / 'lm_eval_results.json')!r}, "
            f"{str(FIXTURES / 'lm_eval_samples_arc_easy.jsonl')!r})\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, "-X", "warn_default_encoding",
             "-W", "error::EncodingWarning", str(script)],
            capture_output=True, text=True, cwd=str(Path(__file__).parent.parent),
        )
        assert completed.returncode == 0, completed.stderr

    def test_non_ascii_content_survives(self, tmp_path):
        payload = _inspect_log()
        payload["samples"][0]["input"] = "Quelle est la température moyenne?"
        payload["samples"][0]["output"]["choices"][0]["message"]["content"] = "20 °C"
        path = tmp_path / "log.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        parsed = from_inspect_log(path)
        assert parsed.cases[0]["inputs"] == "Quelle est la température moyenne?"
        assert parsed.cases[0]["outputs"] == "20 °C"

    def test_samples_with_crlf_and_no_trailing_newline(self, tmp_path):
        samples = tmp_path / "samples_gsm8k_2026-08-12T00-00-00.000000.jsonl"
        samples.write_bytes(
            b'{"doc_id": 0, "exact_match": 1.0}\r\n{"doc_id": 1, "exact_match": 0.0}'
        )
        parsed = from_lm_eval_results(_write_lm_eval(tmp_path), samples)
        assert [c["case_id"] for c in parsed.cases] == ["gsm8k/0", "gsm8k/1"]


class TestReplay:
    def test_round_trip_emits_cases_and_run(self, tmp_path):
        mem = MemoryExporter()
        parsed = from_inspect_log(_write_inspect_json(tmp_path))

        run = replay(parsed, exporter=mem)

        assert [r["decision_type"] for r in mem.records] == [
            "eval.case",
            "eval.case",
            "eval.run",
        ]
        assert run.model == "anthropic/claude-opus-5"
        assert mem.records[-1]["function_name"] == "gsm8k"
        assert mem.records[-1]["outputs"]["pass_rate"] == 0.5
        assert mem.records[-1]["outputs"]["metadata"] == {
            "source": "inspect-ai",
            "metrics": {"match/accuracy": 0.5, "match/stderr": 0.35},
        }

    def test_name_override(self, tmp_path):
        mem = MemoryExporter()
        parsed = from_inspect_log(_write_inspect_json(tmp_path))
        replay(parsed, exporter=mem, name="nightly")
        assert mem.records[-1]["function_name"] == "nightly"
