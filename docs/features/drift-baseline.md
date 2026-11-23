# Baseline drift

Save what "normal" looked like for your LLM calls, then check a later window against it:
latency, tokens, cost, errors, refusals, finish reasons, model mix and how the outputs
themselves are distributed. It runs as a report or as a CI gate.

Flow: **Review**. Feature id: `drift-baseline`. Status: experimental.

The existing `briefcase.drift.DriftCalculator` measures consistency *inside one batch*; this
compares a window *against a saved baseline*. It is pure Python and uses only the standard
library.

## The one command

```bash
briefcase-ai drift baseline save prod --from runs.jsonl --last 500
briefcase-ai drift check prod --from today.jsonl --fail-on attention
```

`--from` takes any JSONL file of decision records (`briefcase.observe("runs.jsonl")`). Select
records with `--where KEY=GLOB` (dotted keys, e.g. `model_parameters.model_name=gpt-4o*`),
`--since` / `--until` (ISO time or an age such as `24h`, `7d`) and `--last N`.

### Real output

Captured on 2026-11-21 in a temporary `HOME` (shown as `/tmp/home`) with **synthetic records**:
400 classification-style records over five days for the baseline file, and a "today" file of 150
records generated with slower latency, an 8% error rate, a second model on a third of the calls
and a new output label. The `ok` run checks the baseline file's own later days (not part of the
saved baseline). `briefcase-ai drift ...` was invoked through the command module's `register()`
(what the CLI's command discovery does). Lines for signals that were `ok` were removed from the
first check below for length; nothing else was edited.

```text
$ briefcase-ai drift baselines
No baselines in /tmp/home/.briefcase/baselines.
Next: briefcase-ai drift baseline save prod --from runs.jsonl
[exit 0]

$ briefcase-ai drift baseline save prod --from prod-week.jsonl --until 2026-11-13T00:00:00Z
Saved baseline 'prod': 256 records, 2026-11-10T00:00:00+00:00 .. 2026-11-12T23:11:00+00:00
  file      /tmp/home/.briefcase/baselines/prod.json
  signals   categories, latency_ms, lexical, output_chars, output_tokens, prompt_tokens, total_tokens
  content   none stored (aggregates and hashes only)
Next: briefcase-ai drift check prod --from FILE --since 24h
[exit 0]

$ briefcase-ai drift check prod --from today.jsonl --fail-on attention
Drift check: baseline 'prod' (256 records) vs window (150 records): ATTENTION
  ATTENTION  latency_ms.psi             p95 latency rose from 1.46 s to 3.72 s; p50 rose from 1.14 s to 2.78 s (PSI 8.253)
  ATTENTION  latency_ms.ks              p95 latency rose from 1.46 s to 3.72 s; p50 rose from 1.14 s to 2.78 s (KS D 0.993; asymptotic 5% critical value 0.140)
  ATTENTION  output_chars.ks            p95 output length stayed at 8.00; p50 fell from 8.00 to 7.00 (KS D 0.273; asymptotic 5% critical value 0.140)
  ATTENTION  error.rate                 error rate rose from 0.0% to 8.7% (two-proportion z = 4.79, p = 1.69e-06)
  ATTENTION  model_mix                  model mix JSD 0.169; new: gpt-4.1-mini (30%)
  ATTENTION  output_categories          label distribution JSD 0.156 over 3 baseline labels; 27% of window outputs are labels never seen in the baseline (chi-square 79.24, df 3, p = 4.46e-17)
  ATTENTION  output_lexical             character 3-gram JSD 0.109 between baseline and window outputs (baseline split-half noise floor 0.007)
Next: briefcase-ai replay run today.jsonl --sample 20   (re-run a sample to see what changed)
[exit 1]

$ briefcase-ai drift check prod --from today-small.jsonl --fail-on attention
Drift check: baseline 'prod' (256 records) vs window (12 records): not scored
  (14 signal(s) not scored; --all lists them with the reason)
  Nothing was scored: each signal needs at least 30 records on both sides.
Next: briefcase-ai drift check prod --from today-small.jsonl --since 7d
[exit 0]

$ briefcase-ai drift check prod --from today-small.jsonl --all
Drift check: baseline 'prod' (256 records) vs window (12 records): not scored
  not scored latency_ms.psi             not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored latency_ms.ks              not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored output_tokens.psi          not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored output_tokens.ks           not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored output_chars.psi           not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored output_chars.ks            not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored prompt_tokens.mean         not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored total_tokens.mean          not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored error.rate                 not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored refusal.rate               not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored finish_reason.stop         not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored model_mix                  not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored output_categories          not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  not scored output_lexical             not scored: needs at least 30 records on each side, has 256 in the baseline and 12 in the window
  Nothing was scored: each signal needs at least 30 records on both sides.
Next: briefcase-ai drift check prod --from today-small.jsonl --since 7d
[exit 0]

$ briefcase-ai drift check prod --from prod-week.jsonl --since 2026-11-13T00:00:00Z --fail-on attention
Drift check: baseline 'prod' (256 records) vs window (144 records): ok
  ok         latency_ms.psi             p95 latency rose from 1.46 s to 1.48 s; p50 rose from 1.14 s to 1.16 s (PSI 0.040)
  ok         latency_ms.ks              p95 latency rose from 1.46 s to 1.48 s; p50 rose from 1.14 s to 1.16 s (KS D 0.067; asymptotic 5% critical value 0.141)
  ok         output_tokens.psi          p95 output tokens stayed at 3.00; p50 stayed at 2.00 (PSI 0.003)
  ok         output_tokens.ks           p95 output tokens stayed at 3.00; p50 stayed at 2.00 (KS D 0.027; asymptotic 5% critical value 0.141)
  ok         output_chars.psi           p95 output length stayed at 8.00; p50 stayed at 8.00 (PSI 0.001)
  ok         output_chars.ks            p95 output length stayed at 8.00; p50 stayed at 8.00 (KS D 0.024; asymptotic 5% critical value 0.141)
  ok         prompt_tokens.mean         mean prompt tokens per call stayed at 200
  ok         total_tokens.mean          mean total tokens per call stayed at 202
  ok         error.rate                 error rate stayed at 0.0% (two-proportion z = 0.00, p = 1; few events, the normal approximation is rough)
  ok         refusal.rate               refusal rate stayed at 0.0% (two-proportion z = 0.00, p = 1; few events, the normal approximation is rough)
  ok         finish_reason.stop         share of finish_reason=stop stayed at 100.0% (two-proportion z = 0.00, p = 1; few events, the normal approximation is rough)
  ok         model_mix                  model mix JSD 0.000
  ok         output_categories          label distribution JSD 0.000 over 3 baseline labels; 0% of window outputs are labels never seen in the baseline (chi-square 0.18, df 2, p = 0.915)
  ok         output_lexical             character 3-gram JSD 0.000 between baseline and window outputs (baseline split-half noise floor 0.007)
Next: briefcase-ai drift check prod --from FILE --fail-on attention   (add to CI)
[exit 0]

$ briefcase-ai drift baselines
  prod                    256 records  2026-11-10T00:00:00+00:00 .. 2026-11-12T23:11:00+00:00  models: gpt-4o-mini
Next: briefcase-ai drift check prod --from FILE
[exit 0]
```

## What it writes, and where

| What | Where | Content? |
|---|---|---|
| Baseline profile | `~/.briefcase/baselines/<name>.json` (`$BRIEFCASE_HOME` overrides `~/.briefcase`), mode 0600 | **no** |
| Last check, for `briefcase-ai status` | `~/.briefcase/drift/last_check.json` | no: state and flagged signal names |

Nothing is sent anywhere. A profile holds only aggregates and hashes, so it is safe to commit or
share: record counts; a 101-point quantile table, mean, p50, p95 and ten-bin proportions for
latency, output tokens and output length; mean prompt/total tokens and cost per call; error,
refusal and finish-reason counts; model names and counts; for classification-like outputs, the
first 16 hex digits of the SHA-256 of each normalized label with its count; character 3-gram
counts hashed (CRC-32) into 4096 bins; and, only if you pass `embed=`, the mean embedding
vector. The source file is recorded by basename and SHA-256, never by path.

One caveat: a hash of a short label ("positive") can be guessed by hashing candidate words. Use
`--no-categories` if your labels are themselves sensitive.

## Signals and formulas

Every signal reports `value`, `threshold`, `n_baseline`, `n_window` and a plain reading
("p95 latency rose from 1.46 s to 3.72 s"). States: `ok`, `attention`, `insufficient_data` (not
scored) and `unavailable` (the records do not carry the field).

| Signal | Test | Default threshold |
|---|---|---|
| `latency_ms`, `output_tokens`, `output_chars` `.psi` | Population stability index over the baseline's ten quantile bins: `PSI = sum((a - e) ln(a / e))`, empty bins floored at 1e-4 | `psi` 0.25 |
| same `.ks` | Two-sample Kolmogorov-Smirnov `D` = the largest absolute gap between the two empirical CDFs, baseline CDF read from its quantile table | `ks` 0.2 **and** above the asymptotic 5% critical value `1.358 * sqrt((n+m)/(n m))` |
| `prompt_tokens`, `total_tokens`, `cost_usd` `.mean` | relative change of the mean per call | `mean_rel_change` 0.25 |
| `error.rate`, `refusal.rate`, `finish_reason.<r>` | two-proportion z test, `z = (p2 - p1) / sqrt(p (1-p) (1/n1 + 1/n2))`, two-sided p | `rate_alpha` 0.01 and at least `rate_min_abs` 0.02 change; finish reasons Bonferroni-corrected |
| `model_mix` | Jensen-Shannon divergence (base 2) of model counts; new models listed | `model_mix_jsd` 0.1, or a new model above `model_new_share` 5% |
| `output_categories` | JSD of hashed label counts, with Pearson chi-square (df = labels - 1) reported | `category_jsd` 0.1 |
| `output_lexical` | JSD of hashed character 3-gram counts | `lexical_jsd` 0.1, raised to 1.5 x the baseline's split-half noise floor |
| `output_embedding` | cosine distance between the baseline and window centroids of your `embed=` vectors | `embedding_distance` 0.05 |

Refusals are finish reasons that mean the provider refused or filtered, as found in the installed
SDKs: `content_filter` (openai), `refusal` (anthropic), `SAFETY`, `RECITATION`, `BLOCKLIST`,
`PROHIBITED_CONTENT`, `SPII`, `IMAGE_SAFETY`, `IMAGE_PROHIBITED_CONTENT`, `MODEL_ARMOR`
(google-genai), `guardrail_intervened`, `content_filtered` (Bedrock), plus any record whose
output carries a `refusal`.

Outputs count as classification-like when the baseline has 2 to 20 distinct normalized outputs,
none longer than 40 characters. Output length, categories and the lexical signal need records
captured with content; latency, tokens, rates and model mix do not.

### What the numbers do and do not claim

- PSI and the JSD thresholds are **rules of thumb**, not tests: PSI under 0.1 is usually read as
  stable and above 0.25 as a large shift. Both are biased upwards with few records per bin.
- The KS critical value, two-proportion z and chi-square p-values are the textbook asymptotic
  tests. They are approximate with small samples and with ties (KS), with fewer than about five
  events on a side (z; the reading says "few events"), and with expected counts under five
  (chi-square; the reading says so). The KS statistic against a saved profile uses the baseline's
  quantile table, which is accurate to about 0.01.
- Many signals are checked at once; expect occasional false alarms at the chosen levels.
- **Small samples are refused**: below `min_n` (default 30) records on either side a signal is
  `insufficient_data` and is not scored, and the reading says how many records each side had.

## Defaults and how to change them

Thresholds are listed above (`briefcase.drift.baseline.DEFAULT_THRESHOLDS`). Change any of them
per run with `--threshold KEY=VALUE` (repeatable, e.g. `--threshold psi=0.1 --threshold
min_n=50`), or in Python with `check(profile, records, thresholds={{...}})`. `--fail-on` decides
the exit code: `never` (default, report only), `attention` (exit 1 when any signal crosses its
threshold) or `insufficient` (also exit 1 when nothing could be scored). An embedding hook is
`--embed mypkg.emb:embed` on both `save` and `check` (`embed(list_of_texts) -> list of vectors`).

Python API:

```python
from briefcase.drift.baseline import build_profile, check, save_profile, select_records

profile = build_profile(select_records("runs.jsonl", last=500), "prod")
save_profile(profile)
result = check(profile, select_records("today.jsonl"))
```

## How to turn it off or undo it

Delete `~/.briefcase/baselines/<name>.json` (and `~/.briefcase/drift/` for the status summary).
Nothing else is changed.

## Limits and failure modes

- A baseline is only as good as its window: save it from a period you consider normal and with
  well over `min_n` records.
- Window and baseline are compared as wholes; a shift confined to one route or prompt is diluted.
  Narrow both with `--where`.
- Output length, categories and lexical drift need captured content; without it those signals
  are `unavailable` and only operational signals are scored.
- Cost needs a `cost_usd` (or `cost`) field on the records; no prices are bundled.
- The lexical signal measures surface wording, not meaning; a paraphrase can drift lexically while
  meaning the same thing. Use `embed=` for meaning.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "not scored: needs at least 30 records on each side" | Widen the window (`--since 7d`) or the baseline; lowering `--threshold min_n=` trades away reliability. |
| Everything is `unavailable` for output signals | Records were captured without content; latency, tokens and rates still work. |
| CI job never fails | Add `--fail-on attention`; the default only reports. |
| "no baseline named ..." | `briefcase-ai drift baselines` lists saved names; save one with `drift baseline save`. |
| "no records matched the selection" | Check `--where` keys (dotted, glob values) and the `--since`/`--until` times (UTC). |
| A known-good window shows `attention` | Read the signal's reading and sample sizes; raise that threshold with `--threshold`, or re-save the baseline if "normal" has changed. |
| `output_embedding` is `unavailable` | The baseline has a centroid; pass the same `--embed` to `check`. |
