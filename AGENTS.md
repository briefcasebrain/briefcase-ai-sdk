# AGENTS.md

Guidance for AI coding agents (Cursor, Claude Code, Codex, Replit, etc.). Two
parts: how to **use** briefcase-ai in application code, and how to **work on**
this repository.

## Using briefcase-ai in your code

The canonical pattern — wire an exporter once, then decorate functions:

```python
import briefcase
briefcase.observe("console")          # "console" | "memory" | "runs.jsonl" | "https://…" | ExporterInstance

@briefcase.capture
def classify(text: str) -> str:
    return text.upper()
```

Rules to follow when generating briefcase code:
- Always call `briefcase.observe(...)` (or `briefcase.setup(exporter=...)`) once at
  startup. Without it, `@capture` records nothing.
- `@briefcase.capture` needs no `briefcase.init()`. It works on sync and async
  functions and on (async) generators, with or without arguments. A generator
  emits one record when it finishes, holding the collected chunks.
- Export runs on a bounded background queue. An atexit hook drains it for up
  to 5 s at a normal exit (also `sys.exit()`), so scripts need no explicit
  flush; call `briefcase.shutdown()` (or `briefcase.flush()`) only before
  `os._exit`, in prefork workers, or to drain earlier.
  `briefcase.export_stats()` reports dropped/failed counts.
- `import briefcase` works without the compiled core; only native-backed names
  (`briefcase.init`, `DecisionSnapshot`, `briefcase.replay`, …) need it.
- Content rule: briefcase records prompt/completion/tool content only where
  someone chose to. `briefcase.auto()` (frameworks and provider SDKs),
  `briefcase-ai exec`, `connect`/the receiver and eval records record none unless
  given `capture_content="full"` (or `"hash"`; `True`/`False` also work),
  `--capture-content` / `--keep-content`, or `BRIEFCASE_CAPTURE_CONTENT=full`.
  `@capture` on your own function is that choice and records its arguments and
  return value by default (`capture_content="hash"`/`"none"` to keep them out).
- Never log or persist raw PII. Sanitize first:
  `from briefcase.sanitize import Sanitizer; Sanitizer().sanitize(text).sanitized`.
  For external-data snapshots, pass `ExternalDataTracker(sanitizer=Sanitizer())`.
- In tests, use `@briefcase.capture(async_capture=False)` for deterministic,
  inline export, and `briefcase.observe("memory")` to inspect records.
- Logging is opt-in: `briefcase.enable_logging("DEBUG")` or `BRIEFCASE_LOG_LEVEL`.
- Gate model calls with `briefcase.controls`: `Gateway` composes hard-cap,
  quota, and throttle-cooldown checks; `retry_call` adds capped backoff. The
  same surface ships for TypeScript as `@briefcase-ai/controls`.
- Most features need no pip extra. These extras are empty groupings that
  install nothing: `replay`, `drift`, `sanitize`, `storage`, `validate`,
  `guardrails`, `rag`, `correlation`, `external`, `events`, `webhook`,
  `routing`, `vcs`, `bitemporal`, `compliance`, and `controls`. These install
  third-party packages: `otel`, `kafka`, `gcp-logging`, `opa`, `lakefs`,
  `rag-chroma`, `rag-pinecone`, `rag-weaviate`, `vcs-dvc`, `vcs-pachyderm`,
  `vcs-ducklake`, `vcs-iceberg`, `gym`, `evals`, `bitemporal-iceberg`,
  `bitemporal-glue`, `kdb`, `compliance-kms`, `integrity`, `langchain`,
  `crewai`, `llamaindex`, `autogen`, `ag2`, `pageindex`, `openai-agents`,
  `mcp`, `run`, `engine`, `receiver`, `budgets-redis`, `budgets-postgres`, and `budgets-mysql` (`all` is every one of them except `kdb`; `dev` is the test
  tooling). See `llms-full.txt` for the extras map and recipes.

### Plug-and-play integrations

To capture what coding tools themselves do (Claude Code, Codex, Cursor, VS Code
Copilot), do not hand-write their config. Run:

```bash
briefcase-ai connect            # writes each detected tool's telemetry + MCP settings (diff, one confirm)
briefcase-ai receive --detach   # local OTLP receiver on 127.0.0.1:4318 -> decision records
briefcase-ai connect --undo     # removes exactly what connect wrote
```

- `connect` never enables prompt or tool content unless given `--capture-content`,
  and the receiver redacts content, e-mail and host paths unless given `--keep-content`.
- Codex and VS Code Copilot send OTLP/protobuf: `pip install 'briefcase-ai[receiver]'`. Copilot CLI sends OTLP/JSON and works on a bare install.
- `briefcase-ai mcp install` / `briefcase-ai mcp link <tool>` set up the `briefcase-ai-mcp` server.
- Details: `docs/integrations/README.md`.

More: `README.md`, `llms.txt`, `llms-full.txt`, and `examples/`.

## Working on this repository

Monorepo: Python package `briefcase/` (published as `briefcase-ai`) backed by a
Rust core in `crates/briefcase-core/` via PyO3 bindings in `bindings/python/`,
plus the TypeScript package `js/controls/` (published as
`@briefcase-ai/controls`).

Build and test:

```bash
maturin develop                      # build + install the native extension
pytest tests/                        # Python facade tests (mock briefcase._native)
pytest bindings/python/tests/        # native binding tests (real extension)
python scripts/check_imports.py      # smoke-test that every submodule imports
cargo fmt --all --check
cargo test -p briefcase-core --all-features
cargo clippy -p briefcase-core --all-features -- -D warnings
cargo test -p briefcase-python --locked          # bindings crate unit tests
flake8 briefcase/ scripts/ tests/ bindings/python/tests/ examples/
mypy briefcase/
cd js/controls && npm install && npm run typecheck && npm test && npm run build
```

Conventions:
- The facade tests under `tests/` mock `briefcase._native` (see
  `tests/mock_core.py`); the real native classes are exercised only by
  `bindings/python/tests/` — run both.
- `--all-features` matters for Rust: `sanitize` is not a default feature, so a
  narrower `cargo test` compiles out every PII-redaction test.
- All modules log via `from briefcase._logging import get_logger`.
- Optional OpenTelemetry is imported once through `briefcase/_otel.py`.
- Keep version strings in sync with `python scripts/version_sync.py set --version X.Y.Z`
  (`--dry-run` lists every target it would touch; `check` gates CI).
- `briefcase-core` is a path dependency of the bindings crate, so a manifest
  change can leave the sdist unbuildable while the in-repo build still works.
  CI builds a wheel from the sdist; reproduce with
  `maturin sdist --out dist && tar xzf dist/*.tar.gz -C dist && (cd dist/briefcase_ai-*/ && maturin build --release)`.
- `pyo3/extension-module` is applied by maturin (`[tool.maturin] features`), not
  in `Cargo.toml`. Enabling it in the manifest makes every Rust test binary
  unlinkable, since it leaves Python symbols to the host interpreter.
- `briefcase-ai connect` formats live in `briefcase/connect/adapters.json` with the docs URL and
  date each was checked. A format that is not confirmed against the tool's documentation must
  stay print-only (a `Manual` plan item), never a write. Connect tests run in a temp `HOME`.
- Do not commit secrets; do not add internal notes under `docs/design/` (gitignored).
