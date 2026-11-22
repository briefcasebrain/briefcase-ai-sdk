### Added

- `@briefcase-ai/auto` (new npm package, `js/auto`): zero-code decision capture for Node.
  `node --import @briefcase-ai/auto/register app.js` (or `observe()` + `auto()` in code)
  records one decision per `openai`, `@anthropic-ai/sdk`, `@google/genai` and
  `@aws-sdk/client-bedrock-runtime` call, with the same field names and `decision_type`
  values as Python's `briefcase.auto()`. Destinations and environment variables match
  `briefcase.observe()`: console, `*.jsonl`, `http(s)://` batches (`{"records": [...]}`),
  and OTLP/HTTP JSON. Content capture is off by default. `withWorkflow()` and an active
  OpenTelemetry span are carried into records as `workflow_id` / `trace_id`.

### Changed

- `@briefcase-ai/auto` now requires Node 26 or later (`engines.node` `>=26`; Node 20 is end
  of life). Tested on Node 26.0.0 and 26.13.1.
- SDKs are patched lazily, when the application loads each copy (a `module.registerHooks`
  load hook), instead of loading the ESM and CommonJS copy of every installed SDK up front.
  `--import @briefcase-ai/auto/register` startup cost with all four SDKs installed dropped
  from about +356 ms to about +36 ms p50 (Node 26.13.1). `auto({ eager: true })` /
  `BRIEFCASE_AUTO_EAGER=1` keep the old behaviour. `status()` reports a provider whose SDK
  is installed but not loaded yet as `waiting`.
- `@google/genai` clients created before `auto()` are now recorded (through the internal
  methods on the shared `Models.prototype`); one record per call, except non-streamed
  automatic function calling on such a client, which gives one record per model round.
- Worker threads are instrumented by the preload too; their records are forwarded to the main
  thread's destination as each call finishes, so they survive `worker.terminate()`.

### Added

- `instrument(client)` (also accepts the client classes): instruments the SDK copy a client
  belongs to, for SDKs bundled into the application.
- `@briefcase-ai/auto/esbuild`: `briefcaseAuto()` esbuild plugin that keeps bundled SDKs
  recordable, and `SDK_PACKAGES` for `external`.
- A warning (once per provider) and `status().providers[i].unrecordedRequests` when a
  provider request is made by an SDK copy that is not instrumented (e.g. bundled).
