"""Evaluation-harness bridge: turn eval results into Briefcase decision records.

No evaluation framework is imported, so this package works from a bare
`pip install briefcase-ai`. One case needs help: inspect-ai `.eval` archives
store zstd-compressed zip entries, which stdlib zipfile decodes only on Python
3.14+. The `evals` extra adds `zstandard` for older Pythons; without a backend,
`.eval` parsing raises with instructions and every other path still works.

Records use two decision types: ``"eval.case"`` per case and ``"eval.run"`` per
run. Both flow through the configured exporter like any `@briefcase.capture`
record.

Usage:
    1. Log a run as it happens:

        from briefcase.integrations.evals import EvalRun

        with EvalRun("gsm8k", model="claude-opus-5") as run:
            run.log_case("q1", inputs=question, outputs=answer, passed=True)

    2. Replay an inspect-ai log (.json or .eval archive):

        from briefcase.integrations.evals import from_inspect_log, replay

        replay(from_inspect_log("logs/2026-08-12_gsm8k.eval"))

    3. Replay lm-eval-harness results, with per-sample cases:

        from briefcase.integrations.evals import from_lm_eval_results, replay

        parsed = from_lm_eval_results("results.json", "samples_gsm8k.jsonl")
        replay(parsed, exporter=my_exporter)

    4. Feed parsed cases into a run you control:

        run = EvalRun("nightly", exporter=my_exporter)
        run.ingest(parsed.cases)
        print(run.finish()["outputs"]["pass_rate"])
"""

from briefcase.integrations.evals.parsers import (
    ParsedEvalLog,
    from_inspect_log,
    from_lm_eval_results,
    replay,
)
from briefcase.integrations.evals.run import EvalRun

__all__ = [
    "EvalRun",
    "ParsedEvalLog",
    "from_inspect_log",
    "from_lm_eval_results",
    "replay",
]
