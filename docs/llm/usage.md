# briefcase-ai — condensed usage

The full, canonical recipe book is [`llms-full.txt`](../../llms-full.txt). This is
the short version.

## Instrument a function

```python
import briefcase
briefcase.observe("console")      # console | memory | runs.jsonl | ExporterInstance

@briefcase.capture
def classify(text: str) -> str:
    return text.upper()
```

## Recipes (see llms-full.txt for full snippets)

| Task | Entry point |
| --- | --- |
| Capture decisions | `briefcase.capture` (+ `briefcase.observe`) |
| Pick where records go | `briefcase.exporters.{Console,JSONLFile,Memory}Exporter` |
| Custom export target | subclass `briefcase.exporters.BaseExporter` |
| Redact PII | `briefcase.sanitize.Sanitizer().sanitize(text).sanitized` |
| Track external data (PII-safe) | `briefcase.external_data.ExternalDataTracker(sanitizer=...)` |
| Estimate model cost | `briefcase.cost.CostCalculator().estimate_cost(...)` |
| Detect output drift | `briefcase.drift.DriftCalculator().calculate_drift(...)` |
| Turn on logging | `briefcase.enable_logging("DEBUG")` / `BRIEFCASE_LOG_LEVEL` |

## Gotchas
- No `observe(...)`/`setup(...)` → records go nowhere.
- Deterministic tests → `@briefcase.capture(async_capture=False)` + `observe("memory")`.
- `briefcase.otel` needs `pip install briefcase-ai[otel]`.
