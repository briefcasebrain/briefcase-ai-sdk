# Briefcase AI SDK

![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)

Governance infrastructure for AI decisions: enforce controls, record context,
replay later.

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
[`examples/eval_runs/`](https://github.com/briefcasebrain/briefcase-ai-sdk/tree/main/examples/eval_runs/).

```python
from briefcase.integrations.gym import GuardrailGymEnv, capture_episodes

env = GuardrailGymEnv(guardrail, tasks, injections)  # a guardrail as a gym.Env
env = capture_episodes(env)                          # rl.step / rl.episode records
```

Needs `pip install briefcase-ai[gym]`. See [`examples/rl_gym/`](https://github.com/briefcasebrain/briefcase-ai-sdk/tree/main/examples/rl_gym/).

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

This repo ships machine-readable usage guidance: [`llms.txt`](https://github.com/briefcasebrain/briefcase-ai-sdk/blob/main/llms.txt) /
[`llms-full.txt`](https://github.com/briefcasebrain/briefcase-ai-sdk/blob/main/llms-full.txt), an [`AGENTS.md`](https://github.com/briefcasebrain/briefcase-ai-sdk/blob/main/AGENTS.md), and copy-paste
editor rules under [`docs/llm/`](https://github.com/briefcasebrain/briefcase-ai-sdk/tree/main/docs/llm/). An MCP server is available via
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
| `rag-chroma` / `rag-pinecone` / `rag-weaviate` | Vector-store adapters (each adds its client) |
| `correlation` | Workflow and trace correlation helpers |
| `external` | External data snapshot tracking |
| `events` | Structured event type and emitter interface |
| `kafka` / `webhook` / `gcp-logging` | Event transports and the Cloud Logging exporter (webhook is stdlib) |
| `routing` | Router protocol, agent router, versioned policy registry |
| `opa` | OPA HTTP router with cached decisions and internal-router fallback (adds `httpx`) |
| `lakefs` | lakeFS versioned storage client, branch manager, lineage, staged commits |
| `vcs` | VCS client base protocol plus DVC, Nessie, Pachyderm, ArtiVC, DuckLake, Iceberg, and git-LFS adapters |
| `vcs-dvc` / `vcs-pachyderm` / `vcs-ducklake` / `vcs-iceberg` | Per-provider VCS clients (the rest are HTTP/subprocess based) |
| `gym` | Gymnasium bridge: guardrail env adapter and RL episode capture |
| `evals` | Eval-harness bridge: `EvalRun` logger, inspect-ai / lm-eval parsers (adds `zstandard` for `.eval` archives on Python < 3.14) |
| `bitemporal` | Bitemporal evidence store, as-of views, append-only corrections |
| `bitemporal-iceberg` | pyiceberg-backed bitemporal store (any supported catalog) |
| `bitemporal-glue` | AWS Glue catalog auth for the Iceberg backend (adds `boto3`) |
| `kdb` | kdb+ bitemporal backend (adds KX-licensed `pykx`; excluded from `all`) |
| `compliance` | Examiner bundles joining decision, evidence, and policy version |
| `compliance-kms` | KMS-signed examiner bundles against your own AWS KMS key (adds `boto3`) |
| `controls` | Gateway, quota, throttle classification, and retry (no extra dependencies) |
| `integrity` | Tamper-evident hash chains, canonical JSON, Ed25519 signing over digests and JSON manifests (signing adds `pynacl`) |
| `langchain` / `crewai` / `llamaindex` / `autogen` / `ag2` / `pageindex` / `openai-agents` | Framework auto-instrumentation via `briefcase.auto` (each adds its framework) |
| `mcp` | MCP server (`briefcase-mcp`) exposing the SDK to AI agents |
| `dev` | Dev tooling: pytest, mypy, flake8, moto |
| `all` | Installs every optional extra listed above except `kdb` |

Most features are native- or pure-Python-backed and ship with the base package —
their extras (`replay`, `drift`, `sanitize`, `storage`, `routing`, `bitemporal`,
`compliance`, …) are convenience groupings that pull in **no** additional
dependencies. The extras that install third-party packages are `otel`,
`lakefs`, `bitemporal-iceberg`, `bitemporal-glue`, `kdb`, `compliance-kms`,
`gym`, `evals`, `mcp`, `opa`, `kafka`, `gcp-logging`, `integrity`, the `rag-*` and `vcs-*`
store adapters, and the framework auto-instrumentation extras.

## Managed platform

Every library feature runs against infrastructure you own: storage
backends (in-memory, SQLite, pyiceberg, Glue-authenticated Iceberg, kdb+),
KMS-signed examiner bundles, guardrails, RBAC/ABAC/OPA policy evaluation,
routing, replay, framework auto-instrumentation (`briefcase.auto`), lakeFS
branching and lineage, vector-store and VCS adapters, event transports, and
the controls layer, in both Python and TypeScript
(`@briefcase-ai/controls`).

The commercial offering is the hosted platform, not gated code: a managed
control plane, catalog provisioning and credential brokering, operational
runbooks (DR failover, catalog migration, key rotation), certified retention
and regulator attestations, licensed market-data ingest (Bloomberg BPIPE,
Refinitiv, ICE), and SOC 2 / FedRAMP posture with SLA support.

Contact sales@briefcasebrain.com for the managed platform.

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
- Contributing: [CONTRIBUTING.md](https://github.com/briefcasebrain/briefcase-ai-sdk/blob/main/CONTRIBUTING.md)
