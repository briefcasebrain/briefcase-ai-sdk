# Eval Runs

Turns evaluation results into Briefcase decision records: log a run live, or
replay a log file an eval framework already wrote.

## Requirements

The parsers never import an evaluation framework, so you can replay an
inspect-ai or lm-eval-harness log without either installed.

```bash
pip install briefcase-ai
```

One exception: inspect-ai's default `.eval` archive stores zstd-compressed zip
entries, which stdlib `zipfile` decodes only on Python 3.14+. On older Pythons
install a backend, or export the log as JSON instead.

```bash
pip install "briefcase-ai[evals]"     # adds zstandard
inspect eval task.py --log-format json  # or avoid the archive entirely
```

## Run

```bash
python examples/eval_runs/main.py
```

Runs offline. The example writes its own sample log files to a temp directory.

## What it shows

| Pattern | API | Emits |
|---------|-----|-------|
| Live logging | `with EvalRun(name) as run: run.log_case(...)` | one `eval.case` per case, one `eval.run` at exit |
| inspect-ai replay | `replay(from_inspect_log(path))` | same, from a `.json` log or `.eval` archive |
| lm-eval replay | `replay(from_lm_eval_results(results, samples))` | same, from `results.json` plus the samples jsonl |

## Record types

- `eval.case`: case id, inputs, outputs, target, scores, pass flag, tokens
- `eval.run`: pass rate, per-score mean/min/max/count, token totals, cost

Both go through the exporter configured by `briefcase.observe(...)`, or one you
pass as `exporter=`.

## Cost and drift

`summary()` estimates cost when the run has a `model` and token counts, using
`briefcase.cost.CostCalculator` unless you inject your own via
`cost_calculator=`. `summary(include_drift=True)` adds output drift metrics
when at least two cases logged string outputs. Both degrade to `None` rather
than raising when the calculator is unavailable.

## Parser scope

Parsing is best-effort over the documented log shapes. A file that does not
match raises `ValueError` naming the expected format rather than returning a
half-parsed run.

Verified against artifacts inspect-ai 0.3.257 and lm-eval-harness 0.4.12
actually wrote; both are checked in under `tests/fixtures/`. Multiple-choice
lm-eval tasks produce one `[logprob, is_greedy]` pair per choice rather than
generated text, so `outputs` holds that list as JSON.
