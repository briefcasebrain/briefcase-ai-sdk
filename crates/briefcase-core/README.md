# Briefcase Core

The core Rust library for Briefcase AI — high-performance AI observability, replay, and decision tracking.

[![Crates.io](https://img.shields.io/crates/v/briefcase-core.svg)](https://crates.io/crates/briefcase-core)
[![Documentation](https://docs.rs/briefcase-core/badge.svg)](https://docs.rs/briefcase-core)

## Features

- **AI Decision Tracking** — Capture inputs, outputs, and context for every AI decision
- **Deterministic Replay** — Reproduce AI decisions exactly with full context preservation
- **Drift Detection** — Monitor model performance and behavior changes
- **Cost Calculation** — Track and estimate model usage costs
- **Data Sanitization** — Built-in PII redaction with regex patterns
- **Token Counting** — Estimate token usage via tiktoken
- **Flexible Storage** — SQLite backend with VCS provider protocol for custom backends

## Installation

```toml
[dependencies]
briefcase-core = "3.0"
```

## Quick Start

```rust
use briefcase_core::*;
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let decision = DecisionSnapshot::new("ai_function")
        .add_input(Input::new("user_query", json!("Hello world"), "string"))
        .add_output(Output::new("response", json!("Hello back!"), "string").with_confidence(0.95))
        .with_execution_time(120.5);

    let storage = storage::SqliteBackend::in_memory()?;
    let decision_id = storage.save_decision(decision).await?;

    println!("Saved decision: {}", decision_id);
    Ok(())
}
```

## Feature Flags

| Feature | Default | Description |
|---------|---------|-------------|
| `recording` | Yes | Baseline decision capture |
| `async` | Yes | Tokio async runtime support |
| `storage` | Yes | SQLite storage backend |
| `replay` | No | Deterministic replay engine |
| `drift` | No | Drift detection + cost calculation |
| `sanitize` | No | PII sanitization |
| `otel` | No | OpenTelemetry instrumentation |
| `tokens` | No | Token counting (tiktoken) |
| `networking` | No | HTTP client (reqwest) |
| `compression` | No | zstd + flate2 compression |
| `vcs-storage` | No | VCS provider protocol |

## Storage

```rust
// In-memory
let storage = storage::SqliteBackend::in_memory()?;

// File-based
let storage = storage::SqliteBackend::new("decisions.db")?;
```

### Custom Storage

Implement the `StorageBackend` trait:

```rust
use briefcase_core::storage::StorageBackend;
use async_trait::async_trait;

pub struct CustomBackend;

#[async_trait]
impl StorageBackend for CustomBackend {
    // implement required methods
}
```

### Custom VCS Provider

Implement the `VcsProvider` trait for version-controlled storage:

```rust
use briefcase_core::storage::vcs::{VcsProvider, VcsStorageBackend};

// Implement VcsProvider, then wrap:
let backend = VcsStorageBackend::new(my_provider);
```

## License

Apache-2.0 — see LICENSE file for details.
