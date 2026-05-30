# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.2.0] - 2026-05-30

### Added
- **Centralized logging** (`briefcase/_logging.py`): a `NullHandler` on the
  top-level `briefcase` logger (silent by default), `briefcase.enable_logging()`,
  `set_log_level()`, `disable_logging()`, `get_logger()`, and the
  `BRIEFCASE_LOG_LEVEL` environment variable. All modules now log via `get_logger`.
- **Stock exporters** (`briefcase.exporters`): `ConsoleExporter`,
  `JSONLFileExporter`, `MemoryExporter`, and a one-line `briefcase.observe(...)`
  helper — so `@capture` actually emits records instead of being a silent no-op.
- **LLM-friendly usage**: `llms.txt`, `llms-full.txt`, `AGENTS.md`, and copy-paste
  editor rules under `docs/llm/` (Cursor / Claude / Codex).
- **MCP server** (`briefcase.mcp`, extra `mcp`): `briefcase-mcp` exposes
  `sanitize_text`, `estimate_cost`, `analyze_drift`, and `how_to` tools plus the
  usage guide as a resource.

### Changed
- Normalized log levels: non-fatal OpenTelemetry/lakeFS fallbacks now log at
  `DEBUG` (with `exc_info`); previously-silent `except` blocks now emit debug logs.

## [3.2.1] - 2026-05-30

### Added
- **Cost rate cards** (`briefcase.cost.CostCalculator.estimate_cost`): an optional
  keyword-only `rate_card` selects a `platform × tier × modifier` pricing scheme —
  platforms `first_party` / `bedrock` / `vertex` / `azure`, tiers `standard` /
  `batch` / `cached` / `priority` / `flex`, and modifiers for long-context (>200K
  tiered pricing), data residency (`us`, 1.1x), and fast-mode (a premium base-rate
  override). Cards are forgiving strings such as `"batch"`, `"bedrock:batch"`, or
  `"first_party:fast"`; batch/flex are 0.5x, cache reads are 0.1x of input, and
  regional/residency add 10%. New keyword-only `cache_read_tokens` /
  `cache_write_5m_tokens` / `cache_write_1h_tokens` arguments bill prompt-cache
  usage, a `cache_cost` field is exposed on `CostEstimate`, and
  `get_available_rate_cards()` lists representative cards. Omitting `rate_card`
  (or passing `"standard"`) preserves the previous first-party standard pricing.
- **Latest model pricing**: added Anthropic Claude 4.x (`claude-opus-4-8` / `4-7`
  / `4-6` / `4-5` / `4-1`, `claude-sonnet-4-6` / `4-5`, `claude-haiku-4-5` / `3-5`),
  OpenAI GPT-5.x (`gpt-5.5`, `gpt-5.5-pro`, `gpt-5.4`, `gpt-5.4-mini`,
  `gpt-5.4-nano`, `gpt-5.4-pro`), and Google Gemini (`gemini-3.5-flash`,
  `gemini-3.1-pro`, `gemini-3.1-flash-lite`, `gemini-3-flash`, `gemini-2.5-pro` /
  `flash` / `flash-lite`) to the default pricing table. All previously available
  models are retained.

### Changed
- `CostCalculator.estimate_cost`, `estimate_cost_from_text`, and
  `project_monthly_cost` gained keyword-only `rate_card` (and, for `estimate_cost`,
  cache-token) parameters. The existing positional arguments and their
  `input_tokens` / `output_tokens` keyword names are unchanged, so existing calls
  behave identically.

## [3.1.0] - 2026-05-30

### Added
- **Bitemporal evidence primitives** (`briefcase.bitemporal`): `BitemporalRecord`,
  `BitemporalStore` protocol with in-memory, SQLite, and Iceberg backends,
  `AsOfView`, append-only corrections, and batch/stream ingest.
- **Versioned routing policy** (`briefcase.routing`): `PolicyRegistry`,
  `PolicyVersion`, `PolicyRule`, `AgentRouter`, and `AgentRoutingDecision`.
- **Compliance examiner bundles** (`briefcase.compliance`): `ExaminerBundle` with
  SHA-256 content-hash integrity and tamper detection.
- Top-level `briefcase.capture`, `briefcase.setup`, and `briefcase.BriefcaseConfig`
  re-exports for discoverability.
- `ExternalDataTracker(sanitizer=...)` to redact PII from external-data snapshots
  before they are persisted to durable storage.
- `scripts/check_imports.py` import-smoke test for the built wheel.

### Fixed
- **`briefcase.cost`, `briefcase.drift`, and `briefcase.sanitize` now import from a
  clean source build.** `bindings/python/src/lib.rs` was missing `add_class`
  registrations for `CostEstimate`, `BudgetStatus`, `DriftMetrics`, `Redaction`,
  `SanitizationResult`, and `SanitizationJsonResult`; `briefcase.cost` also imported
  a non-existent `BudgetAlert` type.
- `briefcase.rag` no longer fails to import on a spurious `pyarrow` requirement.
- Misleading `ImportError` messages on native-backed modules now point to
  reinstall/rebuild rather than no-op pip extras.
- `scripts/version_sync.py` missing `Iterable` import; the manifest now also tracks
  `bindings/python/Cargo.toml`.
- The flagship `examples/python-basic` and validation examples now run end-to-end.

### Security
- External-data snapshots can be redacted before persistence (see above); fails
  closed if redaction errors.
- Expanded PII detection: corrected the email regex and added GitHub/GitLab/Stripe/
  HuggingFace API-key prefixes.
- Robust telemetry opt-out: `BRIEFCASE_TELEMETRY` now accepts `0/false/no/off`.
- `source_name` is sanitized before use in storage object keys (path-traversal
  hardening).

### Changed
- Deduplicated the optional OpenTelemetry import into `briefcase._otel`.
- Extracted guardrail core data types into `briefcase.guardrails._types`.
- CI builds and tests across Python 3.9–3.13, runs the native binding tests, and
  import-smoke-tests the built wheel before publish.

## [3.0.0]

### Added
- Initial open-source release: decision tracking, deterministic replay, drift and
  cost calculation, PII sanitization, and SQLite storage, backed by a Rust core.
