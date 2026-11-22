# @briefcase-ai/auto

Zero-code decision capture for Node. One decision record per call made through
`openai`, `@anthropic-ai/sdk`, `@google/genai` and `@aws-sdk/client-bedrock-runtime`, with the
same field names and `decision_type` values as Python's `briefcase.auto()`.

```bash
npm install @briefcase-ai/auto
node --import @briefcase-ai/auto/register app.js        # records on stderr as JSON lines
BRIEFCASE_EXPORTER=runs.jsonl node --import @briefcase-ai/auto/register app.js
```

With the Python CLI (`briefcase exec` sets `BRIEFCASE_AUTO=1`):

```bash
NODE_OPTIONS=--import=@briefcase-ai/auto/register briefcase exec -- node app.js
```

In code:

```ts
import { observe, auto, withWorkflow, shutdown } from "@briefcase-ai/auto";

observe();                 // or observe("runs.jsonl"), observe("https://collector.example.com/v1/decisions")
auto();                    // { captureContent?, maxContentChars?, skip?, only?, streamUsage?, contextVersion?, redact?, eager? }
instrument(client);        // only for an SDK copy auto() cannot reach (bundled into the app); returns client

await withWorkflow("claim-123", async () => {
  await openai.chat.completions.create({ ... });   // records carry the same workflow_id
});

await shutdown();          // flush before a short script exits
```

Node >= 26, ESM with type declarations, no runtime dependencies. The provider SDKs and
`@opentelemetry/api` are optional peer dependencies.

## Bundled apps and worker threads

- esbuild: `plugins: [briefcaseAuto()]` from `@briefcase-ai/auto/esbuild` keeps inlined SDKs
  recordable; or `external: SDK_PACKAGES` (same module) to leave them in `node_modules`; or
  `instrument(client)`. An inlined SDK that none of these covers is reported once on stderr.
- Worker threads are instrumented by the same preload; their records go to the main thread's
  destination, also when a worker is terminated. Pass a workflow id to a worker through
  `workerData` and `withWorkflow(name, fn, { workflowId })`.

## Destinations

Same order and variables as Python's `briefcase.observe()`:

1. `BRIEFCASE_EXPORTER`: `console`, `memory`, `*.jsonl`, `https://...` (POSTs
   `{"records": [...]}`, bearer token from `BRIEFCASE_EXPORTER_TOKEN`; plain `http://` only
   to loopback), or `otlp`.
2. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_ENDPOINT`: OTLP/HTTP JSON, one
   `briefcase.decision` span per record (attributes as Python's `OTelExporter`).
3. The console (stderr).

Records go through a bounded in-memory queue (`exportStats()` reports dropped and failed
counts). Timers never keep the process alive; queued records are sent when the event loop
empties and on SIGTERM / SIGINT.

## Activation rule for the preload

- `--import @briefcase-ai/auto/register` on the command line: on.
- Through `NODE_OPTIONS` only: on only with `BRIEFCASE_AUTO=1` (so npm, tsc and test runners
  that inherit `NODE_OPTIONS` stay quiet).
- `BRIEFCASE_AUTO=0`: off. `BRIEFCASE_CAPTURE_CONTENT=none|hash|full` (default none),
  `BRIEFCASE_AUTO_SKIP=openai,anthropic,google-genai,bedrock`, `BRIEFCASE_AUTO_EAGER=1`
  (patch every installed SDK copy at startup instead of when the app loads it).

## Patch techniques (verified against the installed packages)

| SDK | Version | Technique | ESM | CJS |
|---|---|---|---|---|
| openai | 7.62.0 | `create` on the resource class prototypes; the returned `APIPromise` is kept and only its `responsePromise` / `parseResponse` fields are wrapped; streams keep their object, with the `iterator` field wrapped | yes (both copies patched) | yes |
| @anthropic-ai/sdk | 1.12.0 | same as openai (`Messages.prototype.create`, regular and beta); `messages.stream()` and `.parse()` go through `create` | yes | yes |
| @google/genai | 3.6.1 | `generateContent` / `generateContentStream` are per-instance properties: an accessor on `Models.prototype` wraps them as the constructor assigns them; clients created before that are recorded through the internal methods they call on `Models.prototype` | yes | yes |
| @aws-sdk/client-bedrock-runtime | 3.1175.0 | an own `send` on `BedrockRuntimeClient.prototype` adds an `initialize` middleware to each client's middleware stack | one CJS copy serves both | yes |

Dual packages load their ESM and CommonJS builds as separate classes. Each copy is patched
when the application loads it: a synchronous load hook (`module.registerHooks`) appends one
statement to the few SDK files that hold patch targets, which hands the module's exports to
`auto()` after the module body has run. An app that never imports an SDK never loads it, and
an ESM app never loads the CommonJS copy (`--import` startup cost went from about +356 ms to
about +36 ms p50 with all four SDKs installed; see the docs). Named ESM imports themselves are
never reassigned: only prototypes and instances are touched. An SDK whose layout differs is
skipped whole with one warning; `status()` says why.

See `docs/features/typescript-auto.md` in the repository for limits and troubleshooting.

## Development

```bash
npm install
npm run typecheck && npm test && npm run build
```

Tests are offline: each SDK gets canned JSON / SSE / AWS event-stream bodies through its own
transport hook, or a local HTTP / HTTP/2 server (bundled-app tests build real esbuild bundles;
worker tests run real `worker_threads`). `npm run bench:startup` measures the preload's
startup cost. The cross-language tests also run the repository's Python `briefcase`
package (OTelExporter, the OTLP receiver parser, HTTPExporter) when `python3` can import it,
and are skipped otherwise.
