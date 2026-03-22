# Briefcase AI SDK

![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)

Open-source decision tracking for AI.

## Install

```bash
pip install briefcase-ai
```

## Quick Example

```python
from briefcase.decorators import capture

@capture(decision_type="classify_text")
def classify(text: str) -> str:
    return text.upper()

result = classify("hello world")
print(result)
```

## Extras

| Extra | Description |
| --- | --- |
| `replay` | Deterministic replay engine for recorded decisions |
| `drift` | Drift scoring and cost calculation for model outputs |
| `sanitize` | Built-in PII redaction utilities |
| `otel` | OpenTelemetry helpers |
| `storage` | Rust-backed SQLite storage engine |
| `validate` | Prompt validation engine |
| `guardrails` | Guardrail framework, wrappers, and registries |
| `rag` | Versioned embedding pipeline and instrumented retrieval |
| `correlation` | Workflow and trace correlation helpers |
| `external` | External data snapshot tracking |
| `events` | Structured event type and emitter interface |
| `routing` | Router protocol for auto vs. human review |
| `lakefs` | lakeFS versioned storage client |
| `vcs` | VCS client base protocol |
| `dev` | Dev tooling: pytest, black, mypy, flake8 |
| `all` | Installs every optional extra listed above |

Pre-built implementations (framework integrations, vector store adapters, specific routers and exporters) are available in [briefcase-ai-enterprise](https://github.com/briefcasebrain/briefcase-ai-sdk-enterprise).

## Telemetry

When the `otel` extra is enabled, the SDK sends anonymous usage metrics (SDK version, OS, architecture, backend type). No personal data or decision content is collected. To opt out:

```bash
export BRIEFCASE_TELEMETRY=0
```

## Links

- Docs: [briefcaseai.io](https://briefcaseai.io)
- GitHub: [github.com/briefcasebrain/briefcase-ai-sdk](https://github.com/briefcasebrain/briefcase-ai-sdk)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
