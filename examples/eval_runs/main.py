"""
Example: turn evaluation results into Briefcase decision records.

Three patterns:
1. EvalRun as a context manager, logging cases from a hand-rolled loop
2. Replaying an inspect-ai log file
3. Replaying lm-eval-harness results plus its per-sample jsonl

Runs offline and needs no extra: the parsers are stdlib only and never import
an evaluation framework. The log files here are written to a temp directory so
the example is self-contained.
"""

import json
import tempfile
from pathlib import Path

import briefcase
from briefcase.integrations.evals import (
    EvalRun,
    from_inspect_log,
    from_lm_eval_results,
    replay,
)

records = briefcase.observe("memory")


# Pattern 1: log a run as it happens
print("Pattern 1: EvalRun context manager")
print("-" * 50)

CASES = [
    ("q1", "What is 2+2?", "4", "4"),
    ("q2", "What is 3+3?", "7", "6"),
    ("q3", "Capital of France?", "Paris", "Paris"),
]

with EvalRun("arithmetic", model="claude-opus-5") as run:
    for case_id, question, answer, target in CASES:
        run.log_case(
            case_id,
            inputs=question,
            outputs=answer,
            target=target,
            passed=answer == target,
            scores={"exact_match": 1.0 if answer == target else 0.0},
            input_tokens=len(question.split()),
            output_tokens=len(answer.split()),
        )

summary = run.summary()
print(f"pass_rate: {summary['pass_rate']:.2f}")
print(f"exact_match mean: {summary['scores']['exact_match']['mean']:.2f}")
print(f"tokens: {summary['input_tokens']} in, {summary['output_tokens']} out")
print()


workdir = Path(tempfile.mkdtemp(prefix="briefcase-evals-"))


# Pattern 2: replay an inspect-ai log
print("Pattern 2: replay an inspect-ai log")
print("-" * 50)

inspect_log = workdir / "gsm8k.json"
inspect_log.write_text(json.dumps({
    "version": 2,
    "status": "success",
    "eval": {"run_id": "abc123", "task": "gsm8k", "model": "anthropic/claude-opus-5"},
    "results": {
        "scores": [
            {"name": "match", "metrics": {"accuracy": {"name": "accuracy", "value": 0.5}}}
        ]
    },
    "samples": [
        {
            "id": 1,
            "epoch": 1,
            "input": "Natalia sold 48 clips in April. How many in April and May?",
            "target": "72",
            "output": {"choices": [{"message": {"role": "assistant", "content": "72"}}]},
            "scores": {"match": {"value": "C"}},
        },
        {
            "id": 2,
            "epoch": 1,
            "input": [
                {"role": "system", "content": "Answer with a number."},
                {"role": "user", "content": "Weng earns $12 an hour. 50 minutes?"},
            ],
            "target": "10",
            "output": {"choices": [{"message": {"role": "assistant", "content": "12"}}]},
            "scores": {"match": {"value": "I"}},
        },
    ],
}))

parsed = from_inspect_log(inspect_log)
print(f"source={parsed.source} task={parsed.name} model={parsed.model}")
print(f"metrics: {parsed.metrics}")
inspect_run = replay(parsed)
print(f"replayed pass_rate: {inspect_run.summary()['pass_rate']}")
print()


# Pattern 3: replay lm-eval-harness results
print("Pattern 3: replay lm-eval-harness results")
print("-" * 50)

lm_results = workdir / "results.json"
lm_results.write_text(json.dumps({
    "results": {
        "gsm8k": {
            "alias": "gsm8k",
            "exact_match,strict-match": 0.5,
            "exact_match_stderr,strict-match": 0.35,
        }
    },
    "config": {"model": "hf", "model_args": "pretrained=meta-llama/Llama-3-8B"},
}))

lm_samples = workdir / "samples_gsm8k.jsonl"
lm_samples.write_text("\n".join(json.dumps(row) for row in [
    {
        "doc_id": 0,
        "task": "gsm8k",
        "arguments": [["Natalia sold 48 clips in April.", ""]],
        "target": "72",
        "filtered_resps": ["72"],
        "exact_match": 1.0,
    },
    {
        "doc_id": 1,
        "task": "gsm8k",
        "arguments": [["Weng earns $12 an hour.", ""]],
        "target": "10",
        "filtered_resps": ["12"],
        "exact_match": 0.0,
    },
]))

lm_parsed = from_lm_eval_results(lm_results, lm_samples)
print(f"source={lm_parsed.source} task={lm_parsed.name} model={lm_parsed.model}")
print(f"metrics: {lm_parsed.metrics}")
lm_run = replay(lm_parsed)
print(f"replayed pass_rate: {lm_run.summary()['pass_rate']}")
print()


by_type = {}
for record in records.records:
    by_type[record["decision_type"]] = by_type.get(record["decision_type"], 0) + 1
print(f"Captured records by type: {by_type}")
print(f"Temp log files under: {workdir}")
