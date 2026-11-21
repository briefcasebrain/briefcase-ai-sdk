# Evaluation datasets from captured records

Verb: **Review** · Status: experimental · Commands: `briefcase dataset`, `briefcase eval run`

## What it is for

Turn the decision records your app already captures into a versioned test set, so a
change to a prompt, model or function is checked against real production inputs. Score
any Python function against that set offline, and fail CI when the pass rate drops.

## The one command

```bash
briefcase dataset from-records runs.jsonl --name triage --where decision_type=triage,status=ok
```

`runs.jsonl` is what `briefcase.observe("runs.jsonl")` writes. Then:

```bash
briefcase eval run --dataset triage --target myapp.triage:classify --fail-under 0.9
```

Output below is real, captured in a temporary `HOME` from the demo app in
`docs/features/prompt-versions.md` (8 `triage` records, 3 `lookup` records captured with
`capture_content="hash"`; the scratch directory is shown as `/tmp/demo`). The native
extension was not built in that environment, so the sanitizer gate is visible.

```text
$ briefcase dataset from-records runs.jsonl --name triage
error: the native sanitizer (briefcase.sanitize) is not available, so PII in inputs/outputs would be copied as-is. Install a wheel with the native core (pip install --force-reinstall briefcase-ai) or re-run with --allow-unsanitized
[exit 2]

$ briefcase dataset from-records runs.jsonl --name triage --where decision_type=triage,status=ok --dedupe --allow-unsanitized
Created triage@v1: 7 cases, hash 7ab9e89f3986
  source     runs.jsonl  sha256 b0774d7402e0  (11 records)
  filters    decision_type=triage, status=ok
  reference  recorded output
  sanitized  NO — --allow-unsanitized given; native sanitizer not available, content copied as recorded
  3 of 11 records skipped: did not match --where
  1 of 11 records skipped: duplicate inputs (--dedupe)
Next: briefcase eval run --dataset triage@v1 --target module:function
[exit 0]

$ briefcase dataset from-records runs.jsonl --name lookups --where decision_type=lookup --allow-unsanitized
No dataset created: none of the 11 records in runs.jsonl became a case.
  8 of 11 records skipped: did not match --where
  3 of 11 records skipped: captured without content — re-capture with content: @briefcase.capture(capture_content="full"), briefcase.auto("<provider>", capture_content=True), or `briefcase exec --capture-content full -- <command>`
[exit 1]

$ briefcase dataset show triage
triage@v1  hash 7ab9e89f3986  7 cases  created 2026-11-21T23:19:24+00:00
  versions   v1 (latest v1)
  source     runs.jsonl  sha256 b0774d7402e0  (11 records)
  filters    decision_type=triage, status=ok
  reference  recorded
  sanitized  no (--allow-unsanitized)
  sdk        briefcase-ai 4.4.0, dataset schema 1
  id               input                                            reference
  c-26b681b7db7c   {"args": ["The app crashes on login"]}           'bug'
  c-9dcd36c55d4d   {"args": ["How do I export my data?"]}           'question'
  c-bc5e1f41767c   {"args": ["I was charged twice this month"]}     'bug'
  c-3062b45e6c38   {"args": ["Error 500 when saving"]}              'bug'
  c-d71faf6872ed   {"args": ["Can I get a refund for March?"]}      'bug'
  … 2 more (--cases N, or --json)
Next: briefcase eval run --dataset triage@v1 --target module:function
[exit 0]
```

Correcting expected outputs by hand (here: three charges that production labelled `bug`
are really `billing`) is export, edit, import:

```text
$ briefcase dataset export triage --format jsonl --out triage.jsonl
writing triage.jsonl (7 cases from triage@v1)
Next: edit triage.jsonl, then: briefcase dataset import triage.jsonl --name triage
[exit 0]

$ briefcase dataset import triage.jsonl --name triage --allow-unsanitized
Created triage@v2: 7 cases, hash 30b18383a34a (imported from triage.jsonl)
  sanitized  NO — --allow-unsanitized given; native sanitizer not available, content copied as recorded
Next: briefcase dataset diff triage@v1 triage@v2
[exit 0]

$ briefcase dataset diff triage@v1 triage@v2
triage@v1 (7ab9e89f3986) -> triage@v2 (30b18383a34a)
  +0 added  -0 removed  ~3 changed  4 unchanged
  reference: 'recorded' -> 'provided'
  source: 'records from runs.jsonl' -> 'import of triage.jsonl'
~ c-bc5e1f41767c   reference: 'bug' -> 'billing'
~ c-d71faf6872ed   reference: 'bug' -> 'billing'
~ c-2fad186dfaf9   reference: 'bug' -> 'billing'
[exit 0]

$ briefcase eval run --dataset triage --target triage_app:triage_v1 --target triage_app:triage_v2
eval triage@v2 (30b18383a34a): 7 cases, scorer exact
  target                prompt               cases  passed  pass rate  mean score  errors
  triage_app:triage_v1  triage@1aa8e1bfefb4  7      4       57.1%      0.57        0
  triage_app:triage_v2  triage@f64e29b2acce  7      7       100.0%     1.00        0
Worst failures (triage_app:triage_v1):
  c-2fad186dfaf9   score 0.00  expected 'billing'  got 'bug'
  c-bc5e1f41767c   score 0.00  expected 'billing'  got 'bug'
  c-d71faf6872ed   score 0.00  expected 'billing'  got 'bug'
records: 14 eval.case + 2 eval.run -> /tmp/demo/home/.briefcase/evals/records.jsonl
Next: add --fail-under 0.9 to gate CI on this pass rate
[exit 0]

$ briefcase eval run --dataset triage --target triage_app:triage_v2 --fail-under 0.9
eval triage@v2 (30b18383a34a): 7 cases, scorer exact
  target                prompt               cases  passed  pass rate  mean score  errors
  triage_app:triage_v2  triage@f64e29b2acce  7      7       100.0%     1.00        0
records: 7 eval.case + 1 eval.run -> /tmp/demo/home/.briefcase/evals/records.jsonl
PASS  triage_app:triage_v2: pass rate 100.0% >= 90.0% (--fail-under 0.9)
Next: briefcase dataset show triage@v2
[exit 0]
```

## What it writes or sends, and where

| What | Where | Content? |
|---|---|---|
| Dataset versions | `~/.briefcase/datasets/<name>/v<N>.jsonl` (`$BRIEFCASE_HOME` overrides `~/.briefcase`), mode 0400 | Yes: case inputs and expected outputs, sanitized when the native extension is present |
| Eval records (`eval.case` per case, `eval.run` per target) | `--to`, else `$BRIEFCASE_EXPORTER` / `OTEL_EXPORTER_OTLP_ENDPOINT`, else `~/.briefcase/evals/records.jsonl` | No: SHA-256 digests of input, output and reference, unless `--capture-content` |
| Eval history (for `briefcase status`) | `~/.briefcase/evals/history.jsonl` | No: counts, pass rates, target names |
| Exports | stdout, or the `--out` path you give | Yes |

Nothing is sent over the network unless you point `--to` or `BRIEFCASE_EXPORTER` at a URL.

A version file is the editable format: a header line, then one case per line.

```json
{"briefcase_dataset": {"name": "triage", "version": "v1", "hash": "7ab9…", "source": {"file": "runs.jsonl", "sha256": "b077…", "records": 11}, "filters": ["decision_type=triage", "status=ok"], "sample": null, "reference": "recorded", "sanitized": false, "schema_version": "1", "sdk_version": "4.4.0", "created_at": "…"}}
{"id": "c-26b681b7db7c", "input": {"args": ["The app crashes on login"]}, "reference": "bug", "metadata": {"decision_id": "…", "prompt_name": "triage", "prompt_version": "1aa8e1bfefb4"}, "content_sha256": "…"}
```

Only `input` is required when you write cases by hand; `id` defaults to a hash of the
input and `content_sha256` is always recomputed.

## Defaults and how to change them

| Default | Change it with |
|---|---|
| Every record except `eval.case` / `eval.run` | `--where KEY=GLOB` (repeat or comma-separate; `KEY!=GLOB` negates). Keys: any record field or dotted path, plus `status` (`ok`/`error`/`cancelled`), `model`, `prompt_name`, `prompt_version` |
| All matching records | `--sample N --seed S` (seed defaults to 0, so the draw is repeatable) |
| Duplicates kept (ids get `-2`, `-3`) | `--dedupe` keeps the first case per input |
| Recorded output is the expected output | `--reference none`: no expected output; label later via export/edit/import |
| Sanitize with `briefcase.sanitize` | Required when the native extension is missing: `--allow-unsanitized` |
| No field redaction | `--redact-fields email,phone` replaces those keys anywhere with `[REDACTED]` |
| Scorer `exact` (strings compared after trimming whitespace) | `--scorer contains`, `json` (parse and compare), or `module:function(output, reference, case)` returning a bool, a number (passes at >= 1.0) or `{"score": …, "passed": …}` |
| One case at a time | `--concurrency N` (threads; results and order are identical) |
| Exit 0 whatever the pass rate | `--fail-under 0.9`: exit 1 when any `--target` is below the rate |

Calling convention: an input `{"args": [...], "kwargs": {...}}` (what `@capture` records
decode to) is called as `target(*args, **kwargs)`; any other input (provider requests,
hand-written cases) is passed as one argument. Async functions are awaited.

Export formats:

- `jsonl`: the format above; `briefcase dataset import` reads it back.
- `inspect`: one inspect-ai `Sample` per line (`id`, `input`, `target`, `metadata`); load with
  `json_dataset("triage.inspect.jsonl")`. Checked by loading and running it with inspect-ai
  0.3.291.
- `lm-eval` (experimental): `--out triage.jsonl` writes the docs and `triage.yaml`, a
  `generate_until` task with `exact_match`. Checked with lm-eval 0.4.13 and its `dummy` model
  (`lm_eval --model dummy --include_path <dir> --tasks briefcase_triage_v2`); not run with a
  real model.

## How to turn it off or undo it

- Delete a dataset: `rm -r ~/.briefcase/datasets/<name>` (versions are read-only files, so
  use `rm -rf` or `chmod u+w` first).
- Delete eval records and history: `rm -r ~/.briefcase/evals`.
- Exports are plain files at the path you chose; delete them.
- Nothing runs in the background and no configuration is written.

## Limits and failure modes

- Records captured with `capture_content="hash"` or `"none"` (the default for provider
  auto-instrumentation) hold no inputs or outputs; they are counted and skipped, never
  turned into empty cases. A run that would produce zero cases exits 1 and writes nothing.
- `@capture` stores reprs truncated at `max_input_chars` (default 1000). A repr that is not
  a Python literal (custom objects, truncated text) becomes `{"args_text": "..."}` and the
  case is marked `input_decoded: false`; call your target with that text or write the case
  input by hand.
- Errored or cancelled records have no recorded output: with `--reference recorded` they
  are skipped, with `--reference none` they are kept.
- Running `from-records` again on the same data writes no new version; versions are only
  ever added. An edited version file is detected (hash mismatch) and refused.
- A dataset name already used by `briefcase dataset register` (evaluation-engine datasets)
  is refused; `briefcase run submit` does not read local versions.
- The target runs in this process with your environment: a target that calls a paid model
  costs money per case.

## Troubleshooting

| Symptom | Do this |
|---|---|
| `error: the native sanitizer … is not available` | Install a wheel with the native core (`pip install --force-reinstall briefcase-ai`), or accept unsanitized content with `--allow-unsanitized` |
| `N of M records skipped: captured without content` | Re-capture with `@briefcase.capture(capture_content="full")`, `briefcase.auto("<provider>", capture_content=True)` or `briefcase exec --capture-content full -- <command>` |
| `No dataset created` | Read the skip lines; loosen `--where`, drop `status=ok`, or use `--reference none` |
| `Unchanged: same cases as NAME@vN` | The filters produced the same cases; nothing to do |
| `was modified after it was created` | Restore the file, or save the edit as a new version: `briefcase dataset import <file> --name NAME` |
| `dataset import` lists `line N: …` | Fix those lines; each line is `{"input": …, "reference": …}` |
| `cannot import 'myapp' for --target` | Run from your project root or set `PYTHONPATH` |
| `has no references` | The dataset was made with `--reference none`: pass `--scorer module:function`, or export, add `reference`, import |
| CI fails with `FAIL … pass rate … <` | `briefcase dataset show NAME --cases 20` and the `Worst failures` list |
| `briefcase status` shows datasets as `attention` | The last `eval run` was below `--fail-under`; re-run the `next` command it prints |
