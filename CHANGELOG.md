# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `auto(dedupe="none"|"provider"|"framework")` and `briefcase exec --dedupe`;
  provider records carry `parent_run_id` / `parent_framework` linking them to
  the framework run that made the call. `briefcase.auto.status()` reports what
  is patched or skipped and why. Opt-in OpenAI stream usage
  (`stream_usage=True`) and aiobotocore support.
- Zero-code Python instrumentation. `briefcase exec -- python app.py` runs an
  unmodified program with capture switched on. `briefcase.observe()` with no
  arguments resolves its destination from `BRIEFCASE_EXPORTER` (and
  `BRIEFCASE_EXPORTER_TOKEN`), then the standard `OTEL_EXPORTER_OTLP_*`
  variables, then the console. `briefcase.auto()` with no arguments
  instruments every installed framework and provider SDK (`skip=[...]`,
  `lazy=True`); `auto("name")` still raises `ImportError` when the package
  is missing.
- Provider auto-instrumentation for the OpenAI, Anthropic, Google GenAI, and
  Bedrock (boto3) SDKs, sync, async, and streaming: one decision record per
  call with model, token usage, latency, status, finish reason, and request
  id. Prompt and completion content is off unless `capture_content` is set.
- `briefcase connect`: one command that configures the coding tools on the
  machine to report to briefcase. It detects Claude Code, Codex CLI, Cursor and
  VS Code Copilot Chat, shows a plan with a unified diff, asks once, and writes
  key-level edits (JSON keys merged in place, one marker-delimited TOML block)
  with a timestamped backup. `--undo` removes exactly the keys it set, `--dry-run`
  writes nothing, and re-running is a no-op. Malformed JSON, JSON with comments
  and managed settings are never rewritten; the snippet is printed instead.
  Claude Desktop/Cowork, Copilot CLI, Cline, Windsurf and opencode are
  print-only, and Gemini CLI is experimental. Prompt and tool content is
  enabled in a tool only with `--capture-content` (Copilot CLI then gets
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT=SPAN_ONLY`). Tested against Claude
  Code 2.1.330 and Codex CLI 0.171.0.
- `briefcase receive`: a stdlib OTLP/HTTP receiver (default `127.0.0.1:4318`)
  that turns Claude Code, Cowork, Codex and Copilot telemetry and Cursor hook
  events into decision records and sends them to any exporter (`--to console`,
  a `.jsonl` path, an `https://` URL, or `otlp`). Prompts, responses, tool
  input and host paths are dropped and `user.email` is hashed unless
  `--keep-content` is given. A non-loopback `--host` requires `--token`.
  `--detach`, `--stop` and `--status` manage a background instance.
- `receiver` extra (`opentelemetry-proto`) for OTLP/protobuf, which Codex CLI
  sends. Claude Code and Copilot CLI (`http/json` by default) need no extra, so
  `pip install briefcase-ai && briefcase connect && briefcase receive` works
  for them as is.
- `briefcase mcp install` and `briefcase mcp link <tool>`: per-tool install
  payloads for `briefcase-mcp` (Cursor deeplink, `claude mcp add`,
  `codex mcp add`, `code --add-mcp`, project `.mcp.json` / `.cursor/mcp.json` /
  `.vscode/mcp.json`), and a Claude Code plugin
  (`claude plugin install briefcase@briefcase-ai`).
- `briefcase hook cursor` (the hook entry point `connect` installs; always
  exits 0) and `briefcase doctor --connect`.
- `briefcase.semantic_conventions.cowork` gains `assistant_response`,
  `api_refusal`, `tool_use_id`, and the event-name prefixes of sources that
  share the vocabulary.
- `@briefcase-ai/runtime`, a Node-only TypeScript package with explicit
  `integrity`, `lakefs`, `connectors`, and `trace` subpath exports. It adds
  Python-compatible hash chains, a typed lakeFS client, provider-neutral
  connector orchestration and OAuth security primitives, and an asynchronous
  invocation/step recorder behind application-owned persistence ports.
- `briefcase.exporters.HTTPExporter`: a stdlib-only exporter that POSTs
  `{"records": [...]}` JSON batches (by size and flush interval) with an
  optional bearer token and headers, retries 5xx/429/network errors with
  jittered backoff, never follows redirects, and requires https (plain http
  only for loopback hosts or with `allow_insecure=True`).
  `briefcase.observe("https://...")` wires one up.
- `briefcase.flush(timeout=)`, `briefcase.shutdown(timeout=)`,
  `briefcase.export_stats()`, `briefcase.configure_export(...)`, and a
  top-level re-export of `wait_for_pending_exports`. Queued records are now
  also drained at interpreter exit (up to 5 s).
- `@capture` on generator and async-generator functions: one record when the
  stream finishes, is closed, or raises, with the collected chunks as output
  (`chunk_count`, `stream_completed`; at most 1000 chunks retained) and the
  whole stream's duration.
- `@capture(sample_rate=..., rng=...)` records a fraction of calls; pass a
  seeded `random.Random` for deterministic sampling.
- Capture records carry `schema_version` (`"1.1"`), and, when present in the
  ambient context, `workflow_id` (from `briefcase.correlation`) and
  `trace_id` / `parent_span_id` / `trace_flags` (the OpenTelemetry span active
  at call time).
- `capture` is typed with `ParamSpec` overloads, so decorated functions keep
  their signatures under mypy/pyright.
- `MemoryExporter(max_records=N)` keeps only the newest N records (default
  remains unbounded).
- `OPARouter(headers=..., token=..., cache_max_entries=...)` and
  `OPARouter.aclose()`; `KafkaPublisher(config={...})` for SASL/TLS and other
  librdkafka settings, plus `flush()` / `close()`; `WebhookEmitter`
  `max_attempts` / `backoff_base` / `backoff_max`.
- `briefcase.auto(...)` is callable from the top level (`briefcase.auto`
  remains the submodule), and the exporter classes are importable from
  `briefcase` directly.
- `GatewayConfig.run_sync_in_thread` (default `True`).
- `NessieClient` now talks to a Nessie server over its REST v1 API (read
  content, staged writes, commit with `expectedHash`).
- Framework handlers accept `capture_content` (off by default, see Changed),
  `redact`, `max_records`, and (LangChain, LlamaIndex, CrewAI, OpenAI Agents)
  `max_inflight` / `inflight_ttl_seconds`. The LangChain handler exports failed
  chains and standalone LLM/tool/retriever runs, reads `usage_metadata`, and
  captures streamed tokens.
- `CostCalculator.load_pricing_json` / `load_pricing_file` (Rust and Python)
  load user-supplied model prices; `add_model` / `remove_model` are exposed to
  Python. New sanitizer detectors: JWT, bearer tokens, IPv6, IBAN, and
  contextual AWS secret keys.
- `SqliteBackend(path, append_only=True)`: saving an existing id raises
  instead of overwriting, and deletes are refused. Off by default.
- The sanitizer redacts PII in JSON object keys (`set_redact_json_keys(False)`
  restores the old behaviour).
- `briefcase.mcp` supports `mcp` 2.x as well as 1.x.
- TypeScript decision records and Gateway records in both languages carry
  `schema_version` "1.1".
- Budgets (`briefcase.budgets`): hard and soft spending limits on LLM calls, in
  USD and/or tokens, per app, per model, or per tenant/user/session/key
  (`budgets.budget_scope(tenant="acme")`), over calendar UTC windows (hour, day,
  month) or a total. Each call reserves its maximum cost before it is sent
  (input estimate + output cap, at the packaged prices) and settles to the
  provider's reported usage afterwards; unknown usage keeps the reservation
  (`cost_source: "reserved"`). `on_exceed` = `block` (`BudgetExceeded` before
  sending), `warn`, or `degrade` (switch to a cheaper model). Unpriced models:
  `unpriced = "warn"` (default) / `"allow"` / `"block"`.
- Budget stores: in-process (default), Redis (atomic Lua reserve/settle) and SQL
  (SQLite, PostgreSQL, MySQL; conditional `UPDATE`, retried on deadlocks),
  tested against real Redis 7, PostgreSQL 16 and MySQL 8 servers including two
  processes.
- `briefcase budget set|unset|show|status|test` and `~/.briefcase/budgets.toml`
  (`$BRIEFCASE_BUDGETS_FILE`); a `briefcase status` row that turns to attention
  above 80%.
- Content guardrails (`briefcase.guardrails.content`): PII/secret checks (native
  Sanitizer when importable plus pure-Python detectors for `password=`/`secret=`
  assignments, `Authorization`/`Bearer` credentials, PEM/OpenSSH/PGP private
  keys, international and national phone numbers), prompt-injection heuristics
  on tool and retrieved content (`content.retrieved(text)`, pluggable
  `classifier=`), and output policies (regex, JSON schema, callable). Effects
  `block` / `redact` / `warn`; redaction keeps the SDK object's type; `block`
  checks fail closed when the checker raises. Streams: final-text checks,
  optional per-chunk early stop.
- `briefcase guard test|show|init` and `~/.briefcase/guard.toml`
  (`$BRIEFCASE_GUARD_FILE`); a `briefcase status` row.
- Call hooks for provider auto-instrumentation
  (`briefcase.integrations.providers._base.add_call_hook`): `before(ctx)` /
  `after(ctx, outcome)` around every instrumented openai, anthropic,
  google-genai and Bedrock call, including streams; nothing runs when no hook is
  registered.
- Provider records gain optional fields `budget_effect`, `budget_scope`,
  `budget_checks`, `cost_usd`, `cost_source`, `cost_reserved_usd`,
  `budget_degraded_from`, `guard_effect`, `guard_findings`, `guard_limits`.
- `controls.Gateway`: `GatewayConfig(budgets=...)` and `invoke(...,
  budget=BudgetRequest(...))`, new outcome reason `budget_exceeded` and
  `Outcome.budget`.
- `briefcase` with no arguments is now a front door. The first time on a
  terminal it runs a guided setup: one question (AI coding tools, an app that
  calls LLMs, or a team rollout), then two or three steps that call the existing
  commands (`connect`, `receive --detach`, `exec`), each with its own
  confirmation. Completion is remembered in `~/.briefcase/setup.json` only after
  the guide was finished or skipped; `briefcase setup` runs it again. Without a
  terminal, or with `--no-input` / `BRIEFCASE_NO_INPUT=1`, it prints the command
  list and the status screen and exits 0.
- `briefcase status [--json]`: one screen showing connected coding tools, the
  receiver and its request counters, MCP registration and where records go,
  ending with exactly one `Next:` suggestion. Command modules can add a row with
  an optional `status()` hook.
- `briefcase features [ID] [--verb V] [--json] [--markdown]`: the feature
  catalog, grouped by Connect, Capture, Enforce, Verify and Review, generated
  from one JSON manifest per feature in `briefcase/_features/`. `briefcase
  features <id>` shows the docs link and whether the feature is active on this
  machine. `briefcase.features.load_catalog()` reads the manifests through
  `importlib.resources`, skips malformed files with a warning and ignores
  unknown keys. Manifests are seeded for the 23 features that already existed.
- `briefcase completion bash|zsh|fish`: a completion script generated from the
  installed commands, with no new dependency.
- Command auto-discovery: every `briefcase/cli/*_commands.py` module with a
  `register(subparsers)` function is registered without editing `__main__.py`. A
  module declares its help group with `GROUP` (and optionally its position with
  `ORDER`). A module that fails to import is skipped, and `--debug` names it.
- `briefcase/cli/_ux.py`: shared helpers for the `Next:` line, errors with a
  `Fix:` command, `--json` output, prompts, and terminal and `NO_COLOR`
  detection.
- Global flags `--debug` (tracebacks, skipped modules; also `BRIEFCASE_DEBUG=1`)
  and `--no-input`.
- `docs/features/`: the three entry points, the contributor protocol (command
  modules, status hooks, manifests) and a generated catalog page.
- Adapter conformance harness (`python -m scripts.conformance
  run|drift|scrub|merge|changes`): installs the latest Claude Code, Codex CLI,
  Gemini CLI and Copilot CLI into an isolated prefix and temp HOME, runs
  `briefcase connect --yes`, checks the result with each tool's own commands,
  and records a level (`docs-only` < `config-accepted` < `telemetry-verified`)
  with tool version and date.
- Docs-drift check: `briefcase/connect/conformance.json` lists the strings each
  adapter depends on; `python -m scripts.conformance drift` fetches each
  `docs_url` and reports expectations that vanished (network failures are
  "unknown").
- Captured-payload corpus `tests/receiver/fixtures/real/` with a scrubber and
  tests; seeded with Claude Code 2.1.330 telemetry emitted by the real binary.
- `.github/workflows/conformance.yml`: nightly and manual run, per-tool matrix,
  report artifact, one tracking issue on a level change or failure. Never runs
  on pull requests.
- `briefcase doctor --connect` shows the last known conformance level per tool
  and flags an installed version newer than the tested one
  (`briefcase/connect/conformance-report.json`).
- `briefcase dataset from-records FILE.jsonl --name NAME`: turn captured
  decision records into an immutable, content-addressed evaluation dataset
  version, with `--where` filters (globs; `status`, `model`, `prompt_name`,
  `prompt_version` keys), seeded `--sample`, `--dedupe`, `--reference
  recorded|none` and `--redact-fields`. Records captured without content are
  counted and explained, never turned into empty cases. Content is run through
  `briefcase.sanitize` when the native extension is present; otherwise
  `--allow-unsanitized` is required and the output says so.
- `briefcase dataset show|diff|export|import`: inspect and compare versions;
  export as hand-editable JSONL, inspect-ai `Sample` JSONL, or an lm-eval docs
  file plus task YAML (experimental); re-import edited JSONL with errors
  reported by line. `briefcase dataset list` also lists local versions and takes
  `--json`; `dataset register` / `list` behave as before.
- `briefcase eval run --dataset NAME[@v] --target module:function`: score a
  function per case offline with `exact`, `contains`, `json` or a custom scorer,
  `--concurrency`, CI gates (exit 1): `--fail-under X` for a single target,
  `--fail-under X --gate TARGET` to gate named targets when comparing several,
  and `--no-regression BASELINE [--tolerance T]` to fail when another target
  scores below the baseline; `--fail-under` with several targets and no `--gate`
  is a usage error (exit 2), a results table split by prompt version with the
  worst failures first. Results are `eval.case` / `eval.run` records logged
  through `EvalRun` (content as SHA-256 digests unless `--capture-content`).
- `briefcase.prompts`: `prompt(name, template)` gives a template a content-hash
  version; records made while it is used carry `prompt_name` and
  `prompt_version`. `briefcase prompt list|show|diff`.
- `status()` hooks for `briefcase status`: dataset count and the last eval
  result.
- `briefcase engine install|up|down|status|logs|doctor|upgrade|uninstall`:
  install the licensed Briefcase engine (oci-jj) from the gated portal
  (https://observe.briefcasebrain.io/engine): `engine install --code bci_...`
  (single-use install code), `--token bcl_...` (automation), or offline
  `--bundle FILE --manifest FILE` (`docker load`, `skopeo` fallback, no
  network). The online flow calls `POST /v1/engine/install`, downloads from
  presigned URLs, logs in to ECR with a short-lived password on stdin, pulls
  every image by digest from the signed manifest and logs out. A digest-verified
  single-box compose with generated secrets, required gRPC auth and an optional
  `--tls`.
- Release verification: manifest v2 (`schema_version`, `key_id`,
  `min_sdk_version`, files with sha256/size/signature, images by digest) signed
  with an ECDSA P-256 KMS key via `cosign sign-blob --tlog-upload=false`,
  checked in Python with `cryptography` (or `cosign verify-blob --key` without
  it) against release keys pinned in `briefcase/engine/keys/release-*.pub`. No
  override exists. This build ships no key yet, so install refuses with "no
  trusted release key is bundled in this SDK build".
- `briefcase engine verify FILE [--signature S] [--manifest M]`: check a
  manually downloaded file against the pinned key; prints the key id.
- `engine status` shows the key id the install was verified with and, given a
  licence token, whether a newer release exists (`GET /v1/engine/index`).
  `engine up`/`doctor` refuse an image whose local tag no longer points at the
  image verified at install.
- Releases ship `oci-bai` for Linux only; on macOS (or any OS without a build)
  the install still succeeds and the commands that need `oci-bai` run it from
  the verified server image with `docker run`. A verified macOS build, should
  one ship, has `com.apple.quarantine` removed only after verification.
- `briefcase graph
  log|diff|search|provenance|whodepends|show|explain|resolve|referrers|optimize`
  (Review) and `briefcase env
  push|cohort|compose|checkout|mount|bookmark|undo|gc` (Capture).
- `briefcase run attach ID --result verdict.json`, `briefcase dataset publish
  NAME FILE` (tenant-scoped `s3://` dataset in the local engine), `briefcase
  verify --graph REF` (engine fsck).
- The `engine` extra (`grpcio`, `protobuf`, `cryptography`) and the vendored
  Apache-2.0 gRPC contract in `briefcase/engine/_proto` (regenerate with
  `scripts/engine/regen_proto.py`).
- Engine commands are listed in `--help`, `briefcase features` and `briefcase
  status` without an install, marked "engine not installed — `briefcase engine
  install`", and exit 3 when run.
- Hash scheme `jcs-1`: RFC 8785 canonical JSON in Python
  (`briefcase.integrity.canonical_json_v2`, `canonical_bytes`,
  `parse_canonical_text`), TypeScript (`canonicalJsonV2`, `canonicalBytes` in
  `@briefcase-ai/runtime/integrity`) and Rust
  (`briefcase_core::integrity::canonical_json_v2`), byte-identical across the
  three, floats included. Integers a double cannot hold exactly are rejected,
  never rounded. Golden vectors `tests/vectors/canonical_json_v2.json` (RFC 8785
  examples, published JCS test data, floats, large integers, astral keys,
  escapes) are asserted by all three languages and in the `canonical-vectors` CI
  job.
- `hash_scheme` on every hash-carrying structure: `HashChainEntry` (Python and
  TypeScript; absent means `json-sorted-1`), chained-store entries and
  checkpoints, bundle manifests. Verifiers dispatch on it.
  `HashChainAppender(..., hash_scheme="jcs-1")` / `{ hashScheme: "jcs-1" }` opts
  a chain in; the default stays `json-sorted-1`.
- Tamper-evident store (Rust feature `chain`):
  `SqliteBackend::with_chain(ChainConfig)` / `SqliteBackend(path, chained=True,
  signing_key=..., checkpoint_every=100)` in Python. Each saved decision or
  `append_record` gets `seq`, `prev_hash` and `entry_hash` in one immediate
  transaction (no forks across processes); overwrites and deletes are refused.
  Merkle checkpoints every N entries or on demand, Ed25519-signed with keys
  interchangeable with `briefcase.integrity.signing`. `verify` / `verify_chain`
  report broken links, missing ranges, modified entries, checkpoint and
  signature failures, unexpected keys, and truncation against an externally kept
  checkpoint (`against_checkpoint`). `inclusion_proof(seq)`.
- `briefcase.observe("chain:evidence.db")` and
  `briefcase.exporters.ChainedSqliteExporter` (`BRIEFCASE_SIGNING_KEY`,
  `BRIEFCASE_CHECKPOINT_EVERY`); a checkpoint is written on shutdown.
- `briefcase verify PATH` for a chained store, chain JSONL or evidence bundle of
  any version: plain-language verdict, `--json`, `--against CHECKPOINT`,
  `--key`, `--kms`, `--save-checkpoint`; exit 0 intact, 1 tampered or
  incomplete, 2 usage. Pure Python, read-only.
- One evidence bundle format (`briefcase.compliance.bundle`, `briefcase-bundle`
  format 2): a ZIP with manifest (file hashes, RFC 8785 `content_hash`, sealed
  signer list, signatures), records (one or many; a sealed routing decision is a
  bundle whose records are the decision, policy and evidence), chain entries
  with Merkle proofs, checkpoints, public keys and a `README.txt` on what it
  does and does not prove. One verifier, `verify_bundle`, dispatching on format
  version, hash scheme and signer type, also accepts the released format-1
  `ExaminerBundle` JSON.
- Pluggable signers (`briefcase.compliance.signers`): `Ed25519Signer` (key files
  / JWK), `KmsSigner` (AWS KMS, same key-id and signing-algorithm metadata as
  `SignedExaminerBundle`), a `Signer` protocol and `register_verifier` for
  others. Verifiers pin keys; KMS is only called when the caller passes a client
  (`--kms`).
- `briefcase bundle create|verify|inspect|keygen`: create from a chained store
  (select by session, workflow, trace or time) or re-package a format-1 examiner
  JSON (its KMS signature is kept and still checked); `--sign-key` accepts key
  files and `kms:KEY_ID`.
- `ExaminerBundle.to_bundle(path, signers=...)` / `from_bundle(path)`;
  `SignedExaminerBundle.build_and_sign` / `verify_signature` accept
  `kms_client=`; `briefcase.compliance.migrate_legacy`, `inspect_bundle`,
  `write_bundle`.
- TypeScript: `verifyBundle`, `readZip` in `@briefcase-ai/runtime/integrity`:
  format-2 verifier (files, seal, Ed25519 via `node:crypto`, checkpoints, Merkle
  proofs); KMS signatures are reported unchecked and format 1 is not read.
- `briefcase.integrity.merkle`, `briefcase.integrity.keys`,
  `briefcase.integrity.evidence`.
- `status()` hook for `briefcase status` (`chained-store`).
- Plugin entry points (`briefcase.plugins`, plugin API 1): installed packages
  can add exporters (`briefcase.exporters`, used by `observe("NAME:TARGET")` and
  `BRIEFCASE_EXPORTER`), bitemporal backends (`briefcase.bitemporal_backends`,
  `get_backend(NAME)`), VCS providers (`briefcase.vcs_providers`,
  `get_provider(NAME)`), receiver sources (`briefcase.receiver_sources`, `POST
  /v1/sources/NAME`), `briefcase connect` adapters
  (`briefcase.connect_adapters`), guardrails (`briefcase.guardrails`,
  `make(NAME)`), CLI commands (`briefcase.cli_commands`) and feature cards
  (`briefcase.features`). Plugins are imported only when their name is used
  (`briefcase --help` imports none), a broken one is skipped with one warning
  naming its package, built-in names always win, and `BRIEFCASE_PLUGINS=off` /
  `BRIEFCASE_PLUGINS_DISABLE=...` turn them off. A plugin may declare
  `BRIEFCASE_PLUGIN_API`; an unsupported version is refused with the upgrade
  command.
- `briefcase plugins [list|show NAME] [--json]`, and `briefcase doctor
  --plugins` (the default `briefcase doctor` adds a plugins section only when a
  plugin is installed).
- `briefcase-mcp` tools over recorded decisions, all read-only:
  `search_decisions`, `get_decision` (content only with `include_content=true`,
  capped), `get_trace` (tree with durations, tokens and cost; provider records
  under framework runs counted once), `summarize_usage` (recorded vs
  `briefcase.pricing`-estimated vs unpriced cost), `explain_policy_decision`,
  `verify_records` (hash chain and payload hashes; bitemporal SQLite content
  hashes), `replay_plan` (plans only, never runs) and `drift_check`. Reads the
  receiver's records, a `.jsonl` `BRIEFCASE_EXPORTER`, `BRIEFCASE_MCP_RECORDS`
  or a `path` confined to the home directory or `BRIEFCASE_MCP_ROOTS`. Works
  with mcp 1.x and 2.x.
- `briefcase.bitemporal.backends.get_backend` / `open_backend` /
  `available_backends` and `briefcase.integrations.vcs.get_provider` /
  `available_providers`: name-based lookup of the built-in backends and
  providers.
- `briefcase policy` (init, check, test, log, off, status): allow, ask or deny
  what Claude Code, Cursor and Codex CLI may run. A `PreToolUse` / `before*`
  hook decides in-process against `~/.briefcase/policy.toml` (presets `observe`,
  `guarded`, `strict`; project and system policy layers that can only make it
  stricter), with shell analysis that follows quoting, chaining, substitutions,
  `sh -c`, `eval`, `sudo`/`env` wrappers and here-documents, and path rules that
  resolve `..` and symlinks. Fails open by default and says so in `briefcase
  status`; `fail_closed = true` blocks instead. Claude Code 2.1.330 verified end
  to end; Cursor and Codex from their documentation.
- `briefcase hook claude-code|codex|cursor --enforce`: the hook entry point,
  always exit 0, with the verdict as each tool's documented JSON.
- `briefcase connect --enforce` (connect plus `policy init`) and `briefcase
  connect --managed --out DIR`: a rollout pack for administrators (Claude Code
  `managed-settings.d` drop-in and macOS profile, Cursor enterprise
  `hooks.json`, Codex system `hooks.json`, labelled templates for the rest, and
  a README per OS). Nothing is written to system paths.
- Receiver: `POST /v1/policy/decisions` turns hook decisions into
  `policy.tool_call` records (rule id and effect; command and path only as a
  keyed hash unless content capture is on).
- `briefcase.policy.guardrail.ToolPolicyEnv`: the same policy as a guardrails
  `GuardrailEnv`.
- Versioned pricing data: `briefcase/data/pricing/v1.json` (39 models with
  `aliases` and null provenance fields, extracted mechanically from `cost.rs`)
  and `unpriced.json` (ids of common models with no shipped price).
- `briefcase.pricing`: layered loading (packaged < `~/.briefcase/pricing.json` <
  `$BRIEFCASE_PRICING_FILE` < explicit), line-anchored validation, `apply()`
  into a native `CostCalculator`, staleness helpers, and a pure-Python
  `estimate()` for flat per-token pricing that raises "needs native extension"
  for anything else.
- `briefcase pricing list|show|set|unset|import|check`; `check --records FILE`
  lists the models in your records that have no price.
- `scripts/pricing/extract.py`, `generate.py --check` and a cargo oracle, plus
  drift tests tying the JSON to `cost.rs` and
  `js/controls/src/bedrock/pricing.ts`.
- Provider API contract tests (`tests/contracts`,
  `docs/features/provider-contracts.md`): the provider instrumentation, the
  replay executors, the budget token caps and the datasets/eval flow are
  checked, offline and with no model call, against OpenAI's published OpenAPI
  spec, the anthropic 2.12.0 request/response types (Anthropic publishes no API
  spec), the Gemini API discovery document (downloaded, not vendored) and
  botocore's Bedrock Runtime service model. Specs are pinned with source URL,
  retrieval date, SHA-256 and licence in `tests/contracts/specs/manifest.json`;
  `scripts/refresh_provider_specs.py` and a weekly workflow report upstream
  drift. CI runs the suite; `jsonschema` joins the `dev` extra.
- `briefcase replay run|explain|diff` and `briefcase.replay.recorded`: replay
  LLM calls captured by `briefcase.auto(capture_content=True)` from a JSONL
  file, through the user's installed OpenAI, Anthropic, google-genai or Bedrock
  SDK (`--executor auto`) or a no-network cassette (`--executor cassette`).
  Shows a plan with per-model counts and a token bound and asks before live
  calls (`--yes`, `--max-calls`, `--limit`, `--sample`, `--concurrency`,
  `--max-retries`). Compares answers at exact, normalized, JSON-structural and
  optional `--scorer` levels, with structural tool-call comparison, unified
  diffs, and token, cost and latency deltas. Records that cannot be replayed
  (content off or hashed, truncated or binary content, partial streams,
  unanswered tool calls, failed originals) are reported with the reason. What-if
  replays with `--model`, `--param`, `--drop-param`. Replays are not re-captured
  as decisions and never run tools.
- `briefcase drift baseline save|check` and `briefcase drift baselines`, plus
  `briefcase.drift.baseline`: save a content-free baseline profile to
  `~/.briefcase/baselines/<name>.json` and compare a later window against it:
  PSI and two-sample KS for latency, output tokens and output length; mean
  tokens and cost per call; error, refusal and finish-reason rates
  (two-proportion z test); model mix and categorical output shift
  (Jensen-Shannon, chi-square); character n-gram lexical drift; optional
  `embed=` hook. Signals under `min_n` records are refused, not scored.
  `--fail-on attention` makes it a CI gate.
- Feature manifests `replay-recorded` (verify) and `drift-baseline` (review),
  with `status()` hooks for `briefcase status`.
- `@briefcase-ai/auto` (new npm package, `js/auto`): zero-code decision capture
  for Node. `node --import @briefcase-ai/auto/register app.js` (or `observe()` +
  `auto()` in code) records one decision per `openai`, `@anthropic-ai/sdk`,
  `@google/genai` and `@aws-sdk/client-bedrock-runtime` call, with the same
  field names and `decision_type` values as Python's `briefcase.auto()`.
  Destinations and environment variables match `briefcase.observe()`: console,
  `*.jsonl`, `http(s)://` batches (`{"records": [...]}`), and OTLP/HTTP JSON.
  Content capture is off by default. `withWorkflow()` and an active
  OpenTelemetry span are carried into records as `workflow_id` / `trace_id`.
  Requires Node 26 or later (`engines.node` `>=26`, like the other TypeScript
  packages; tested on 26.0.0 and 26.13.1). SDKs are patched lazily, when the
  application loads each copy (a `module.registerHooks` load hook), so startup
  with all four SDKs installed costs about +36 ms p50; `auto({ eager: true })` /
  `BRIEFCASE_AUTO_EAGER=1` load every installed copy up front, and `status()`
  reports a provider whose SDK is installed but not loaded yet as `waiting`.
  `@google/genai` clients created before `auto()` are recorded too (one record
  per call, except non-streamed automatic function calling on such a client,
  which gives one record per model round). Worker threads are instrumented by
  the preload; their records are forwarded to the main thread's destination as
  each call finishes, so they survive `worker.terminate()`.
- `instrument(client)` (also accepts the client classes): instruments the SDK
  copy a client belongs to, for SDKs bundled into the application.
- `@briefcase-ai/auto/esbuild`: `briefcaseAuto()` esbuild plugin that keeps
  bundled SDKs recordable, and `SDK_PACKAGES` for `external`.
- A warning (once per provider) and `status().providers[i].unrecordedRequests`
  when a provider request is made by an SDK copy that is not instrumented (e.g.
  bundled).

### Changed
- **Breaking:** `briefcase exec` refuses `python -I/-E/-S` (override with
  `--allow-uninstrumented`) and exits 2 on an unknown `BRIEFCASE_EXPORTER`, a
  malformed OTLP endpoint or an unsupported OTLP protocol.
- **Breaking:** the OTLP protocol defaults to `http/protobuf` and is no longer
  guessed from the port number.
- **Breaking:** VCS clients, `BranchManager`, `StagedArtifactClient.from_env`,
  `VersionedEmbeddingPipeline`, and `InstrumentedRetriever` no longer return
  placeholder data unless `mock=True`. They raise `ImportError` (missing
  provider SDK), `NotImplementedError` (DuckLake, Iceberg, Pachyderm, and
  ArtiVC operations have no real implementation yet), or `ValueError`.
  Embedding-model errors propagate; zero vectors are never stored.
- **Breaking:** `VcsClientBase.read_object`, `write_object`, and
  `create_version` propagate provider errors. DVC and git-LFS
  `create_version` commit only paths written through the client or passed as
  `track_paths` (`add_all=True` restores `git add -A`) and raise
  `NothingToCommitError` instead of returning the previous HEAD.
- **Breaking:** `SamplingWrapper` denies unsampled requests by default
  (`skip_effect=Effect.ALLOW` restores the old behaviour), and
  `GuardrailPipeline` turns a raising stage into a DENY (`on_error="raise"`
  restores propagation).
- **Breaking:** `briefcase.auto.auto()` raises `ImportError` for a framework
  that is not installed.
- **Breaking:** framework handlers (`briefcase.auto("langchain")`,
  `"llamaindex"`, `"crewai"`, `"openai-agents"`, `"autogen"`, `"ag2"`,
  `"pageindex"`, and the handler classes themselves) record no prompt,
  completion or error text by default, like the provider integrations,
  `briefcase exec`, `connect`/the receiver and eval records. They take the
  providers' values (`capture_content="full"`/`True`, `"hash"`,
  `"none"`/`False`) and `BRIEFCASE_CAPTURE_CONTENT` through `auto()`. The
  one-time "records FULL content" warning is gone. `@briefcase.capture` is
  unchanged: decorating your own function is the opt-in and records its
  arguments and return value. Migration: `briefcase.auto("langchain",
  capture_content="full")` or `BRIEFCASE_CAPTURE_CONTENT=full`.
- **Breaking:** `@briefcase-ai/controls`, `@briefcase-ai/runtime` and
  `@briefcase-ai/native` now declare `engines.node` as `>=26` (was `>=20.19`,
  and `>= 12.22` for native). Node 26 is the Active LTS; Node 20 reached end of
  life on 2026-04-30. The code uses nothing newer than `AbortSignal.any` (Node
  20.3), so no later 26.x minor is required.
- `briefcase receive` reads `BRIEFCASE_EXPORTER_TOKEN`; `BRIEFCASE_EXPORT_TOKEN`
  still works with a deprecation warning.
- Decision records carry `schema_version` "1.2" (adds optional `prompt_name`,
  `prompt_version`, `parent_run_id`, `parent_framework`); `prompt_name` and
  `prompt_version` are set by `briefcase.prompts` (see Added) and are also in
  the golden schema fixture.
- `briefcase.guardrails.framework` is now a package; every public name is
  re-exported. `VectorGuardrailEnv` evaluates in a thread pool,
  `AuditWrapper` is a ring buffer with a `dropped` count, and `SyncAdapter`
  runs off the event loop.
- External-data snapshot ids are unique (microseconds plus a random suffix),
  `snapshot_stored` is `False` with `snapshot_error` when the upload fails,
  and a configured retention policy is enforced after each snapshot.
- Background export now uses a bounded queue (10,000 records by default,
  `BRIEFCASE_EXPORT_QUEUE_SIZE` or `configure_export(max_queue_size=)`). When
  it is full, new records are dropped, counted, and reported at WARNING
  (rate-limited) instead of growing memory without bound.
- Background exports that raise are retried (2 retries with backoff by
  default) and an awaitable export is cut off after 30 s. A background
  failure, and an exporter returning `False` on either path, is now logged at
  WARNING; background failures were previously DEBUG-only and `False` was
  ignored.
- Cancelled or timed-out `@capture` async calls now emit a record with
  `status: "cancelled"` and re-raise; previously they left no record. An
  exception with an empty message is recorded by its class name instead of
  `""`.
- `OTelExporter` reads dict records (what `@capture` emits) as well as
  objects, so spans are no longer missing `briefcase.decision.id` and friends.
  Spans now span the decision's own `started_at`..`ended_at`, are parented to
  the span that was active at capture time even when exported from the
  background thread, and get ERROR status for records with an `error`.
  `OPARouter`, `InternalRouter`, and `emit_low_confidence` accept dict records
  the same way.
- `GCPCloudLoggingExporter` only removes records from its buffer once they
  were sent; a failed send keeps the unsent records for the next flush (capped
  by `max_buffer`) and logs at WARNING. The buffer is lock-protected and the
  blocking client calls run off the event loop.
- `WebhookEmitter` waits with jittered exponential backoff between 5xx
  attempts, logs every failed delivery at WARNING, and no longer sends an
  `X-Briefcase-Signature` header when no secret is configured (it logs one
  WARNING instead of signing with an empty key).
- `OPARouter` reuses one HTTP client, logs each fallback to the internal
  router at WARNING (rate-limited), and bounds its decision cache (LRU, 1024
  entries by default).
- `Gateway.invoke` runs a synchronous `fn` on a worker thread so a blocking
  call no longer stalls the event loop, and `deadline_s` now also bounds that
  synchronous part. Set `GatewayConfig(run_sync_in_thread=False)` for the
  previous inline behaviour.
- `briefcase.setup(exporter=None)` (and `None` for any other argument) now
  clears the setting; omitted arguments are still left unchanged.
  `BriefcaseConfig.get()` is thread-safe.
- `briefcase.setup(webhook_url=..., webhook_secret=..., events=[...])` now
  takes effect: `briefcase.events.emit` delivers to that webhook. These fields
  were stored but never read.
- `import briefcase` no longer requires the compiled core. Pure-Python
  features work without it; native-backed names raise an `ImportError` that
  says how to get the core when first used.
- `briefcase --help` lists commands under "Start here", Connect, Capture,
  Enforce, Verify, Review and Evaluate instead of one flat list. `briefcase
  <command> --help` is unchanged.
- `briefcase` with no arguments exits 0 (it used to print help and exit 2). A
  command group without a subcommand, such as `briefcase run`, still exits 2 and
  now prints that group's help instead of the top-level help.
- An unexpected error in a command prints one `error:` line and a `Fix:` line
  and exits 1; the traceback needs `--debug`. `briefcase features | head` no
  longer ends in a broken-pipe traceback.
- The `dataset`, `secret`, `run`, `stack` and `doctor` parsers moved out of
  `__main__.py` into discovered modules (`job_commands.py`, `stack_commands.py`,
  `doctor_commands.py`) with the same names, flags, defaults and handlers.
- Dataset promotion sanitizes dictionary keys as well as values.
- The gRPC client builds messages from the vendored contract instead of server
  reflection (`BRIEFCASE_GRPC_REFLECTION=1` keeps reflection) and finds a local
  engine install on its own.
- `briefcase secret` says what it is for: the engine uses only
  `OCI_JJ_AUTH_TOKEN`.
- `run submit` says "was not submitted" when the engine refuses a run.
- SQLite schema migration 2 adds the empty `chain_entries`, `chain_checkpoints`
  and `chain_meta` tables (with UPDATE/DELETE guard triggers) to every database
  the core opens. Plain databases behave exactly as before; releases before this
  one ignore the tables.
- `briefcase-core` enables `serde_json`'s `float_roundtrip` feature so stored
  floats parse back to the exact double (needed to re-verify hashes). Float
  parsing is slightly slower.
- `JsonlHashChainStore` writes a `hash_scheme` field only for non-default
  schemes, so `json-sorted-1` lines are byte-identical to earlier releases.
- `SignedExaminerBundle` signs and verifies through `KmsSigner` /
  `kms_verify_raw`; behaviour and the format-1 JSON are unchanged (no
  deprecation).
- Not breaking: apart from the `canonical_json` key-escaping fix listed under
  Fixed, existing hashes, chains, bundles and defaults are unchanged. Data
  written with `jcs-1` (including any chained store) cannot be verified by
  releases older than this one.
- CI tests the TypeScript packages on Node 26 only (previously 20.19 and 22);
  the next even line is added when it ships. Single-version jobs, `publish.yml`
  and `conformance.yml` use Node 26.
- `briefcase --help` has a "Plugins" group; `briefcase mcp install` and
  `briefcase mcp link` list the server's tools.
- `briefcase.features.load_catalog(plugins=False)`: plugin feature cards are
  added only when asked for (`briefcase features` does; `--markdown` and
  `briefcase --help` counts do not).
- Provider records gain usage counters the specs define and capture dropped:
  `token_usage.cache_write_tokens` (OpenAI chat and Responses; a part of
  `prompt_tokens`, priced by budgets at the cache-write rate) and
  `token_usage.reasoning_tokens` for Anthropic
  (`output_tokens_details.thinking_tokens`, a part of `completion_tokens`).
  OpenAI chat records `verbosity`, google-genai records `frequency_penalty` and
  `presence_penalty`.
- `briefcase replay explain` names more request settings that capture does not
  record and that change the answer (OpenAI `web_search_options`, `modalities`,
  `audio`, `functions`, `function_call`, `moderation`; Responses `prompt`,
  `max_tool_calls`, `parallel_tool_calls`, `context_management`, `moderation`,
  `image`; completions `echo`; Anthropic `container`; Gemini
  `response_json_schema`, `response_modalities`, `cached_content`,
  `automatic_function_calling`, `routing_config`, `model_selection_config`;
  Bedrock `outputConfig`, `promptVariables`), so a replay that sends the API
  default instead is flagged.
- `briefcase.replay` and `briefcase.drift` resolve their native names
  (`ReplayEngine`, `ReplayPolicy`, `ReplayResult`, `ReplayStats`,
  `DriftCalculator`, `DriftMetrics`) on first use, so the new pure-Python
  submodules import without the compiled core. Using a native name without the
  core still raises the same `ImportError` (now at `from briefcase.replay import
  ReplayEngine` rather than at `import briefcase.replay`).
- The minimum supported Rust version is now 1.89 (was 1.85); CI builds on
  exactly that toolchain.
- The Python binding moves from pyo3 0.24 to 0.29, which also removes the two
  pyo3 advisory ignores from `deny.toml`.

### Deprecated
- `briefcase stack`: use `briefcase engine` (one release).

### Fixed
- `briefcase.observe("https://...")` sends `BRIEFCASE_EXPORTER_TOKEN` as the
  bearer token and accepts `token=` and `headers=`; a 401/403 logs one WARNING
  per exporter instead of silently dropping every batch.
- Records queued before a normal exit (including `sys.exit()`) are flushed on
  Python 3.12+; the exit drain no longer fails with "can't register atexit
  after shutdown".
- The GCP Logging exporter is fork-safe: fresh locks, the parent's unsent
  buffer is not re-sent, and the client is rebuilt in the child.
- `briefcase receive` default redaction keeps only identifier-shaped values:
  tool parameters, commands, file paths, URLs and error text are dropped
  unless `--keep-content` (a command such as `cat ~/project/.env` used to be
  kept).
- Exported records are no longer lost, duplicated or deadlocked in forked
  workers (gunicorn, multiprocessing fork, `os.fork`).
- An explicit `auto(name, ...)` call with different options re-applies them
  instead of being ignored.
- Anthropic `.stream()` capture no longer depends on a private SDK attribute.
- Provider patches never half-apply on SDK layout drift, `undo()` leaves later
  patches by other libraries in place, and unknown token usage is `null`, not 0.
- `@capture` records are self-consistent: `ended_at - started_at` equals
  `execution_time_ms` (both now come from the same two instants).
- `FixedWindowQuotaStore` denies on an invalid policy (it used to allow
  without limit) and evicts expired windows and cooldowns on a timer.
- `BriefcaseLlamaIndexHandler` exports every root event with its nested
  events as `child_spans`, and reads token usage from the response.
- Embedding batch and manifest ids are collision-free; `EmbeddingManifest`
  reports `uploaded` / `upload_error`.
- Framework handlers log lost records at WARNING.
- Replay without an executor reports Pending, not a matching success, in the
  sync engine too; policy rules on `function_name` and `execution_time_ms`
  resolve on replay.
- Rust core, SQLite store: single and batch saves write `created_at` in one
  format, so time-range queries no longer drop batch-saved (and therefore all
  `BufferedBackend`) rows; `PRAGMA user_version` migrations normalise existing
  databases; `busy_timeout` is set; `offset` without `limit` is valid SQL.
- Rust core, replay: `ValidationOnly` no longer reports `outputs_match: true`
  without comparing anything; a policy rule on a missing field is a violation;
  never-executed replays are counted as `unverified_replays`, not successes;
  `replay_batch(concurrency=0)` no longer hangs; the async engine enforces
  `timeout_ms`.
- Rust core, cost: `project_monthly_cost` works for large daily volumes;
  `add_model` / `remove_model` clear stale tier data; dated, prefixed, and
  `@date` model ids normalise; `gpt-4o` output cap is 16384.
- Rust core, storage: buffered reads see pending writes; the VCS adapter
  re-queues unwritten snapshots when a flush fails and can query what
  `save_decision` wrote; `VcsProviderConfig` no longer prints secrets in
  `Debug`.
- Rust core: `canonical_json` quotes and escapes keys, which changes
  `content_hash` for values containing JSON objects (`content_hash_legacy()`
  keeps the old value); `sdk_version` reports the real crate version; each
  storage and replay Cargo feature compiles on its own; client revalidation
  refreshes permissions and honours expiry.
- Sanitizer: bare 9-digit and 10/11-digit numbers are redacted only with
  nearby context (fewer false positives on timestamps and coordinates); JSON
  integers are scanned; pattern order is deterministic.
- Drift: pairwise distances are computed once, in parallel, with the GIL
  released; the tokenizer is cached.
- The MCP `how_to` tool and `briefcase://llms-full.txt` resource returned only
  a link when installed from a wheel, because the guide lived outside the
  package. It now ships as `briefcase/mcp/llms-full.txt`.
- `briefcase.replay`'s native-name `ImportError` told users to install the
  `replay` extra, which is empty; it now explains that the compiled core is
  missing.
- README, AGENTS.md, `llms.txt`, and `llms-full.txt` now list the same extras
  as `pyproject.toml` for "installs third-party packages" versus "empty
  grouping".
- `briefcase connect --undo` reported a hook entry that had been edited after it
  was written as "already removed" and left it in place; it now recognises its
  own entry by its marker. Undoing two owners of one file (connect telemetry and
  policy hooks in Cursor's `hooks.json`) no longer leaves an empty `"hooks": {}`
  behind.

### Security
- RUSTSEC-2026-0459 (SQLite page-cache disclosure through a crafted `-wal` file)
  is cleared by upgrading rusqlite 0.38 to 0.41 (libsqlite3-sys 0.39, SQLite
  3.54).
- RUSTSEC-2026-0592 (stack overflow in `Url::origin()`) is cleared by `url`
  2.5.9.
- RUSTSEC-2026-0176 and RUSTSEC-2026-0177 (pyo3 `nth`/`nth_back` out-of-bounds
  reads) are cleared by pyo3 0.29.

## [4.4.0] - 2026-08-16

### Added
- `briefcase.integrity.ChainConflictError` and compare-and-swap append
  semantics on `HashChainStore`: both bundled stores reject an entry whose
  `prior_hash` no longer matches the segment tail, and `HashChainAppender`
  re-reads the tail and retries with jittered backoff (`max_attempts`,
  default 32), so concurrent writers converge instead of forking the chain.

### Changed
- `JsonlHashChainStore` holds a POSIX `flock` around every tail read and
  append and folds in lines other writers appended (incremental, from a
  byte offset), making concurrent threads and processes on one host safe.
  On Windows (no fcntl) safety remains thread-only. The previous
  single-writer-per-file caveat is lifted on POSIX hosts.

## [4.3.0] - 2026-08-16

### Added
- `briefcase.integrity`: tamper-evidence primitives. A spec-versioned hash
  chain (`HashChainAppender`, `HashChainStore` Protocol, `verify_chain_segment`
  with an `expected_prior` window parameter) over append-only records, with
  in-memory and hardened JSONL file stores; deterministic `canonical_json`
  (strict, UTF-8) plus the legacy `canonical_json_compat` profile; and local
  Ed25519 signing (`sign_json`, `verify_json_signature`, `sign_digest`,
  `verify_digest`, `jwk_thumbprint`, RFC 8037 JWKs) behind the new `integrity`
  extra (PyNaCl). Chains and canonical JSON are stdlib-only.

### Changed
- `BitemporalRecord.content_hash` and the examiner bundle's canonical encoding
  now route through `briefcase.integrity.canonical_json_compat`. Byte-identical
  to previous releases (pinned by tests); stored hashes do not change.

## [4.2.0] - 2026-08-16

### Added
- `@briefcase-ai/native`, a Node binding over the Rust cost engine
  (`bindings/node`, napi-rs, cost-only build with no C dependencies):
  `CostCalculator.estimateCost`, `modelIds`, and `normalizeModelId`. One
  package carries the binaries for Linux x64/arm64 (gnu), macOS x64/arm64,
  and Windows x64; the loader picks the matching one at require time.
- The cost engine registers Amazon Nova (premier/pro/lite/micro) and the
  Titan and Cohere embedding models at Bedrock list rates, and
  `normalize_model_id` canonicalizes platform-qualified ids (region and
  vendor prefixes, date stamps, version suffixes); cost estimation applies
  it automatically when the exact id is not registered. Exposed in Python
  as `briefcase._native.normalize_model_id`.
- `briefcase.controls.providers`: a provider registry for LLM failover.
  `ProviderRegistry` holds named providers behind the runtime-checkable
  `LLMProvider` Protocol, iterates them preferred-first with an injectable
  fallback order, and `complete_text` returns the first success or raises
  `NoProviderAvailable` naming the last failure. `scoped_credential` binds a
  per-context credential to one provider; a scoped credential never falls
  through to another provider's platform credential. Concrete provider
  clients stay application-side.
- `briefcase.controls`: an enforcement layer for AI invocations. Ports
  (`QuotaStore`, `EntitlementsHook`, `CacheStore`, `UsageSink`) as
  runtime-checkable Protocols; a `Gateway` composing hard-cap, quota, and
  throttle-cooldown checks around a model call with typed outcomes and an
  explicit port-error policy; `classify_provider_error` unifying provider
  throttle detection (429s are throttled; 503 and ServiceQuotaExceededException
  are transient only and never open cooldowns; message-text matching is
  opt-in); `retry_call`/`retry_call_async` with capped exponential backoff,
  jitter, and a deadline guard; and `FixedWindowQuotaStore`, an in-process
  fail-open limiter.
- `@briefcase.capture` accepts `capture_content="full" | "hash" | "none"`.
  "hash" records SHA-256 digests, character counts, and type names instead of
  content; "none" records shape only; in both, the error field carries the
  exception class name, never the message. A `redact` hook rewrites text in
  "full" mode. The default stays "full".
- `briefcase.semantic_conventions.controls` attribute names for gateway, quota,
  cache, and throttle telemetry.
- `@briefcase-ai/controls`, a TypeScript package (`js/controls`) carrying the
  same controls surface, decision-record wire schema, and exporters; wire-schema
  parity is asserted from a shared fixture by both suites, and the TS suite
  additionally asserts semantic-convention parity against the Python source.
- kdb+ bitemporal backend, AWS Glue Iceberg backend, and the KMS-signed
  examiner bundle move in from the enterprise repository as optional extras
  (`kdb`, `bitemporal-glue`, `compliance-kms`); the open-source versus
  commercial boundary moves to the hosted platform.
- Framework auto-instrumentation ships open: `briefcase.auto.auto()` patches
  LangChain, CrewAI, LlamaIndex, AutoGen, AG2, OpenAI Agents, and PageIndex
  to emit decision records, with `undo()` restoring every patch. The
  `briefcase.auto` stub that pointed at the commercial package is replaced by
  the real dispatcher; each framework is an optional extra with a lazy
  import. Records follow the 10-field wire schema.
- lakeFS branch manager, lineage, and staged commits ship open under the
  existing `lakefs` extra; the `BranchManager` gate that pointed at the
  commercial package is replaced by the implementation.
- RBAC and ABAC guardrail environments (`briefcase.guardrails.envs`) and an
  OPA HTTP router with cached decisions and an internal-router fallback
  (`briefcase.routing.opa`, extra `opa`).
- OTel and GCP Cloud Logging exporters (`briefcase.exporters.otel`,
  `briefcase.exporters.gcp_logging`) on the BaseExporter contract, and Kafka
  and webhook event transports (`briefcase.events.kafka`,
  `briefcase.events.webhook`). The webhook transport follows the SDK's HTTPS
  posture: plain-http non-loopback URLs are rejected, redirects are refused,
  and insecure transport is an explicit opt-in.
- Vector-store adapters for Chroma, Pinecone, and Weaviate
  (`briefcase.rag.vector_stores`) and VCS adapters for DVC, Nessie,
  Pachyderm, ArtiVC, DuckLake, Iceberg, and git-LFS
  (`briefcase.integrations.vcs`), each behind a per-provider extra where a
  client package is needed. The Pinecone adapter targets the current v3
  client API; the Weaviate adapter requires `weaviate-client<4`.

### Changed
- Capture and the controls gateway cost nothing when unconfigured: with no
  exporter resolvable (per-decorator or global), `@capture` calls the wrapped
  function directly, building no record and running neither `repr` nor the
  redact hook.
- Full-mode capture renders through a bounded repr: an oversized argument or
  result costs O(bound) time and memory instead of a full `repr` (measured
  2900x on a 10MB string), rendering as both ends around an ellipsis. Values
  under the bound render identically to `repr`. Hash mode still digests the
  exact full repr.
- Background exports run on one shared daemon worker with a FIFO queue
  instead of one thread per record (measured 6x on the default export path);
  `briefcase._export_mixin.wait_for_pending_exports()` drains it before
  process exit. The fixed-window quota store amortizes its prune scan.
- The controls gateway resolves the global exporter wired by
  `briefcase.observe()` exactly like `@capture`; previously it exported only
  with an explicitly configured exporter.
- The kdb+ backend applies a grouped attribute to `record_id`, turning the
  per-append duplicate check into a hash lookup.
- Cross-language parity for semantic conventions and the gateway outcome
  surface is asserted from shared fixtures by both test suites; CI builds the
  abi3 wheel once and shares it across the Python matrix.

### Security
- `SignedExaminerBundle.verify_signature` requires a caller-pinned
  `expected_key_id` (and algorithm) and never verifies against the key id
  embedded in the bundle: a deserialized bundle carries attacker-controlled
  fields, and verifying against them would accept any bundle self-signed
  with a key the attacker owns.
- `JSONLFileExporter` (both languages) creates parent directories 0700 via
  the mkdir mode, opens files 0600, and tightens a pre-existing file through
  the open descriptor (fchmod), not a re-resolved path.
- The kdb+ backend's q-interpolation allowlist uses fullmatch, closing the
  one-trailing-newline gap `$`-anchored patterns accept.
- The controls gateway catches `Exception`, not `BaseException`:
  cancellation and interpreter-exit exceptions propagate instead of
  converting into outcomes.
- The publish workflow drops to read-only default permissions, gates every
  publish job behind the `release` environment, and publishes npm with OIDC
  trusted publishing and provenance; CI gains cargo-deny advisories (action
  SHA-pinned) and a checksum-pinned gitleaks history scan running without
  persisted credentials; npm installs use the committed lockfile.

### Fixed
- The `cost` module and its root re-export are no longer gated behind the
  `drift` feature; a default or minimal build now exposes `CostCalculator`
  at the crate root.

## [4.1.0] - 2026-08-13

Five surfaces described behavior they did not have. Each is now implemented and
covered by a test that was watched failing against the old code.

### Added
- `ReplayEngine.with_executor(fn)` (Python and Rust) makes a replay re-execute.
  `fn` receives the recorded inputs as a dict and returns the current build's
  outputs, either as a dict of names to values or a single value recorded as
  `result`. `outputs_match` and every policy rule now compare a fresh answer
  against the recorded one.
- `DecisionSnapshot.content_hash()` hashes what was decided: inputs, outputs,
  model parameters and their values, module, tags, and any error. Ids,
  timestamps, and execution time are excluded so a verifier holding the record
  can recompute it. Map keys are sorted, so construction order does not change
  the digest.
- `StorageBackend::save_decisions` writes many decisions in one round trip.
  `SqliteBackend` commits them in a single transaction.
- `BufferedBackend` gains `flush()`, `pending()`, the read methods, and the
  context-manager protocol in Python.

### Changed
- `BufferedBackend` batches. Decisions are held until `buffer_size` accumulate,
  then committed together; previously every `save_decision` waited for its own
  write, so `buffer_size` bounded queued writes rather than grouping them.
  `save_decision` still returns a usable id immediately and `load_decision`
  finds a decision that is still buffered. Pending decisions live in memory, so
  use the context manager or call `flush()`; dropping a backend with unflushed
  decisions warns on stderr.
- `TimeoutWrapper` enforces its deadline. Evaluation runs on a worker thread and
  the wrapper stops waiting at `max_ms`; a guardrail blocking 5s under a 50ms
  deadline now returns `DENY` in about 55ms rather than after 5s. Python cannot
  cancel the call, so the guardrail keeps running and its answer is discarded.
  Costs about 45us per evaluation.
- A replay with no executor reports status `"pending"` with `outputs_match`
  `False`, where it previously reported `"success"` with `outputs_match` `True`.
  Nothing was compared, and "not checked" must not read as "verified".
- Replay policy rules are evaluated. `with_exact_match` compares the recorded
  and replayed values; `with_similarity_threshold` scores them by normalized
  Levenshtein distance. Both previously passed unconditionally, and a similarity
  rule only reported a violation when its threshold was at or below 0.5, with a
  constant as the actual value. Rules on a replay that did not re-execute report
  `actual: "not replayed"` rather than passing.
- Replay policy fields resolve against named outputs. `with_exact_match("category")`
  now finds the output called `category`; field lookup previously recognized only
  `function_name`, `execution_time_ms`, and `output`, so any other rule silently
  matched nothing.
- `BufferedBackend.flush()` reports the decisions that call wrote.
  `SqliteBackend.flush()` returns the table's whole row count, which was being
  added to it.

### Note on `fingerprint()`
`fingerprint()` is unchanged and still hashes only the function name, inputs, and
model name, so stored fingerprints stay valid. It identifies the question, not the
answer; documentation that called it tamper-evident was wrong. Use
`content_hash()` when a changed output has to be detectable.

## [4.0.1] - 2026-08-12

### Fixed
- The `mcp` extra pins `mcp>=1.2,<2`. mcp 2.0 removed `mcp.server.fastmcp`,
  which `briefcase.mcp` is built on, so a fresh `pip install briefcase-ai[mcp]`
  resolved 2.x and broke the extra. The import guard now distinguishes "mcp is
  not installed" from "mcp is installed but too new", naming the version it
  found; it previously told you to install a package you already had. Found by
  running the documentation's own snippets against the published 4.0.0 wheel.

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
- CI-only failures fixed after the gates ran for real: two clippy lints that
  only a newer toolchain reports (`for_kv_map`, `unnecessary_sort_by`), a test
  using `EncodingWarning` which does not exist before 3.10, a test parsing the
  zstd fixture without a backend installed, and a lakeFS test importing an
  extra CI does not install.
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
