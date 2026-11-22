# Zero-code capture for Node (TypeScript)

`@briefcase-ai/auto` records one decision record for every call a Node application makes
through the OpenAI, Anthropic, Google GenAI (Gemini) and Amazon Bedrock SDKs, without editing
the application. The records have the same fields and `decision_type` values as the ones
Python's `briefcase.auto()` writes, so both languages land in the same place looking the same.

Flow step: **Capture**. Status: **experimental** (see [Limits](#limits-and-failure-modes)).

## The one command

```bash
npm install @briefcase-ai/auto
node --import @briefcase-ai/auto/register app.js
```

From the Python CLI, `briefcase exec` sets `BRIEFCASE_AUTO=1`; add the preload yourself:

```bash
NODE_OPTIONS=--import=@briefcase-ai/auto/register briefcase exec -- node app.js
```

Or in code (`await` is not needed; `auto()` is synchronous):

```ts
import { observe, auto, shutdown } from "@briefcase-ai/auto";
observe();   // destination from the environment, else JSON lines on stderr
auto();      // every installed provider SDK
// ...
await shutdown(); // optional: flush before a short script ends
```

Real output, from `node --import @briefcase-ai/auto/register demo.mjs` in a temporary `HOME`,
where `demo.mjs` makes one `chat.completions.create` call through a fake `fetch`:

```text
Covered.
{"schema_version":"1.2","decision_id":"46e94fe6-d141-4568-a904-ea2681f38f73","decision_type":"llm.openai.chat","function_name":"openai.chat.completions.create","inputs":{},"outputs":{},"started_at":"2026-11-22T04:43:56.693949+00:00","ended_at":"2026-11-22T04:43:56.702331+00:00","execution_time_ms":8.382153000000017,"status":"ok","model_parameters":{"provider":"openai","model_name":"gpt-4o-mini-2024-07-18","requested_model":"gpt-4o-mini"},"token_usage":{"prompt_tokens":14,"completion_tokens":2,"total_tokens":16},"finish_reason":"stop","response_id":"chatcmpl-demo","request_id":"req_demo","stream":false}
```

## What it writes or sends, and where

One record per provider call:

| SDK (verified version) | Calls recorded | `decision_type` |
|---|---|---|
| `openai` 7.62.0 | `chat.completions.create` (also `.parse()`, `.stream()`, `runTools`), `responses.create` (also `.parse()`, `.stream()`), `completions.create`, `embeddings.create` | `llm.openai.chat`, `llm.openai.responses`, `llm.openai.completions`, `llm.openai.embeddings` |
| `@anthropic-ai/sdk` 1.12.0 | `messages.create` (also `.parse()`, `.stream()`), `beta.messages.create` | `llm.anthropic.messages` |
| `@google/genai` 3.6.1 | `models.generateContent`, `models.generateContentStream` (and `chats`) | `llm.google_genai.generate_content` |
| `@aws-sdk/client-bedrock-runtime` 3.1175.0 | `Converse`, `ConverseStream`, `InvokeModel`, `InvokeModelWithResponseStream` | `llm.bedrock.converse`, `llm.bedrock.invoke_model` |

Fields: `schema_version` (`"1.2"`), `decision_id`, `decision_type`, `function_name`, `inputs`,
`outputs`, `started_at`, `ended_at`, `execution_time_ms`, `status` (`ok` / `error` /
`cancelled`), `model_parameters` (`provider`, `model_name`, `requested_model` when the response
names a different model, scalar request parameters), `token_usage` (`prompt_tokens`,
`completion_tokens`, `total_tokens` plus provider counters; `null` when the provider reported
none), `finish_reason`, `response_id`, `request_id`, `stream`, and for streams
`time_to_first_chunk_ms` and `outputs.chunk_count` / `outputs.stream_completed`. On failure:
`error`, `error_type`, `http_status`. With `withWorkflow()` or an active OpenTelemetry span:
`workflow_id`, `trace_id`, `parent_span_id`, `trace_flags`.

Never recorded: API keys, request or response headers, and (unless you turn it on) prompt or
completion content.

Destination, resolved in the same order and from the same variables as Python's
`briefcase.observe()`:

1. `BRIEFCASE_EXPORTER`: `console` (JSON lines on stderr), a path ending in `.jsonl` (file
   mode 0600), an `https://` URL (POSTs `{"records": [...]}` batches, the body Python's
   `HTTPExporter` sends, with `BRIEFCASE_EXPORTER_TOKEN` as a bearer token), or `otlp`.
2. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_ENDPOINT` (+
   `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME`): OTLP/HTTP with the JSON encoding, one
   `briefcase.decision` span per record with the same `briefcase.*` attributes as Python's
   `OTelExporter`.
3. Otherwise the console.

## Defaults and how to change them

| Setting | Default | Change it with |
|---|---|---|
| Content capture | off | `BRIEFCASE_CAPTURE_CONTENT=full` (or `hash`), or `auto({ captureContent: true })`; each string is cut at 8000 characters (`maxContentChars`) |
| Providers | every installed one | `BRIEFCASE_AUTO_SKIP=bedrock,google-genai`, or `auto({ skip: [...] })` / `auto({ only: [...] })` |
| OpenAI chat stream usage | not requested | `auto({ streamUsage: true })` adds `stream_options.include_usage` (one extra, usage-only chunk) |
| Queue | 10 000 records in memory, then new records are dropped and counted | `observe(dest, { maxQueue })`; `exportStats().dropped` |
| HTTP batching | 100 records or every 5 s; 3 retries with jittered backoff; https required except to loopback | `new HttpExporter(url, { batchSize, flushIntervalMs, maxRetries, allowInsecure })` |
| Logging | warnings on stderr, prefixed `briefcase.auto:` | `BRIEFCASE_LOG_LEVEL=debug` or `error` |

When the preload activates:

- `node --import @briefcase-ai/auto/register` on the command line: always (unless `BRIEFCASE_AUTO=0`).
- Through `NODE_OPTIONS` only: only with `BRIEFCASE_AUTO=1`. `NODE_OPTIONS` is inherited by
  every Node process below it (npm, tsc, test runners, editors); without this rule all of them
  would start exporting records.
- `BRIEFCASE_AUTO=0` always turns it off. Worker threads are not instrumented.

## How to turn it off or undo it

- Remove the `--import` flag, or set `BRIEFCASE_AUTO=0`.
- In code: `undo()` (or `undo("openai")`) restores the original SDK methods. If another tool
  re-patched a method after `auto()`, `undo()` leaves that patch in place, lists the method
  under `status().providers[i].refusedUndo`, and our wrapper underneath stops recording.
- `shutdown()` flushes and stops exporting; later calls pass through unrecorded.

## Limits and failure modes

- **Experimental.** Verified only against the SDK versions above, by reading the installed
  packages and running every path offline through each SDK's own transport hook (`fetch`,
  `httpOptions.fetch`, `requestHandler`) with fake JSON, SSE and AWS event-stream bodies.
  No live provider call was made. Tested on Node 22.23.5; Node 20.19 is the declared floor
  but was not run.
- **Startup cost.** Dual-format SDKs (openai, anthropic, genai) ship separate ESM and CommonJS
  copies that Node loads as separate classes, so `auto()` loads and patches both copies of
  every installed SDK: about 260 ms with all four installed (measured on the machine above).
  `BRIEFCASE_AUTO_SKIP` / `only` reduce it.
- **Google GenAI: create clients after `auto()`.** In 3.6.1 `generateContent` is assigned per
  instance in the `Models` constructor, so only clients constructed after `auto()` are
  recorded. The `--import` preload always runs first.
- **Bundled apps** (esbuild, webpack, ncc) that inline the SDK are not instrumented: the
  patches target the SDK files in `node_modules`, resolved from this package's location. The
  SDK must be installed next to `@briefcase-ai/auto` (it is a peer dependency).
- **OTLP: HTTP with JSON only.** `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` falls back to the console
  with a warning; `http/protobuf` is sent as JSON to the same URL (collectors accept both on
  `/v1/traces`). Plain `http://` OTLP endpoints are allowed, as in the OTel SDKs.
- **Streams.** A stream read to the end is one record. A stream the code stops reading
  (`break`) is one record with `outputs.stream_completed: false`; an aborted one
  (`controller.abort()`, `abortSignal`) is one record with `status: "cancelled"`. A stream
  never read at all is recorded when it is garbage-collected or when the process exits.
- **`.asResponse()` only**: the body belongs to the caller and is never read; the call is
  recorded (request id, no usage) when garbage-collected or at exit.
- **Return values.** openai and anthropic calls return the SDK's own `APIPromise` object;
  streams are the SDK's own objects, instrumented in place. `@google/genai` and the AWS SDK
  return plain promises: the wrapper returns an equivalent promise resolving to the SDK's own
  result object.
- **Exit.** Records still queued are sent when the event loop empties (`beforeExit`) and on
  SIGTERM / SIGINT (up to 2 s, then the default signal action runs). After `process.exit()`
  only the console and `.jsonl` destinations can still write; an HTTP / OTLP destination
  prints how many records were not sent. Call `await shutdown()` before `process.exit()`.
- **Token usage differences from Python:** an unreported counter is `null` (Python writes 0
  for a missing half and omits `token_usage` when nothing was reported).
- Not covered: OpenAI Realtime / Assistants / audio / images, Anthropic batches, GenAI Live and
  `embedContent`, Bedrock agents and `InvokeModelWithBidirectionalStream`, the Vercel AI SDK
  and LangChain.js.

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| No records at all | Is the preload active? `NODE_OPTIONS` alone needs `BRIEFCASE_AUTO=1`. Then run `node -e "import('@briefcase-ai/auto').then(m=>{m.observe();console.log(JSON.stringify(m.auto(),null,2))})"` and look at each provider's `state`. |
| `briefcase: auto-instrumentation is disabled for this process (...)` | The destination could not be set up; the message says why (for example an `http://` URL to a non-loopback host: use `https://`). The app keeps running uninstrumented. |
| `briefcase.auto: <sdk> (esm): the installed SDK does not have the layout ...` | That SDK version is newer or older than the verified one and was skipped whole; `status()` lists the missing targets. Pin the verified version or upgrade `@briefcase-ai/auto`. |
| Gemini calls missing, others present | The `GoogleGenAI` client was created before `auto()`. Create it after, or use the `--import` preload. |
| Records missing at the end of a short script with an HTTP / OTLP destination | The script calls `process.exit()`. `await shutdown()` first. |
| `briefcase.auto: OTEL_EXPORTER_OTLP_PROTOCOL=grpc is not supported` | Point `OTEL_EXPORTER_OTLP_ENDPOINT` at the collector's HTTP port (4318) and set `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`. |
| `token_usage` is `null` on OpenAI chat streams | The stream did not ask for usage. Pass `stream_options: { include_usage: true }`, or `auto({ streamUsage: true })`. |
| `export queue full` | The destination is slower than the app. Check `exportStats()`; raise `maxQueue` or fix the destination. |
| Prompt text appears in records | Content capture was turned on (`BRIEFCASE_CAPTURE_CONTENT` or `captureContent`). Unset it. |
