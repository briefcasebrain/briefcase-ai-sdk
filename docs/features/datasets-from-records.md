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

Output below is real, captured in a temporary `HOME` with the compiled native extension
built from this checkout (`maturin develop`), so `briefcase.sanitize` ran for real. The demo
app has two prompt versions, `triage_v1` (the one "in production") and `triage_v2`; it
captured 8 `triage` records with `capture_content="full"` (one repeated, one containing an
e-mail address and a phone number) and 3 `lookup` records with `capture_content="hash"`.
The scratch directory is shown as `/tmp/demo`.

```text
$ briefcase dataset from-records runs.jsonl --name lookups --where decision_type=lookup
No dataset created: none of the 11 records in runs.jsonl became a case.
  8 of 11 records skipped: did not match --where
  3 of 11 records skipped: captured without content — re-capture with content: briefcase.auto("<provider or framework>", capture_content="full") or BRIEFCASE_CAPTURE_CONTENT=full, `briefcase exec --capture-content full -- <command>`, or @briefcase.capture(capture_content="full")
[exit 1]

$ briefcase dataset from-records runs.jsonl --name triage --where decision_type=triage,status=ok --dedupe
Created triage@v1: 7 cases, hash ebc456c41a1b
  source     runs.jsonl  sha256 27205a1945c2  (11 records)
  filters    decision_type=triage, status=ok
  reference  recorded output
  sanitized  yes (briefcase.sanitize, native extension)
  3 of 11 records skipped: did not match --where
  1 of 11 records skipped: duplicate inputs (--dedupe)
Next: briefcase eval run --dataset triage@v1 --target module:function
[exit 0]

$ briefcase dataset show triage --cases 7
triage@v1  hash ebc456c41a1b  7 cases  created 2026-11-22T04:47:05+00:00
  versions   v1 (latest v1)
  source     runs.jsonl  sha256 27205a1945c2  (11 records)
  filters    decision_type=triage, status=ok
  reference  recorded
  sanitized  yes
  sdk        briefcase-ai 4.4.0, dataset schema 1
  id               input                                            reference
  c-26b681b7db7c   {"args": ["The app crashes on login"]}           'bug'
  c-9dcd36c55d4d   {"args": ["How do I export my data?"]}           'question'
  c-bc5e1f41767c   {"args": ["I was charged twice this month"]}     'bug'
  c-3062b45e6c38   {"args": ["Error 500 when saving"]}              'bug'
  c-d71faf6872ed   {"args": ["Can I get a refund for March?"]}      'bug'
  c-01f7a6f00f75   {"args": ["Invoice for [REDACTED_EMAIL] is wron… 'bug'
  c-0ab06d110b4c   {"args": ["How do I change my password?"]}       'question'
Next: briefcase eval run --dataset triage@v1 --target module:function
[exit 0]
```

The sanitizer ran on every string in the case input and expected output, so the e-mail
address and phone number in the seventh ticket are already `[REDACTED_EMAIL]` and
`[REDACTED_PHONE]` in the version file, in `show` and in `export`; the case id and content
hash are computed from the sanitized text. What it does and does not catch is listed under
"What the sanitizer redacts".

Correcting expected outputs by hand (here: three tickets that production labelled `bug`
are really `billing`) is export, edit, import:

```text
$ briefcase dataset export triage --format jsonl --out triage.jsonl
writing triage.jsonl (7 cases from triage@v1)
Next: edit triage.jsonl, then: briefcase dataset import triage.jsonl --name triage
[exit 0]

$ briefcase dataset import triage.jsonl --name triage
Created triage@v2: 7 cases, hash 41b577a112a6 (imported from triage.jsonl)
  sanitized  yes (briefcase.sanitize, native extension)
Next: briefcase dataset diff triage@v1 triage@v2
[exit 0]

$ briefcase dataset diff triage@v1 triage@v2
triage@v1 (ebc456c41a1b) -> triage@v2 (41b577a112a6)
  +0 added  -0 removed  ~3 changed  4 unchanged
  reference: 'recorded' -> 'provided'
  source: 'records from runs.jsonl' -> 'import of triage.jsonl'
~ c-bc5e1f41767c   reference: 'bug' -> 'billing'
~ c-d71faf6872ed   reference: 'bug' -> 'billing'
~ c-01f7a6f00f75   reference: 'bug' -> 'billing'
[exit 0]
```

Compare the two prompts. With several targets, say what is gated: the old prompt is expected
to score lower, so `--fail-under` alone exits 2 and prints the command to use. Here the new
prompt must reach 90% and must not score below the old one:

```text
$ briefcase eval run --dataset triage --target triage_app:triage_v1 --target triage_app:triage_v2
eval triage@v2 (41b577a112a6): 7 cases, scorer exact
  target                prompt               cases  passed  pass rate  mean score  errors  gate  result
  triage_app:triage_v1  triage@1aa8e1bfefb4  7      4       57.1%      0.57        0       -     -
  triage_app:triage_v2  triage@f64e29b2acce  7      7       100.0%     1.00        0       -     -
Worst failures (triage_app:triage_v1):
  c-01f7a6f00f75   score 0.00  expected 'billing'  got 'bug'
  c-bc5e1f41767c   score 0.00  expected 'billing'  got 'bug'
  c-d71faf6872ed   score 0.00  expected 'billing'  got 'bug'
records: 14 eval.case + 2 eval.run -> /tmp/demo/home/.briefcase/evals/records.jsonl
Next: add --no-regression triage_app:triage_v1 (or --fail-under 0.9 --gate triage_app:triage_v2) to gate CI
[exit 0]

$ briefcase eval run --dataset triage --target triage_app:triage_v1 --target triage_app:triage_v2 --fail-under 0.9
error: --fail-under with 2 targets does not say which must reach it (an old prompt is expected to score lower than a new one). Name the gated target(s):
  briefcase eval run --dataset triage --target triage_app:triage_v1 --target triage_app:triage_v2 --fail-under 0.9 --gate triage_app:triage_v2
or compare against a baseline:
  briefcase eval run --dataset triage --target triage_app:triage_v1 --target triage_app:triage_v2 --no-regression triage_app:triage_v1
(both flags can be combined; repeat --gate for more targets)
[exit 2]

$ briefcase eval run --dataset triage --target triage_app:triage_v1 --target triage_app:triage_v2 --fail-under 0.9 --gate triage_app:triage_v2 --no-regression triage_app:triage_v1
eval triage@v2 (41b577a112a6): 7 cases, scorer exact
  target                prompt               cases  passed  pass rate  mean score  errors  gate                            result
  triage_app:triage_v1  triage@1aa8e1bfefb4  7      4       57.1%      0.57        0       -                               -
  triage_app:triage_v2  triage@f64e29b2acce  7      7       100.0%     1.00        0       >= 90.0% + >= 57.1% (baseline)  PASS
Worst failures (triage_app:triage_v1):
  c-01f7a6f00f75   score 0.00  expected 'billing'  got 'bug'
  c-bc5e1f41767c   score 0.00  expected 'billing'  got 'bug'
  c-d71faf6872ed   score 0.00  expected 'billing'  got 'bug'
records: 14 eval.case + 2 eval.run -> /tmp/demo/home/.briefcase/evals/records.jsonl
PASS  triage_app:triage_v2: pass rate 100.0% >= 90.0% (--fail-under 0.9)
PASS  triage_app:triage_v2: pass rate 100.0% >= 57.1% (baseline triage_app:triage_v1 57.1%, tolerance 0)
Next: briefcase dataset show triage@v2
[exit 0]

$ briefcase eval run --dataset triage --target triage_app:triage_v2 --target triage_app:triage_v1 --no-regression triage_app:triage_v2
eval triage@v2 (41b577a112a6): 7 cases, scorer exact
  target                prompt               cases  passed  pass rate  mean score  errors  gate                  result
  triage_app:triage_v2  triage@f64e29b2acce  7      7       100.0%     1.00        0       -                     -
  triage_app:triage_v1  triage@1aa8e1bfefb4  7      4       57.1%      0.57        0       >= 100.0% (baseline)  FAIL
Worst failures (triage_app:triage_v1):
  c-01f7a6f00f75   score 0.00  expected 'billing'  got 'bug'
  c-bc5e1f41767c   score 0.00  expected 'billing'  got 'bug'
  c-d71faf6872ed   score 0.00  expected 'billing'  got 'bug'
records: 14 eval.case + 2 eval.run -> /tmp/demo/home/.briefcase/evals/records.jsonl
FAIL  triage_app:triage_v1: pass rate 57.1% < 100.0% (baseline triage_app:triage_v2 100.0%, tolerance 0)
Next: briefcase dataset show triage@v2 --cases 20  (inspect the failing inputs)
[exit 1]
```

A single target keeps the short form (`--fail-under` gates it):

```text
$ briefcase eval run --dataset triage --target triage_app:triage_v2 --fail-under 0.9
eval triage@v2 (41b577a112a6): 7 cases, scorer exact
  target                prompt               cases  passed  pass rate  mean score  errors  gate      result
  triage_app:triage_v2  triage@f64e29b2acce  7      7       100.0%     1.00        0       >= 90.0%  PASS
records: 7 eval.case + 1 eval.run -> /tmp/demo/home/.briefcase/evals/records.jsonl
PASS  triage_app:triage_v2: pass rate 100.0% >= 90.0% (--fail-under 0.9)
Next: briefcase dataset show triage@v2
[exit 0]
```

### What the sanitizer redacts

`briefcase.sanitize.Sanitizer` was run on a ticket and on a captured output (both inside
`@capture(capture_content="full")` records, then `dataset from-records` without
`--allow-unsanitized`). Checked against the real extension:

| Content | Result |
|---|---|
| E-mail (`jane.doe@example.com`, `jane.doe+tag@sub.example.co.uk`, upper case) | redacted: `[REDACTED_EMAIL]` |
| US phone (`+1 415-555-0132`, `(415) 555-0132`) | redacted: `[REDACTED_PHONE]` |
| International phone (`+44 20 7946 0958`, `+49 30 901820`), bare digits (`4155550132`) | **not redacted** |
| IBAN (`DE89370400440532013000`, spaced `DE89 3704 0044 …`, `GB82WEST12345698765432`) | redacted: `[REDACTED_IBAN]` |
| AWS access key id (`AKIAIOSFODNN7EXAMPLE`), AWS secret key assignment, GitHub / Slack tokens | redacted: `[REDACTED_API_KEY]` |
| JWT with a full-length signature | redacted: `[REDACTED_API_KEY]` |
| A short, truncated JWT (`eyJ….eyJ….abc`) | **not redacted** |
| SSN, credit card number, IPv4 address | redacted |
| `password=…`, `Authorization: Bearer abc.def.ghi`, PEM private keys | **not redacted** |
| Names, street addresses, dates of birth, passport numbers, free text | **not redacted** |
| Dictionary keys | redacted since this change (values always were) |
| Numbers that are not strings (an `int` phone number) | not touched |

Treat the sanitizer as a safety net for the common patterns, not as anonymization: read the
`dataset show` / `export` output of a new dataset before sharing it, and use
`--redact-fields name,address` for fields you know are personal.

Without the native extension the command refuses (captured on a build that lacked it):

```text
$ briefcase dataset from-records runs.jsonl --name triage
error: the native sanitizer (briefcase.sanitize) is not available, so PII in inputs/outputs would be copied as-is. Install a wheel with the native core (pip install --force-reinstall briefcase-ai) or re-run with --allow-unsanitized
[exit 2]
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
| Exit 0 whatever the pass rate | One target: `--fail-under 0.9` exits 1 below the rate. Several targets: `--fail-under 0.9 --gate NEW` (repeat `--gate`) gates only the named targets; `--no-regression OLD [--tolerance 0.02]` exits 1 when any other target scores below `OLD` by more than the tolerance. Several targets with `--fail-under` and no `--gate` exit 2. Exit codes: 0 pass, 1 gate failed, 2 usage |

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

- Records captured with `capture_content="hash"` or `"none"` (the default for provider and
  framework auto-instrumentation and `briefcase exec`) hold no inputs or outputs; they are counted and skipped, never
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
| `N of M records skipped: captured without content` | Re-capture with `briefcase.auto("<provider or framework>", capture_content="full")` (or `BRIEFCASE_CAPTURE_CONTENT=full`), `briefcase exec --capture-content full -- <command>`, or `@briefcase.capture(capture_content="full")` |
| `No dataset created` | Read the skip lines; loosen `--where`, drop `status=ok`, or use `--reference none` |
| `Unchanged: same cases as NAME@vN` | The filters produced the same cases; nothing to do |
| `was modified after it was created` | Restore the file, or save the edit as a new version: `briefcase dataset import <file> --name NAME` |
| `dataset import` lists `line N: …` | Fix those lines; each line is `{"input": …, "reference": …}` |
| `cannot import 'myapp' for --target` | Run from your project root or set `PYTHONPATH` |
| `has no references` | The dataset was made with `--reference none`: pass `--scorer module:function`, or export, add `reference`, import |
| `error: --fail-under with N targets does not say which must reach it` | Add `--gate TARGET` for the target that must pass, or `--no-regression BASELINE`; the message prints both commands |
| CI fails with `FAIL … pass rate … <` | `briefcase dataset show NAME --cases 20` and the `Worst failures` list |
| `briefcase status` shows datasets as `attention` | The last `eval run` failed a gate (`--fail-under` / `--no-regression`); re-run the `next` command it prints |
