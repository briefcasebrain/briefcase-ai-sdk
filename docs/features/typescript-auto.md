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

Requires Node 26 or later (`engines.node` is `>=26`; see [Node versions](#node-versions)).

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
| When SDKs are patched | each SDK copy when the application loads it ([startup cost](#startup-cost)) | `BRIEFCASE_AUTO_EAGER=1`, or `auto({ eager: true })`: load and patch every installed copy at once |
| OpenAI chat stream usage | not requested | `auto({ streamUsage: true })` adds `stream_options.include_usage` (one extra, usage-only chunk) |
| Queue | 10 000 records in memory, then new records are dropped and counted | `observe(dest, { maxQueue })`; `exportStats().dropped` |
| HTTP batching | 100 records or every 5 s; 3 retries with jittered backoff; https required except to loopback | `new HttpExporter(url, { batchSize, flushIntervalMs, maxRetries, allowInsecure })` |
| Logging | warnings on stderr, prefixed `briefcase.auto:` | `BRIEFCASE_LOG_LEVEL=debug` or `error` |

When the preload activates:

- `node --import @briefcase-ai/auto/register` on the command line: always (unless `BRIEFCASE_AUTO=0`).
- Through `NODE_OPTIONS` only: only with `BRIEFCASE_AUTO=1`. `NODE_OPTIONS` is inherited by
  every Node process below it (npm, tsc, test runners, editors); without this rule all of them
  would start exporting records.
- `BRIEFCASE_AUTO=0` always turns it off.
- Worker threads: the same rule, per thread. A worker inherits the parent's `execArgv` and
  environment, so `--import` (or `NODE_OPTIONS` with `BRIEFCASE_AUTO=1`) instruments every
  worker too (verified on Node 26.13.1). See [Worker threads](#worker-threads).

## How to turn it off or undo it

- Remove the `--import` flag, or set `BRIEFCASE_AUTO=0`.
- In code: `undo()` (or `undo("openai")`) restores the original SDK methods. If another tool
  re-patched a method after `auto()`, `undo()` leaves that patch in place, lists the method
  under `status().providers[i].refusedUndo`, and our wrapper underneath stops recording.
- `shutdown()` flushes and stops exporting; later calls pass through unrecorded.

## Limits and failure modes

- **Experimental.** Verified only against the SDK versions above, by reading the installed
  packages and running every path offline through each SDK's own transport hook (`fetch`,
  `httpOptions.fetch`, `requestHandler`) or against a local HTTP / HTTP/2 server, with fake
  JSON, SSE and AWS event-stream bodies. No live provider call was made.
- **Clients created before `auto()`** are recorded for every SDK. openai, anthropic and
  Bedrock are patched on shared prototypes. For `@google/genai`, whose `generateContent` /
  `generateContentStream` are assigned per instance in the `Models` constructor, a client
  created after the patch gets wrapped instance functions, and a client created before it is
  recorded through the internal methods those instance functions call on the shared
  `Models.prototype` (`generateContentInternal`, `generateContentWithContinuation`,
  `generateContentStreamInternal`, `generateContentStreamWithContinuation`,
  `processAfcStream` in 3.6.1). Each call is one record either way, with one exception:
  non-streamed **automatic function calling** on a client created before `auto()` gives one
  record per model round (each with that round's usage), where a client created after
  `auto()` gives one record for the whole call with the final response's usage. With the
  `--import` preload every client is created after the patch.
- **Bundled applications**: an SDK inlined into a bundle is not in `node_modules`, so nothing
  can find it unless the bundle cooperates. Use the esbuild plugin, keep the SDKs external, or
  call `instrument(client)` ([Bundled applications](#bundled-applications)). Without any of
  these, the application runs unchanged, no record is written, and one warning per provider
  says so the first time such a request is made.
- **Worker threads**: records from a worker reach the main thread's destination even when the
  worker is terminated; `withWorkflow()` context does not cross threads by itself
  ([Worker threads](#worker-threads)).
- **`auto()` called from code (not the preload)** cannot ask Node whether an SDK's ES module
  copy is already loaded (there is no such API), so it loads the ESM copy of each installed
  SDK it covers (`require(esm)` returns the loaded instance when there is one). That costs the
  load time of SDKs the application never imports; `auto({ only: [...] })` or the preload
  avoid it. CommonJS copies are found in `require.cache` and cost nothing.
- **Load hook.** Laziness uses `module.registerHooks` (stability 1.2, release candidate, in
  Node 26). For the few SDK files that hold patch targets (listed per provider in
  `src/instrument/*.ts`, `FILES`) it appends one statement after the module body that hands
  the module's exports to this package; every other module is returned exactly as Node loaded
  it. Stack traces and line numbers of the SDK files are unchanged (the statement is appended
  after the last line).
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

## Startup cost

Measured before and after the change, on the same machine, with `npm run bench:startup` (`js/auto/scripts/startup-bench.mjs`): wall-clock time
from spawn to exit of a fresh `node` process, 20 runs per row after one warm-up run, Node
26.13.1, Linux x64, all four SDKs installed (versions above). The machine is a shared,
noisy container, so read differences of under ~15 ms as noise.

| Scenario (ms) | before (eager, 4.4.0 as merged) p50 / p95 | after (lazy) p50 / p95 |
|---|---|---|
| `node trivial.mjs`, no preload | 31.7 / 43.3 | 37.8 / 56.7 |
| `--import register`, `trivial.mjs` (imports no SDK) | 387.8 / 486.5 | 74.2 / 136.7 |
| same, only openai enabled (`BRIEFCASE_AUTO_SKIP` the rest) | 133.4 / 189.4 | 72.5 / 141.6 |
| same, only anthropic | 190.5 / 211.6 | 71.1 / 104.5 |
| same, only google-genai | 131.2 / 178.5 | 74.4 / 88.1 |
| same, only bedrock | 98.9 / 122.1 | 62.4 / 86.9 |
| `uses-openai.mjs` (imports openai): no preload / preload, p50 | 146.8 / 366.2 | 165.7 / 208.0 |
| `uses-anthropic.mjs`: no preload / preload, p50 | 135.4 / 339.1 | 130.4 / 155.8 |
| `uses-google-genai.mjs`: no preload / preload, p50 | 102.8 / 339.6 | 96.8 / 122.2 |
| `uses-bedrock.mjs`: no preload / preload, p50 | 113.7 / 476.3 | 90.1 / 117.5 |
| `uses-all.mjs` (imports all four): no preload / preload, p50 | 396.5 / 497.0 | 304.0 / 333.0 |

Before, `auto()` loaded the ESM and the CommonJS copy of every installed SDK up front (about
+356 ms p50 for an application that imports none of them). Now each copy is patched when the
application loads it: an application that never imports an SDK does not load it, an ESM
application never loads the CommonJS copy, and the remaining cost of the preload is loading
this package and registering the hook (+36 ms p50 for `trivial.mjs`, +25 to +42 ms p50 for an
application that imports one SDK, +29 ms with all four).
`BRIEFCASE_AUTO_EAGER=1` / `auto({ eager: true })` restores the old behaviour.

## Bundled applications

When esbuild, webpack, Rollup or Vite SSR inline an SDK into the bundle, its classes live
inside the bundle file, where `auto()` cannot reach them. What happens, measured with esbuild
0.28.2 bundles (ESM and CommonJS output) of an openai, an anthropic, a Gemini and a Bedrock
app talking to a local server (`js/auto/test/bundle.test.ts`):

- 4.4.0 as merged: the app ran, no record was written, nothing was printed.
- Now, without any of the fixes below: the app runs, no record is written, and the first
  provider request made by the inlined copy prints one warning per provider
  (`briefcase.auto: a request through the openai SDK was made by a copy of the SDK that is not
  instrumented ...`) and is counted in `status().providers[i].unrecordedRequests`. Detection
  listens on Node's diagnostics channels (`undici:request:create`,
  `http.client.request.created`, `http2.client.stream.created`) and recognises each SDK by the
  headers it sends; it only observes, and a request made with a custom `fetch` is not seen.

Three fixes, each run end to end in `test/bundle.test.ts`:

1. **esbuild plugin** (inline the SDKs and still record them):

   ```js
   import { build } from "esbuild";
   import { briefcaseAuto } from "@briefcase-ai/auto/esbuild";

   await build({
     entryPoints: ["src/app.ts"], bundle: true, platform: "node", outfile: "dist/app.js",
     plugins: [briefcaseAuto()],
   });
   ```

   then `node --import @briefcase-ai/auto/register dist/app.js`, or bundle
   `@briefcase-ai/auto` too and call `observe(); auto();` in the application. The plugin
   appends to each SDK file that holds patch targets the same one statement the Node load hook
   appends; at run time the bundled module hands its exports to `auto()`, which patches them as
   it patches an installed copy (reports made before `auto()` runs are kept and replayed). The
   statement embeds no build-machine path. Verified for openai, anthropic, Gemini (`dist/node` build) and
   Bedrock (its `dist-cjs` build, and its `dist-es` build when `mainFields: ["module", "main"]`).
   ESM output of `@google/genai` needs esbuild's usual `require` banner
   (`import { createRequire } from "node:module"; const require = createRequire(import.meta.url);`),
   with or without this package.

2. **Keep the SDKs external** (any bundler): they stay in `node_modules`, where the preload
   patches them.

   ```js
   import { SDK_PACKAGES } from "@briefcase-ai/auto/esbuild"; // ["openai", "@anthropic-ai/sdk", "@google/genai", "@aws-sdk/client-bedrock-runtime"]
   await build({ /* ... */ external: SDK_PACKAGES });
   ```

   webpack 5 (verified once by hand with webpack 5.113.0, `target: "node"`, CommonJS output:
   inlined, it warned and recorded nothing; with this `externals`, one record; not part of the
   automated tests):

   ```js
   const SDK_PACKAGES = ["openai", "@anthropic-ai/sdk", "@google/genai", "@aws-sdk/client-bedrock-runtime"];
   module.exports = {
     target: "node",
     externals: [({ request }, callback) =>
       SDK_PACKAGES.some((p) => request === p || request.startsWith(`${p}/`)) ? callback(null, `commonjs ${request}`) : callback()],
   };
   ```

   Rollup / Vite SSR: list the same packages in `external` (Vite SSR externalises
   dependencies by default unless `ssr.noExternal` includes them). Not executed here.

3. **`instrument(client)`**: patch the copy a client belongs to, through its prototypes. Every
   client of that copy, created before or after, is covered; a copy that is already
   instrumented is left alone; it never throws (an unsupported object is reported once on
   stderr) and returns its argument.

   ```ts
   import OpenAI from "openai";
   import { observe, auto, instrument } from "@briefcase-ai/auto";
   observe(); auto();
   const client = instrument(new OpenAI());
   ```

   Accepted: an `OpenAI` or `Anthropic` client or class, a `GoogleGenAI` client (or its
   `.models`, or the `Models` class), a `BedrockRuntimeClient` / `BedrockRuntime` client or
   class. Patches made this way appear in `status()` as `client-esm ...` and are undone by
   `undo()` like any other.

## Worker threads

Each worker thread has its own module graph and its own copy of this package. Verified with
real `worker_threads` (`test/workers.test.ts`, Node 26.13.1):

- `--import @briefcase-ai/auto/register` on the command line, and `NODE_OPTIONS` with
  `BRIEFCASE_AUTO=1`, also run in every `new Worker(...)`, so SDK calls in workers are
  recorded.
- When the main thread is observing, a worker's records are posted to the main thread the
  moment each call finishes (a `BroadcastChannel`, named to workers through
  `worker_threads.setEnvironmentData`, which child processes do not inherit) and exported by
  the main thread's destination: one queue, one HTTP connection. A record posted before
  `worker.terminate()` still arrives (40 of 40 runs, and 48 of 48 runs of 12 processes in
  parallel, each with three workers that end normally and one that is terminated). A call
  still in flight when the worker is terminated has no record.
- A worker that instruments itself while the main thread does not observe
  (`observe(); auto();` inside the worker) exports on its own. Console and `.jsonl`
  destinations write each record synchronously as it finishes, so they survive
  `terminate()`. An HTTP or OTLP destination in such a worker queues records and sends them
  when the worker's event loop empties (`beforeExit` runs in workers); `terminate()` runs no
  hook in the worker, so queued records are lost.
- The main thread's `shutdown()` does not wait for workers (measured well under 500 ms with a
  worker still running). Records a worker posts after it are dropped and counted
  (`exportStats().dropped`); workers created after it export on their own.
- `withWorkflow()` context (AsyncLocalStorage) does not cross threads. Pass the id:

  ```ts
  // main thread
  await withWorkflow("claim-42", async () => {
    new Worker(new URL("./worker.js", import.meta.url), { workerData: { workflowId: currentWorkflow()!.workflowId } });
  });
  // worker.js
  await withWorkflow("claim-42", () => doCalls(), { workflowId: workerData.workflowId });
  ```

## Node versions

`engines.node` is `>=26`. Node 20 reached end of life on 2026-04-30; 26 is the current
Active LTS line. The APIs this package relies on, with the version each shipped in (Node
docs, "Added in" / history, read 2026-11-22): `module.registerHooks` (22.15.0, release
candidate since 24.13.1 / 25.4.0), `require(esm)` without a flag (22.12.0),
`worker_threads.setEnvironmentData` (15.12.0), `BroadcastChannel` and its `unref()` (15.4.0),
`diagnostics_channel.subscribe` (18.7.0), the `http.client.request.created` and
`http2.client.stream.created` channels (documented in Node 26), `import.meta.resolve`
returning a string without a flag (20.6.0). `module.register` is not used (runtime-deprecated
in Node 26, DEP0205). The full test suite, including the bundled-app, worker and activation
tests, passes on Node 26.0.0 and 26.13.1 (official linux-x64 tarballs, checked against
`SHASUMS256.txt`). Older Node versions are not tested.

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| No records at all | Is the preload active? `NODE_OPTIONS` alone needs `BRIEFCASE_AUTO=1`. Then run `node -e "import('@briefcase-ai/auto').then(m=>{m.observe();console.log(JSON.stringify(m.auto(),null,2))})"` and look at each provider's `state`. |
| `briefcase: auto-instrumentation is disabled for this process (...)` | The destination could not be set up; the message says why (for example an `http://` URL to a non-loopback host: use `https://`). The app keeps running uninstrumented. |
| `briefcase.auto: <sdk> (esm): the installed SDK does not have the layout ...` | That SDK version is newer or older than the verified one and was skipped whole; `status()` lists the missing targets. Pin the verified version or upgrade `@briefcase-ai/auto`. |
| `a request through the <sdk> SDK was made by a copy of the SDK that is not instrumented` | The SDK is bundled into the application. Use the esbuild plugin, `external: SDK_PACKAGES`, or `instrument(client)` ([Bundled applications](#bundled-applications)). |
| `status()` shows a provider as `waiting` | Normal: the SDK is installed and is patched when the application loads it. `BRIEFCASE_AUTO_EAGER=1` patches at startup. |
| Gemini with automatic function calling gives one record per round | The client was created before `auto()` (see [Limits](#limits-and-failure-modes)). Create it after, or use the `--import` preload. |
| Records from worker threads missing | The worker was created before the main thread's `observe()`, uses an HTTP destination of its own and was terminated, or the call was still in flight at `terminate()` ([Worker threads](#worker-threads)). |
| Records missing at the end of a short script with an HTTP / OTLP destination | The script calls `process.exit()`. `await shutdown()` first. |
| `briefcase.auto: OTEL_EXPORTER_OTLP_PROTOCOL=grpc is not supported` | Point `OTEL_EXPORTER_OTLP_ENDPOINT` at the collector's HTTP port (4318) and set `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`. |
| `token_usage` is `null` on OpenAI chat streams | The stream did not ask for usage. Pass `stream_options: { include_usage: true }`, or `auto({ streamUsage: true })`. |
| `export queue full` | The destination is slower than the app. Check `exportStats()`; raise `maxQueue` or fix the destination. |
| Prompt text appears in records | Content capture was turned on (`BRIEFCASE_CAPTURE_CONTENT` or `captureContent`). Unset it. |
