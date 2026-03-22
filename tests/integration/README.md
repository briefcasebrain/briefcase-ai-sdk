# Integration Tests

These tests require a clean OpenTelemetry environment and should be run in isolation.

## Running Integration Tests

```bash
# Run in separate process to ensure clean OTel environment
pytest tests/integration/ --forked

# Or skip integration tests in regular runs
pytest tests/ -m "not integration"
```

## Requirements

- OpenTelemetry SDK installed (`pip install briefcase-ai[otel]`)
- Clean tracer provider (no existing spans)
- Isolated test environment

## Note

Integration tests are marked with `@pytest.mark.integration` and may fail
if run alongside other tests that initialize OpenTelemetry providers.

For CI/CD, run these tests in a separate job or use pytest-forked.
