# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.0.0] - 2026-08-12

### Security
- `BriefcaseClient` rejects plain-http server URLs to non-loopback hosts at
  construction, since the API key travels in the request body. Loopback hosts
  stay allowed; `ClientConfig.allow_insecure_http` (Rust) and the
  `allow_insecure_http` constructor argument (Python) opt back in. The same
  rule applies to every redirect hop, so a 307/308 downgrade to remote http
  is refused instead of re-sending the key-bearing body in cleartext.
- CLI state store: `~/.briefcase` and its JSON registries are created 0700/0600
  and tightened on open (symlinked entries are skipped, so a planted link
  cannot chmod a file outside the store). `briefcase secret set KEY` reads the
  value from stdin or an interactive prompt instead of argv; `--secret KEY`
  resolves the value from the caller's environment and warns on stderr when
  the variable is absent.
- The CLI gRPC engine dials `https://` endpoints over TLS
  (`ssl_channel_credentials`) instead of plaintext, and the per-process
  reflection cache is keyed by transport kind as well as authority, so an
  https transport never reuses call objects bound to a plaintext channel.
- SQLite store files are created 0600 before SQLite opens them; tightening of
  pre-existing files is best-effort so shared files owned by another user stay
  usable. Record IDs are validated (empty strings, path separators, and `..`
  are rejected).
- PII sanitizer: card-shaped numbers of 13 to 19 digits (with `-`/space
  separators) are redaction candidates. A 16-digit run is redacted on shape
  alone; other lengths must pass the Luhn checksum *and* start with an issuer
  digit (3 to 6), since Luhn alone accepts roughly one run in ten and would
  redact epoch-millisecond timestamps and snowflake identifiers. Runs
  containing non-ASCII decimal digits (fullwidth, Arabic-Indic) defeat the
  checksum, so they are redacted outright.
- Card, phone, and SSN matches are rejected when they continue into more
  digits, directly or across one `-`. All three patterns can match a prefix of
  a longer identifier (the card and phone patterns span `-`-grouped digits, the
  bare SSN branch is `\d{9}`), which previously turned a UUID into
  `[REDACTED_CREDIT_CARD]-[REDACTED_CREDIT_CARD]`. Redacting part of an
  identifier is worse than redacting none of it. Whitespace is not a
  continuation: it separates two distinct values far more often than it groups
  one, so `4111111111111111 5500000000000004` is two cards and both redact.

### Changed
- Breaking: `VersionedClient` requires an endpoint and credentials (parameters
  or `LAKEFS_ENDPOINT` / `LAKEFS_ACCESS_KEY` / `LAKEFS_PRIVATE_KEY`) and raises
  on live failures instead of returning fabricated data. `mock=True` opts into
  an offline stub whose metadata carries `"mock": True`; `versioned_context`
  and `@versioned` forward `mock`. `require_live=True` rejects `mock=True`.
- Breaking: `ClientConfig` gained the public field `allow_insecure_http` and
  is now `#[non_exhaustive]`; construct via `ClientConfig::default()` and set
  fields. Future field additions are then non-breaking.
- Breaking: `DriftMetrics` gained `total_samples`, the number of outputs the
  metrics were computed over (exposed as a getter and in `to_dict()`), and is
  now `#[non_exhaustive]`.
- Breaking: `PromptValidationEngine` raises `ValueError` for modes other than
  `strict` / `tolerant` / `warn_only`.
- Breaking: Python bindings raise typed exceptions (`PermissionError`,
  `ConnectionError`, `ValueError`, `KeyError`, `OSError`) instead of blanket
  `RuntimeError`, and `python_to_json_value` raises `TypeError` for values with
  no JSON equivalent (a set, an arbitrary object, a non-string dict key,
  NaN/Infinity) rather than storing `null`. Values with one obvious JSON form
  still convert: see the `datetime`/`UUID` entry under Fixed. Batch operations
  (`replay_batch`) raise the first failure's typed exception with a message
  listing every failed item, instead of a blanket `RuntimeError`, and carry the
  already-computed results on the exception.
- `SqliteBitemporalBackend` normalizes timestamp strings persisted by earlier
  versions (naive or non-UTC offsets) to UTC on open, so as-of and history
  TEXT comparisons hold across old and new rows. A read-only store cannot be
  rewritten, so it normalizes per query instead: see Fixed.
- Native calls release the GIL while blocking on the Tokio runtime.

### Added
- `AgentRouter.route(decided_at=...)` pins the decision timestamp for
  deterministic replay; examiner as-of bundles reconstruct the policy version
  active at that time.
- `track_db_query` / `track_file_fetch` accept `valid_time` and
  `source_trust_level`.
- `briefcase.integrations.gym` (extra `gym`, needs `gymnasium>=0.29`):
  `GuardrailGymEnv` exposes any `GuardrailEnv` plus a `GuardrailTask` suite as a
  single-step `gymnasium.Env`. Action 0 submits the clean request, action i
  applies `injections[i - 1]`; reward is 1.0 when the effect matches the task's
  `expected_effect`, inverted under `reward_mode="adversarial"`. Passes
  `gymnasium.utils.env_checker.check_env`. `register_with_gymnasium()` opts into
  `gymnasium.make()`; nothing is registered at import time. It holds the
  guardrail in the entry point's closure rather than in the registry kwargs that
  `gymnasium.make` deep-copies, so the made env drives the guardrail you
  registered and one holding an uncopyable resource can be registered at all.
- `EpisodeCaptureWrapper` / `capture_episodes()` record any `gymnasium.Env`
  rollout as `"rl.step"` and `"rl.episode"` decision records. A reset
  mid-episode or `close()` finalizes the open episode with `completed=False`.
- `briefcase.integrations.evals` (extra `evals`, stdlib only): `EvalRun` logs
  evaluation cases as `"eval.case"` records and one `"eval.run"` summary with
  pass rate, per-score statistics, token totals, and optional cost and drift.
- `from_inspect_log()` parses inspect-ai `.json` logs and `.eval` archives,
  `from_lm_eval_results()` parses lm-eval-harness results plus its samples
  jsonl, and `replay()` emits either as decision records. Neither framework is
  imported. Verified against artifacts inspect-ai 0.3.257 and lm-eval-harness
  0.4.12 wrote, checked in under `tests/fixtures/`.
- The `evals` extra installs `zstandard` on Python < 3.14: inspect-ai `.eval`
  archives store zstd-compressed zip entries (method 93), which stdlib
  `zipfile` decodes only on 3.14+. The backend loads lazily, so a bare install
  still imports and still reads `.json` logs and lm-eval results; without one,
  `.eval` parsing raises with install instructions instead of a
  `NotImplementedError` from inside `zipfile`.
- Examples `examples/rl_gym/` and `examples/eval_runs/`, both offline.

### Fixed
- `capture(fn, ...)` direct-call form forwards all keyword arguments instead of
  applying defaults.
- SQLite `query()` with a content filter stops scanning once it holds
  `offset + limit` matches. It previously dropped LIMIT/OFFSET from the SQL and
  deserialized every row in the time range before truncating.
- Synchronous export joins its helper thread for at most
  `SYNC_EXPORT_TIMEOUT_SECONDS` (5s) when the caller already has a running
  event loop, rather than freezing the loop behind a slow or hung exporter.
  Past the timeout it stops waiting and warns; the export continues on the
  daemon thread and is lost only if the process exits first.
- `PromptValidationEngine` accepts both lakeFS client contracts:
  `get_commit()` and the older `get_commit(repository, branch)`. The signature
  mismatch previously landed in a swallowing `except` and pinned the report to
  `"unknown"`. The two validation examples now use the current contract.
- `SqliteBitemporalBackend` opens read-only stores and answers them correctly.
  The legacy-timestamp rewrite and the WAL pragma degrade with a warning
  instead of raising, and when the rewrite cannot run, `as_of`/`history`
  normalize timestamps to UTC in SQL rather than comparing raw text. Comparing
  the stored text put `06:00+00:00` before `10:00+05:30`, which is 90 minutes
  earlier in real time, so an archived store silently omitted records from
  as-of answers. The rewrite stays the fast path for writable stores, since the
  per-query normalization cannot use the timestamp index.
- `python_to_json_value` raises `TypeError` for NaN and +/-Infinity instead of
  writing JSON `null`, which was indistinguishable from a field that was never
  computed.
- `python_to_json_value` converts values with exactly one obvious JSON form
  rather than rejecting them: `datetime`/`date`/`time` (and anything else
  exposing `isoformat()`) become ISO strings, `UUID` becomes its canonical
  string. `Input`, `Output`, `with_parameter`, and `Sanitizer.sanitize_json`
  accept ordinary payloads again. Values whose JSON form would be a guess (a
  set has no defined order, a non-str dict key no spelling) still raise.
- `ReplayEngine.replay_batch` attaches `results`, `failed_indices`,
  `succeeded`, and `total` to the exception raised on a partial failure, so one
  pruned snapshot id no longer discards every result the runtime computed. Its
  `mode` and `max_concurrent` arguments are now genuinely optional.
- The gRPC reflection cache no longer keys custom `channel_factory` transports
  by `id(factory)`, which CPython reuses once the factory is collected; such
  transports now resolve per instance and never read or write the shared cache.
- The Rust example (`examples/rust`) builds and runs again. It depended on
  `../../crates/core`, a path that does not exist, and clashed with the
  workspace it sat inside, so `cargo build` failed before compiling a line. It
  is now a standalone crate with the right path and features, CI builds and
  runs it, and its unreferenced byte-identical copy of `src/main.rs` is gone.
- The Rust code in `bindings/python` is testable: the workspace no longer
  enables `pyo3/extension-module` by default (maturin supplies it for the wheel
  via `[tool.maturin] features`), and the crate builds an `rlib` beside the
  `cdylib`. Enabling it everywhere left Python symbols to the host interpreter,
  so no test binary could ever link and `cargo test -p briefcase-python` failed
  outright. Three latent test bugs surfaced immediately and are fixed: three
  unit tests each re-initialized the process-global runtime and panicked under
  parallel execution, and a Python example in a rustdoc comment was compiled as
  Rust. `cargo test --workspace` now passes; CI runs the crate's tests and
  clippy. The wheel is unchanged: still abi3, still no libpython linkage.
- `enable_logging(stream=...)` no longer assumes the handler it finds by name
  is a `StreamHandler`. A caller's own handler registered under that name made
  it raise `AttributeError: setStream`.
- The workspace declares `rust-version = "1.85"`, and CI builds on exactly that
  toolchain. The Luhn check used `u32::is_multiple_of`, stable only from 1.87,
  which put the crate two releases above what its dependencies require; it now
  uses `%`. README links are absolute, so they resolve in the PyPI description
  as well as on GitHub.
- The PyPI package carries a description, project links, and a classifier set
  that matches what ships. `readme` and `[project.urls]` were absent, so the
  live 3.3.0 page renders blank today with only a Homepage link. Classifiers
  now declare Python 3.9 through 3.13, the three wheel platforms, topics, and
  `Typing :: Typed`, which drive PyPI's search filters. `twine check` gates
  CI. `black` is dropped from the `dev` extra, since the codebase is not
  black-formatted.
- CI builds a wheel from the sdist and imports it. `publish.yml` uploaded the
  sdist to PyPI without ever building from it, so a manifest change could ship
  an unbuildable source distribution while the in-repo build stayed green;
  `briefcase-core` is a path dependency, which is exactly the fragile case.
- `mypy briefcase/` is clean across all 90 modules and gated in CI. Settings
  live in `pyproject.toml`, with overrides for modules that genuinely carry no
  type information: the compiled extension, the optional dependencies behind
  extras, and the enterprise-only lakeFS module. Fixing the backlog corrected
  `BitemporalRecord.source`, annotated as `str` while both backends declare the
  column nullable and both readers can yield `None`.
- CI runs every Python example. Nothing did, which is how the Rust example came
  to reference a dependency path that no longer existed.
- Lint and format gates now exist and pass. `cargo fmt --all --check` runs in
  CI (19 files had drifted); `flake8` runs over the package, scripts, tests, and
  examples with config in `.flake8` (0 issues, from 1016 at defaults). The dev
  extra declared black/mypy/flake8 but no workflow invoked any of them. Fixing
  the backlog removed a bare `except:` in the lakeFS decorators that swallowed
  `KeyboardInterrupt` and `SystemExit`, several dead imports, and a lambda
  binding; `mypy` is clean on `briefcase.integrations.evals` and `.gym`.
- CI runs `cargo test`/`clippy` for `briefcase-core` with `--all-features`
  instead of `--features drift`. `sanitize` is not a default feature, so every
  PII-redaction test was compiled out and only 70 of 186 tests ran; a sanitizer
  regression could pass CI green. CI also runs `version_sync.py check`, which
  is how a stale release target went unnoticed.
- A bare `pytest` collects only `tests/` (`testpaths`). The two suites cannot
  share an interpreter, since `tests/` mocks `briefcase._native` process-wide;
  running them together produced 93 confusing failures. CI passes each path
  explicitly and is unaffected.
- Removed the `python_native_version` sync target: `__version__` in
  `bindings/python/src/lib.rs` is `env!("CARGO_PKG_VERSION")`, so the literal
  the target's regex required no longer exists and `version_sync.py check`
  failed, blocking releases.
- Synchronous export runs the coroutine on a helper thread when an event loop
  is already running; export failures log at `WARNING` instead of `DEBUG`.
- Bitemporal SQLite timestamps normalize to UTC before serialization so
  lexicographic order equals time order.
- Guardrails result cache is a bounded LRU (1024 entries) instead of an
  unbounded dict.
- Storage pagination saturates on `offset + limit` overflow and applies content
  filters before `LIMIT`/`OFFSET`; WAL checkpoint uses `query_row` so flush no
  longer errors on the row-returning PRAGMA.
- Nested correlation workflow contexts restore the previous context on exit.
- CLI registry writes use per-call unique temp files, so concurrent writers in
  one process no longer delete each other's in-flight temp file.
- `Sanitizer.remove_pattern` removes a custom pattern shadowing a built-in
  name before the built-in, so every pattern stays removable.
- lakeFS mock-mode `list_objects` entries carry `"mock": True` like the other
  mock metadata.

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
