# briefcase-ai usage (append to your project's CLAUDE.md)

When instrumenting code with briefcase-ai:

```python
import briefcase
briefcase.observe("console")   # "console" | "memory" | "runs.jsonl" | ExporterInstance

@briefcase.capture
def classify(text: str) -> str:
    return text.upper()
```

- Call `briefcase.observe(...)` / `briefcase.setup(exporter=...)` once at startup,
  or `@capture` records nothing.
- `@briefcase.capture` needs no `briefcase.init()`; works on sync and async defs.
- Sanitize PII first: `from briefcase.sanitize import Sanitizer; Sanitizer().sanitize(text).sanitized`.
- Deterministic tests: `@briefcase.capture(async_capture=False)` and
  `briefcase.observe("memory")` (inspect `exporter.records`).
- Logging is opt-in: `briefcase.enable_logging("DEBUG")` or `BRIEFCASE_LOG_LEVEL`.
- Most features need no pip extra; only `otel`, `lakefs`, `bitemporal-iceberg`,
  `gym`, `evals`, `mcp` do. Full recipes: the package's `llms-full.txt`.
