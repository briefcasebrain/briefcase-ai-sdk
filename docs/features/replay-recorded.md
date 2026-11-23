# Replay recorded calls

Re-run LLM calls you already captured and see exactly what changed: the answer, the tool
calls, the tokens, the cost and the latency. Use it before a model upgrade, after a prompt
change, or as a no-network regression check in CI.

Flow: **Verify**. Feature id: `replay-recorded`. Status: experimental.

## The one command

```bash
briefcase-ai replay run runs.jsonl
```

`runs.jsonl` is a file of decision records written by provider auto-instrumentation **with
content on**:

```python
import briefcase
briefcase.observe("runs.jsonl")
briefcase.auto(capture_content=True)   # content is off by default (or BRIEFCASE_CAPTURE_CONTENT=full); replay needs the request
```

Start with `briefcase-ai replay explain runs.jsonl` to see which records can be replayed and why
the others cannot. It reads only the file.

### Real output

Captured on 2026-11-21 in a temporary `HOME`, with **synthetic records** (six hand-made
records in the layout `briefcase.auto` writes: three replayable OpenAI chat calls, one captured
with content off, one stream that was not read to the end, one Anthropic conversation ending in
an unanswered tool call) and the **cassette executor**, so no provider was called. The
"changed" run uses a cassette whose three answers were edited by hand to simulate a model that
now answers differently. `briefcase-ai replay ...` was invoked through the command module's
`register()` (what the CLI's command discovery does).

```text
$ briefcase-ai replay explain runs.jsonl
3 of 6 record(s) can be replayed.
  ok   #0 0000a7c2 openai.chat gpt-4o-mini
  ok   #1 1111a7c2 openai.chat gpt-4o-mini
  ok   #2 2222a7c2 openai.chat gpt-4o-mini
  no   #3 3333a7c2 openai.chat gpt-4o-mini
         - content capture was off when this call was recorded (the default), so the request (messages) is not in the record; re-capture with briefcase.auto(capture_content=True)
  no   #4 4444a7c2 openai.chat gpt-4o-mini
         - the stream was not read to the end when it was recorded, so the recorded output is partial
  no   #5 5555a7c2 anthropic.messages claude-haiku-test
         - the conversation ends in tool calls with no tool results (get_weather); replay never runs tools, so the request cannot be completed
note (#0, #1, #2): capture does not record response_format, tool_choice, stop, parallel_tool_calls, logit_bias, prediction, reasoning_effort (non-string), web_search_options, modalities, audio, functions, function_call, moderation; if the original call set any of them, pass it with --param
Next: briefcase-ai replay run runs.jsonl --plan-only
[exit 0]

$ briefcase-ai replay run runs.jsonl --executor cassette --save-cassette cassette.jsonl --out report.jsonl
Replay plan (cassette executor, no network)
  records in file      6
  replayable           3  (3 not replayable: see `briefcase-ai replay explain`)
  calls to make        3
    gpt-4o-mini                  3
  tokens (upper bound) 252  (recorded prompt tokens + each call's max-tokens cap)
  matched (exact)  #0 openai.chat gpt-4o-mini
  matched (exact)  #1 openai.chat gpt-4o-mini
  matched (exact)  #2 openai.chat gpt-4o-mini
Result: 3 matched, 0 changed, 3 not_replayable, 0 error
  total tokens delta     +0
  mean latency delta     +0.0 ms
Report: report.jsonl
Next: briefcase-ai replay diff report.jsonl
[exit 0]
```

```text
$ briefcase-ai replay run runs.jsonl --executor cassette --cassette cassette-changed.jsonl --out report.jsonl --fail-on-change
Replay plan (cassette executor, no network)
  records in file      6
  replayable           3  (3 not replayable: see `briefcase-ai replay explain`)
  calls to make        3
    gpt-4o-mini                  3
  tokens (upper bound) 252  (recorded prompt tokens + each call's max-tokens cap)
  changed           #0 openai.chat gpt-4o-mini, tokens +2
  matched (normalized)  #1 openai.chat gpt-4o-mini
  matched (json)  #2 openai.chat gpt-4o-mini
Result: 2 matched, 1 changed, 3 not_replayable, 0 error
  total tokens delta     +2
  mean latency delta     +110.0 ms
Report: report.jsonl
Next: briefcase-ai replay diff report.jsonl
[exit 1]

$ briefcase-ai replay diff report.jsonl
6 call(s): 2 matched, 1 changed, 3 not_replayable, 0 error

#0 openai.chat gpt-4o-mini -> gpt-4o-mini: changed
  checks: exact=no, json=n/a, normalized=no, semantic=n/a
  tokens: completion_tokens +2, prompt_tokens +0, total_tokens +2
  latency: +330.0 ms
  --- recorded
  +++ replay
  @@ -1 +1 @@
  -Paris
  +Paris, France.

#3 openai.chat gpt-4o-mini -> gpt-4o-mini: not_replayable
  - content capture was off when this call was recorded (the default), so the request (messages) is not in the record; re-capture with briefcase.auto(capture_content=True)

#4 openai.chat gpt-4o-mini -> gpt-4o-mini: not_replayable
  - the stream was not read to the end when it was recorded, so the recorded output is partial

#5 anthropic.messages claude-haiku-test -> claude-haiku-test: not_replayable
  - the conversation ends in tool calls with no tool results (get_weather); replay never runs tools, so the request cannot be completed

Next: briefcase-ai replay explain FILE   (why records were not replayable)
[exit 0]

$ briefcase-ai replay run runs.jsonl --executor cassette --model gpt-4.1-mini
Replay plan (cassette executor, no network)
  records in file      6
  replayable           3  (3 not replayable: see `briefcase-ai replay explain`)
  blocked by executor  3
    3 x the built-in cassette only holds the recorded answer to the recorded request; --model/--param need a live executor (--executor auto) or a --cassette file with entries for the changed request
  calls to make        0
Result: 0 matched, 0 changed, 6 not_replayable, 0 error
Next: briefcase-ai replay run runs.jsonl --out report.jsonl
[exit 0]

$ briefcase-ai replay run runs.jsonl --plan-only --model gpt-4.1-mini --param temperature=0.5
Replay plan (auto executor, live calls: spends your provider credits)
  records in file      6
  replayable           3  (3 not replayable: see `briefcase-ai replay explain`)
  calls to make        3
    gpt-4.1-mini                 3
  tokens (upper bound) 252  (recorded prompt tokens + each call's max-tokens cap)
Next: briefcase-ai replay run runs.jsonl --model gpt-4.1-mini --param temperature=0.5 --yes
[exit 0]

$ briefcase-ai replay run runs.jsonl --model gpt-4.1-mini
Replay plan (auto executor, live calls: spends your provider credits)
  records in file      6
  replayable           3  (3 not replayable: see `briefcase-ai replay explain`)
  calls to make        3
    gpt-4.1-mini                 3
  tokens (upper bound) 252  (recorded prompt tokens + each call's max-tokens cap)
error: live replay needs confirmation and there is no terminal to ask
  fix: briefcase-ai replay run runs.jsonl --yes   (or --executor cassette)
[exit 1]
```

## What it reads, writes and sends

| What | Where | Content? |
|---|---|---|
| Reads your records | the file you name | yes, it needs the captured request |
| Report (`--out FILE`) | the file you name, mode 0600 | yes: recorded and replayed answers and their diff |
| Cassette (`--save-cassette FILE`) | the file you name, mode 0600 | yes: the replayed answers, keyed by a SHA-256 request fingerprint |
| Last-run summary for `briefcase-ai status` | `~/.briefcase/replay/last_run.json` | no: counts, executor, file basename |
| Live calls (`--executor auto`) | the provider of each record, through **your installed SDK** and its usual credentials (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`/`GEMINI_API_KEY`, AWS credentials) | the recorded request |

Live replays add the header `x-briefcase-replay: <run id>` to OpenAI, Anthropic and Gemini
requests so they can be told apart downstream (not to Bedrock: those requests are SigV4-signed
and that path was not verified). They are **not captured as new decisions**: replay calls the
SDK method underneath any `briefcase.auto` patch. `briefcase.replay.recorded.replaying()`
returns the run id while a replay call is running. **Tools are never run**: a tool call in an
answer is compared by name and arguments.

## What is pinned, and what cannot be

Every parameter the record carries is sent again: model (the requested name; `--served-model`
pins the dated version the provider reported), temperature, top_p, max tokens, seed where the
API has one (OpenAI chat/completions, Gemini), and the recorded `tools`. Capture does not record
some request settings; if the original call set them the replay sends the API default instead,
and `explain` lists them per record. Pass them with `--param`:

| API | Not recorded by capture |
|---|---|
| OpenAI chat | `response_format`, `tool_choice`, `stop`, `parallel_tool_calls`, `logit_bias`, `prediction`, `web_search_options`, `modalities`, `audio`, `functions`, `function_call`, `moderation` |
| OpenAI Responses | `text` (structured output format), `tool_choice`, `reasoning`, `previous_response_id`, `conversation`, `include`, `prompt`, `max_tool_calls`, `parallel_tool_calls`, `context_management`, `moderation`, `image` |
| OpenAI completions | `stop`, `suffix`, `logit_bias`, `best_of`, `echo` |
| Anthropic Messages | `tool_choice`, `stop_sequences`, `thinking`, `output_config` other than `effort`, `container` |
| Gemini | `config.tools`, `tool_config`, `response_mime_type`, `response_schema`, `response_json_schema`, `response_modalities`, `safety_settings`, `stop_sequences`, `thinking_config`, `cached_content`, `automatic_function_calling`, `routing_config`, `model_selection_config` |
| Bedrock Converse | `toolConfig`, `inferenceConfig.stopSequences`, `additionalModelRequestFields`, `guardrailConfig`, `outputConfig`, `promptVariables` |

Every request setting in each provider's published spec is either recorded, listed here, or
cannot change the answer replay compares (for example `metadata`, `user`, `store`, cache keys);
`tests/contracts` checks this against the pinned specs (`docs/features/provider-contracts.md`).

A recorded parameter the installed SDK no longer accepts is reported, not silently dropped
(for example `temperature` on `anthropic` 2.12.0, whose `create()` does not take it); drop it
explicitly with `--drop-param temperature`.

Request shapes were read from the installed SDKs: `openai` 3.57.0, `anthropic` 2.12.0,
`google-genai` 3.6.1, `boto3`/`botocore` 1.44.9.

## How answers are compared

Each replayed call gets one status:

- `matched`, with the strongest level that held: `exact` (byte-equal text, raw tool-call
  arguments equal), `normalized` (equal after Unicode NFKC, case folding and whitespace
  collapsing; tool calls equal by name and parsed arguments), `json` (both answers parse as
  JSON, a Markdown code fence allowed, and are equal as structures; key order ignored), or
  `semantic` (your `--scorer` returned at least `--threshold`).
- `changed`: none of the levels held. A different tool or different tool arguments is always a
  change, whatever the text says.
- `not_replayable`: the record could not be rebuilt or run; the reasons are listed.
- `error`: the replay call failed (the SDK error type, HTTP status and message).

No record is reported as matched without being compared. Rows also carry the unified diff,
token deltas, latency delta and, when a cost is known, a cost delta. Replay ships **no price
table**: costs come from a `cost_usd` field on the record, or from `--prices prices.json`
(`{"model": {"input_per_mtok": N, "output_per_mtok": N}}`).

The semantic hook is any callable `(recorded_text, replay_text) -> float`; no embedding model or
LLM judge is bundled: `--scorer mypkg.scoring:similarity`.

## Defaults and how to change them

| Setting | Default | Change with |
|---|---|---|
| Executor | `auto` (live, per record's provider) | `--executor cassette` (no network), `--cassette FILE` |
| Confirmation before live calls | asked once; refused when there is no terminal | `--yes` |
| Hard cap on live calls | 100 (the plan is refused above it) | `--max-calls N` |
| Parallel calls | 2 | `--concurrency N` |
| Retries on rate limits (429) and server errors (5xx) | 3, with the SDK's own backoff | `--max-retries N` |
| Which records | all replayable ones | `--limit N`, `--sample N --seed S` |
| What-if | none | `--model X`, `--param k=v` (JSON values), `--drop-param k`, `--served-model` |
| Exit code | 1 on any `error` | `--fail-on-change` also fails on `changed` |

The plan's token figure is an **upper bound** only when every call has a recorded max-tokens
cap: recorded prompt tokens plus each cap. Otherwise it is labelled an estimate and says how many
calls are uncapped. With `--model` the prompt token count of another model's tokenizer can
differ.

The built-in cassette (no `--cassette`) answers with each record's own recorded output. That
checks the pipeline and is deterministic, but it is **not evidence that a model still answers
the same way**; it refuses `--model`/`--param`. For a CI regression check, record a cassette once
with a live run (`--save-cassette`) and replay against it: a request your application now builds
differently has no cassette entry and is reported as an `error`.

## How to turn it off or undo it

Replay changes nothing in your application or its configuration. Delete the report and cassette
files you asked for, and `~/.briefcase/replay/` for the status summary.

## Limits and failure modes

- Only records written by `briefcase.auto` provider instrumentation (OpenAI chat, Responses and
  legacy completions; Anthropic Messages (the beta resource is handled but was not tested); Gemini `generate_content`; Bedrock
  `Converse`) can be replayed. Embeddings and Bedrock `InvokeModel` record no output to compare.
- Records captured with `capture_content=False` (the default) or `"hash"` cannot be replayed.
- Captured strings are cut at 8000 characters and lists at 200 items by default; a request that
  hit those bounds, or held images/files/audio (captured as placeholders such as `<1024 bytes>`),
  is reported as not replayable. The truncation check assumes the default `max_content_chars`.
- Streams: a stream that was not read to the end is not replayable. Streamed tool calls are not
  captured, so a streamed answer that was only a tool call has nothing to compare. Replays are
  made without streaming.
- For the Responses API, Gemini and Bedrock, capture keeps only answer text, so a recorded answer
  that was only a function call is reported as not replayable.
- `n > 1`: only the first choice is recorded and compared.
- Live executors were exercised only against each SDK's fake transport (and botocore's
  `Stubber`); no live provider call was made while building this feature.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "content capture was off when this call was recorded" | Re-capture with `briefcase.auto(capture_content=True)`; old records cannot be replayed. |
| Every record is `not_replayable` | `briefcase-ai replay explain runs.jsonl` lists the reason per record. |
| "live replay needs confirmation and there is no terminal to ask" | Add `--yes` in scripts and CI, or use `--executor cassette`. |
| "over --max-calls" | Narrow with `--limit N` / `--sample N`, or raise `--max-calls`. |
| `error` rows with "could not create an OpenAI client" (or another provider) | Set that provider's API key / credentials in the environment. |
| "recorded parameter(s) temperature are not accepted by the installed anthropic" | Your SDK no longer takes that parameter; `--drop-param temperature`. |
| "no cassette entry for this request" | The request changed since the cassette was recorded; re-record with `--save-cassette` or inspect the change. |
| Everything matched with the built-in cassette | Expected: it answers with the recorded outputs. Use a live run or a recorded `--cassette`. |
| Answers differ only in formatting | `briefcase-ai replay diff report.jsonl` shows which level held; consider `--scorer`. |
