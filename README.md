# Briefcase AI SDK

![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)

Open-source decision tracking for AI.

## Install

```bash
pip install briefcase-ai
```

## Quick Example

```python
from briefcase import capture

@capture(decision_type="classify_text")
def classify(text: str) -> str:
    return text.upper()

result = classify("hello world")
print(result)
```

`capture` works immediately — no `briefcase.init()` required. Call `init()` only
when you use the native runtime features (storage backends, snapshots).

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
| `routing` | Router protocol, agent router, versioned policy registry |
| `lakefs` | lakeFS versioned storage client |
| `vcs` | VCS client base protocol |
| `bitemporal` | Bitemporal evidence store, as-of views, append-only corrections |
| `bitemporal-iceberg` | pyiceberg-backed bitemporal store (any supported catalog) |
| `compliance` | Examiner bundles joining decision, evidence, and policy version |
| `dev` | Dev tooling: pytest, black, mypy, flake8 |
| `all` | Installs every optional extra listed above |

Most features are native- or pure-Python-backed and ship with the base package —
their extras (`replay`, `drift`, `sanitize`, `storage`, `routing`, `bitemporal`,
`compliance`, …) are convenience groupings that pull in **no** additional
dependencies. Only `otel`, `lakefs`, and `bitemporal-iceberg` install third-party
packages.

## Enterprise features

The OSS SDK ships everything needed to run the end-to-end walkthrough, persist evidence to SQLite or Iceberg, and pass an internal audit. The following features require the commercial [`briefcase-ai-sdk-enterprise`](https://github.com/briefcasebrain/briefcase-ai-sdk-enterprise) package:

| Feature | OSS | Enterprise |
| --- | --- | --- |
| In-memory, SQLite, pyiceberg backends | ✓ | |
| kdb+ backend (`pykx` client) | stub only | ✓ |
| Managed-catalog connectors (Glue, Snowflake Horizon, Databricks Unity, Confluent Tableflow) | | ✓ |
| Licensed market data ingest (Bloomberg BPIPE, Refinitiv, ICE) | | ✓ |
| Signed content-hash envelopes (AWS KMS, GCP KMS, YubiHSM) | | ✓ |
| WORM retention integration (S3 Object Lock, Azure Blob immutable, MinIO) | | ✓ |
| Multi-tenant `PolicyRegistry` with RBAC and approvals | | ✓ |
| Cross-region evidence replication, DR runbooks | | ✓ |
| Regulator-format bundle exporters (SEC SDR, FCA, OCC, FINRA) | | ✓ |
| SOC 2 Type II / FedRAMP attestations, 24/7 SLA support | | ✓ |

Contact sales@briefcasebrain.com for enterprise access.

## Telemetry

The SDK can report anonymous usage metrics (SDK version, OS, architecture, backend
type) to help prioritize development. **No personal data or decision content is ever
collected.** Telemetry is only transmitted when a collection endpoint is configured
via `BRIEFCASE_API_URL` (it defaults to `localhost`, i.e. a no-op), and you can
disable it entirely at any time:

```bash
export BRIEFCASE_TELEMETRY=0   # also accepts: false, no, off (case-insensitive)
```

## Links

- Docs: [briefcaseai.io](https://briefcaseai.io)
- GitHub: [github.com/briefcasebrain/briefcase-ai-sdk](https://github.com/briefcasebrain/briefcase-ai-sdk)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
