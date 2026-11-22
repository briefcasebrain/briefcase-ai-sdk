### Added

- `@briefcase-ai/auto` (new npm package, `js/auto`): zero-code decision capture for Node.
  `node --import @briefcase-ai/auto/register app.js` (or `observe()` + `auto()` in code)
  records one decision per `openai`, `@anthropic-ai/sdk`, `@google/genai` and
  `@aws-sdk/client-bedrock-runtime` call, with the same field names and `decision_type`
  values as Python's `briefcase.auto()`. Destinations and environment variables match
  `briefcase.observe()`: console, `*.jsonl`, `http(s)://` batches (`{"records": [...]}`),
  and OTLP/HTTP JSON. Content capture is off by default. `withWorkflow()` and an active
  OpenTelemetry span are carried into records as `workflow_id` / `trace_id`.
