# Prompt versions on every record

Verb: **Review** · Status: experimental · API: `briefcase.prompts.prompt` · Command: `briefcase prompt`

## What it is for

Give each prompt template a name and a version derived from its text, so every captured
record says which prompt produced it. Combined with evaluation datasets, one table answers
"did the new prompt help?".

## The one command

```bash
briefcase prompt list
```

Prompts are registered in code; the version is the first 12 hex characters of the
template's SHA-256, so the same text always has the same version on every machine.

```python
import briefcase
from briefcase.prompts import prompt

V1 = prompt("triage", "Classify the ticket as bug, question or billing.\nTicket: {text}")
V2 = prompt("triage", "Classify the support ticket as exactly one of: bug, question, billing.\n"
                      "Money or charges -> billing.\nTicket: {text}")

@briefcase.capture(decision_type="triage")
def triage(text: str) -> str:
    return call_model(V1.render(text=text))   # this record gets prompt_name / prompt_version
```

A prompt is used by `render(...)` or by `with V1:`. A record is tagged when it is made
inside `with V1:` (so provider records made in the block are tagged), or when the prompt is
used at any point while the record's call runs (so the `triage` record above is tagged). A
call that uses no prompt is not tagged, even right after one that did.

Output below is real, from the demo app used in
`docs/features/datasets-from-records.md`, run in a temporary `HOME` (the scratch directory
is shown as `/tmp/demo`). A captured record:

```json
{"schema_version": "1.2", "decision_id": "af594d6a-f3be-428b-8040-d8546f80bf4b", "decision_type": "triage", "function_name": "triage_v1", "inputs": {"args": "('The app crashes on login',)"}, "outputs": {"result": "'bug'"}, "started_at": "2026-11-22T04:47:04.853290+00:00", "ended_at": "2026-11-22T04:47:04.853298+00:00", "execution_time_ms": 0.008, "prompt_name": "triage", "prompt_version": "1aa8e1bfefb4"}
```

```text
$ briefcase prompt list
  name    versions  latest        first seen
  triage  2         f64e29b2acce  2026-11-22T04:47:04
Next: briefcase prompt show triage
[exit 0]

$ briefcase prompt show triage@1aa8
triage@1aa8e1bfefb4  first seen 2026-11-22T04:47:04+00:00  63 chars
placeholders: text
---
Classify the ticket as bug, question or billing.
Ticket: {text}
---
Next: briefcase dataset from-records runs.jsonl --name triage --where prompt_name=triage
[exit 0]

$ briefcase prompt diff triage@1aa8e1bfefb4 triage@f64e29b2acce
--- triage@1aa8e1bfefb4
+++ triage@f64e29b2acce
@@ -1,2 +1,3 @@
-Classify the ticket as bug, question or billing.
+Classify the support ticket as exactly one of: bug, question, billing.
+Money or charges -> billing.
 Ticket: {text}
Next: briefcase eval run --dataset triage --target <old> --target <new>  (compare both versions)
[exit 0]

$ briefcase dataset from-records runs.jsonl --name triage-v1-prompt --where prompt_name=triage,prompt_version=1aa8e1bfefb4
Created triage-v1-prompt@v1: 8 cases, hash 56bb8587e84e
  source     runs.jsonl  sha256 27205a1945c2  (11 records)
  filters    prompt_name=triage, prompt_version=1aa8e1bfefb4
  reference  recorded output
  sanitized  yes (briefcase.sanitize, native extension)
  3 of 11 records skipped: did not match --where
Next: briefcase eval run --dataset triage-v1-prompt@v1 --target module:function
[exit 0]
```

"Did the new prompt help?": run both versions against the same dataset; rows are split by
the prompt version each call actually used.

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
```

In CI, gate the new prompt and fail if it regresses against the old one (the old one may be
worse, so `--fail-under` is not applied to it):

```bash
briefcase eval run --dataset triage --target app:triage_v1 --target app:triage_v2 \
  --no-regression app:triage_v1 --fail-under 0.9 --gate app:triage_v2
```

## What it writes or sends, and where

| What | Where |
|---|---|
| `prompt_name`, `prompt_version` fields | On each tagged decision record, through your configured exporter (the template text is never put on records) |
| Each distinct template, once | `~/.briefcase/prompts/<name>/<version>.json` (`$BRIEFCASE_HOME` overrides `~/.briefcase`), mode 0600; local only, read by `briefcase prompt` |
| `eval.case` records | carry `prompt_name` / `prompt_version` in `metadata` and a `prompt_version:<v>` tag |

## Defaults and how to change them

| Default | Change it with |
|---|---|
| Templates saved to the local registry | `BRIEFCASE_PROMPT_REGISTRY=off`, or `prompt(name, template, save=False)` |
| `render` uses `str.format` placeholders (`{text}`) | Render yourself and call `with V1:` to tag |
| `prompt show NAME` shows the latest version | `NAME@<version or prefix>` |

## How to turn it off or undo it

- Stop tagging: stop calling `prompt(...)` / `render` / `with`; records then carry no prompt fields.
- Remove saved templates: `rm -r ~/.briefcase/prompts`.

## Limits and failure modes

- Only the template text is hashed; the model, temperature and rendered values are not part
  of the version.
- Records made in a different thread or task than the one that used the prompt are not
  tagged (the active prompt is a context variable); use `with V1:` inside that thread.
- Nested `@capture` calls: the inner record and the outer record are both tagged when the
  inner call uses the prompt.
- Older records (before this feature) have no prompt fields; `--where prompt_name=…` skips them.
- The local registry is per machine; `prompt list` on a CI runner shows only what ran there.

## Troubleshooting

| Symptom | Do this |
|---|---|
| Records have no `prompt_version` | Make sure the prompt is used (`render` or `with`) inside the captured call, in the same thread |
| `briefcase prompt list` says `no prompts recorded yet` | The code that calls `prompt(...)` has not run on this machine, or `BRIEFCASE_PROMPT_REGISTRY=off` is set |
| `(modified on disk)` in `prompt list` | A saved template was edited; delete that file and re-run the code that registers it |
| `version '…' of NAME is ambiguous` | Give more characters of the version |
| Eval table shows `-` in the prompt column | The target never rendered or entered a prompt |
