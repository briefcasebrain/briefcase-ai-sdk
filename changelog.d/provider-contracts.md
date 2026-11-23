### Added

- Provider API contract tests (`tests/contracts`, `docs/features/provider-contracts.md`): the
  provider instrumentation, the replay executors, the budget token caps and the datasets/eval
  flow are checked, offline and with no model call, against OpenAI's published OpenAPI spec,
  the anthropic 2.12.0 request/response types (Anthropic publishes no API spec), the Gemini
  API discovery document (downloaded, not vendored) and botocore's Bedrock Runtime service
  model. Specs are pinned with source URL, retrieval date, SHA-256 and licence in
  `tests/contracts/specs/manifest.json`; `scripts/refresh_provider_specs.py` and a weekly
  workflow report upstream drift. CI runs the suite; `jsonschema` joins the `dev` extra.

### Changed

- Provider records gain usage counters the specs define and capture dropped:
  `token_usage.cache_write_tokens` (OpenAI chat and Responses; a part of `prompt_tokens`,
  priced by budgets at the cache-write rate) and `token_usage.reasoning_tokens` for Anthropic
  (`output_tokens_details.thinking_tokens`, a part of `completion_tokens`). OpenAI chat records
  `verbosity`, google-genai records `frequency_penalty` and `presence_penalty`.
- `briefcase replay explain` names more request settings that capture does not record and that
  change the answer (OpenAI `web_search_options`, `modalities`, `audio`, `functions`,
  `function_call`, `moderation`; Responses `prompt`, `max_tool_calls`, `parallel_tool_calls`,
  `context_management`, `moderation`, `image`; completions `echo`; Anthropic `container`;
  Gemini `response_json_schema`, `response_modalities`, `cached_content`,
  `automatic_function_calling`, `routing_config`, `model_selection_config`; Bedrock
  `outputConfig`, `promptVariables`), so a replay that sends the API default instead is flagged.
