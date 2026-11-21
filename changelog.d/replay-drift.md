### Added

- `briefcase replay run|explain|diff` and `briefcase.replay.recorded`: replay LLM calls captured
  by `briefcase.auto(capture_content=True)` from a JSONL file, through the user's installed
  OpenAI, Anthropic, google-genai or Bedrock SDK (`--executor auto`) or a no-network cassette
  (`--executor cassette`). Shows a plan with per-model counts and a token bound and asks before
  live calls (`--yes`, `--max-calls`, `--limit`, `--sample`, `--concurrency`, `--max-retries`).
  Compares answers at exact, normalized, JSON-structural and optional `--scorer` levels, with
  structural tool-call comparison, unified diffs, and token, cost and latency deltas. Records
  that cannot be replayed (content off or hashed, truncated or binary content, partial streams,
  unanswered tool calls, failed originals) are reported with the reason. What-if replays with
  `--model`, `--param`, `--drop-param`. Replays are not re-captured as decisions and never run
  tools.
- `briefcase drift baseline save|check` and `briefcase drift baselines`, plus
  `briefcase.drift.baseline`: save a content-free baseline profile to
  `~/.briefcase/baselines/<name>.json` and compare a later window against it: PSI and two-sample
  KS for latency, output tokens and output length; mean tokens and cost per call; error, refusal
  and finish-reason rates (two-proportion z test); model mix and categorical output shift
  (Jensen-Shannon, chi-square); character n-gram lexical drift; optional `embed=` hook. Signals
  under `min_n` records are refused, not scored. `--fail-on attention` makes it a CI gate.
- Feature manifests `replay-recorded` (verify) and `drift-baseline` (review), with `status()`
  hooks for `briefcase status`.

### Changed

- `briefcase.replay` and `briefcase.drift` resolve their native names (`ReplayEngine`,
  `ReplayPolicy`, `ReplayResult`, `ReplayStats`, `DriftCalculator`, `DriftMetrics`) on first use,
  so the new pure-Python submodules import without the compiled core. Using a native name without
  the core still raises the same `ImportError` (now at `from briefcase.replay import ReplayEngine`
  rather than at `import briefcase.replay`).
