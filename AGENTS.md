# AGENTS.md

Guidance for AI coding agents (Cursor, Claude Code, Codex, Replit, etc.). Two
parts: how to **use** briefcase-ai in application code, and how to **work on**
this repository.

## Using briefcase-ai in your code

The canonical pattern — wire an exporter once, then decorate functions:

```python
import briefcase
briefcase.observe("console")          # "console" | "memory" | "runs.jsonl" | ExporterInstance

@briefcase.capture
def classify(text: str) -> str:
    return text.upper()
```

Rules to follow when generating briefcase code:
- Always call `briefcase.observe(...)` (or `briefcase.setup(exporter=...)`) once at
  startup. Without it, `@capture` records nothing.
- `@briefcase.capture` needs no `briefcase.init()`. It works on sync and async
  functions, with or without arguments.
- Never log or persist raw PII. Sanitize first:
  `from briefcase.sanitize import Sanitizer; Sanitizer().sanitize(text).sanitized`.
  For external-data snapshots, pass `ExternalDataTracker(sanitizer=Sanitizer())`.
- In tests, use `@briefcase.capture(async_capture=False)` for deterministic,
  inline export, and `briefcase.observe("memory")` to inspect records.
- Logging is opt-in: `briefcase.enable_logging("DEBUG")` or `BRIEFCASE_LOG_LEVEL`.
- Most features need no pip extra; only `otel`, `lakefs`, `bitemporal-iceberg`,
  and `mcp` do. See `llms-full.txt` for the full extras map and recipes.

More: `README.md`, `llms.txt`, `llms-full.txt`, and `examples/`.

## Working on this repository

Monorepo: Python package `briefcase/` (published as `briefcase-ai`) backed by a
Rust core in `crates/briefcase-core/` via PyO3 bindings in `bindings/python/`.

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
  CI now builds a wheel from the sdist; reproduce with
  `maturin sdist --out dist && tar xzf dist/*.tar.gz -C dist && (cd dist/briefcase_ai-*/ && maturin build --release)`.
- `pyo3/extension-module` is applied by maturin (`[tool.maturin] features`), not
  in `Cargo.toml`. Enabling it in the manifest makes every Rust test binary
  unlinkable, since it leaves Python symbols to the host interpreter.
- Do not commit secrets; do not add internal notes under `docs/design/` (gitignored).
