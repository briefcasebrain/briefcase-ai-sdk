# Briefcase AI SDK

![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)

Open-source decision tracking for AI.

## Install

```bash
pip install briefcase-ai
```

## Quick Example

```python
import briefcase

briefcase.observe("console")          # send records to stderr (or "memory" / "runs.jsonl")

@briefcase.capture(decision_type="classify_text")
def classify(text: str) -> str:
    return text.upper()

classify("hello world")               # a decision record is emitted
```

`capture` works immediately — no `briefcase.init()` required. Call `init()` only
when you use the native runtime features (storage backends, snapshots). Without a
call to `briefcase.observe(...)` (or `briefcase.setup(exporter=...)`), `@capture`
records decisions but has nowhere to send them.

**Exporters.** `briefcase.observe(...)` accepts `"console"`, `"memory"` (records
collected on `exporter.records`), a `"*.jsonl"` path, or any
`briefcase.exporters.BaseExporter` instance — subclass `BaseExporter` to ship
records to your own backend.

## Evaluations and RL

Two bridges emit decision records from work that is not a single function call.
Both use the exporter you already configured.

```python
from briefcase.integrations.evals import EvalRun, from_inspect_log, replay

with EvalRun("gsm8k", model="claude-opus-5") as run:   # one eval.case per case,
    run.log_case("q1", inputs=q, outputs=a, passed=a == target)   # one eval.run
print(run.summary()["pass_rate"])

replay(from_inspect_log("logs/2026-08-12_gsm8k.eval"))  # inspect-ai .json/.eval
```

The parsers are stdlib only and never import the eval framework, so a log can be
replayed on a machine that has neither installed. See
[`examples/eval_runs/`](examples/eval_runs/).

```python
from briefcase.integrations.gym import GuardrailGymEnv, capture_episodes

env = GuardrailGymEnv(guardrail, tasks, injections)  # a guardrail as a gym.Env
env = capture_episodes(env)                          # rl.step / rl.episode records
```

Needs `pip install briefcase-ai[gym]`. See [`examples/rl_gym/`](examples/rl_gym/).

`capture_episodes` exports on a background thread by default, since step capture
is on the hot path. A script that exits right after `close()` can lose its
records; pass `capture_episodes(env, async_capture=False)` in short runs.
`EvalRun` already defaults to `async_capture=False` for that reason.

## Logging

The library is silent by default (it installs only a `NullHandler`). Turn on
visible logs explicitly:

```python
import briefcase
briefcase.enable_logging("DEBUG")     # or set BRIEFCASE_LOG_LEVEL=DEBUG
```

## Using with AI tools (Cursor, Claude Code, Codex, …)

This repo ships machine-readable usage guidance: [`llms.txt`](llms.txt) /
[`llms-full.txt`](llms-full.txt), an [`AGENTS.md`](AGENTS.md), and copy-paste
editor rules under [`docs/llm/`](docs/llm/). An MCP server is available via
`pip install briefcase-ai[mcp]` then `briefcase-mcp`.

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
| `gym` | Gymnasium bridge: guardrail env adapter and RL episode capture |
| `evals` | Eval-harness bridge: `EvalRun` logger, inspect-ai / lm-eval parsers (adds `zstandard` for `.eval` archives on Python < 3.14) |
| `bitemporal` | Bitemporal evidence store, as-of views, append-only corrections |
| `bitemporal-iceberg` | pyiceberg-backed bitemporal store (any supported catalog) |
| `compliance` | Examiner bundles joining decision, evidence, and policy version |
| `mcp` | MCP server (`briefcase-mcp`) exposing the SDK to AI agents |
| `dev` | Dev tooling: pytest, black, mypy, flake8 |
| `all` | Installs every optional extra listed above |

Most features are native- or pure-Python-backed and ship with the base package —
their extras (`replay`, `drift`, `sanitize`, `storage`, `routing`, `bitemporal`,
`compliance`, …) are convenience groupings that pull in **no** additional
dependencies. Only `otel`, `lakefs`, `bitemporal-iceberg`, `gym`, `evals`, and
`mcp` install third-party packages.

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
