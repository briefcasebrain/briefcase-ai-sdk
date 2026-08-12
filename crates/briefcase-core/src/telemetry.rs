use crate::DEFAULT_API_URL;
use once_cell::sync::Lazy;
use opentelemetry::{
    global,
    trace::{Span, Tracer},
    KeyValue,
};
use serde::Serialize;
use std::env;
use std::sync::Arc;
use tokio::sync::mpsc;

fn get_telemetry_url() -> String {
    let base = env::var("BRIEFCASE_API_URL").unwrap_or_else(|_| DEFAULT_API_URL.to_string());
    format!("{}/v1/telemetry/usage", base.trim_end_matches('/'))
}

/// Returns true when telemetry has been disabled via the `BRIEFCASE_TELEMETRY`
/// environment variable. Accepts `0`, `false`, `no`, `off` (case-insensitive,
/// surrounding whitespace ignored) so the documented opt-out is forgiving.
pub fn telemetry_disabled() -> bool {
    match env::var("BRIEFCASE_TELEMETRY") {
        Ok(v) => matches!(
            v.trim().to_ascii_lowercase().as_str(),
            "0" | "false" | "no" | "off"
        ),
        Err(_) => false,
    }
}

#[derive(Debug, Serialize)]
pub struct AnonymousMetrics {
    pub sdk_version: String,
    pub os: String,
    pub arch: String,
    pub backend_type: String,
    pub mode: String,
    pub session_hash: String,
}

pub struct TelemetryEmitter {
    sender: mpsc::Sender<AnonymousMetrics>,
}

fn record_usage_span(metrics: &AnonymousMetrics) {
    let tracer = global::tracer("briefcase-core");
    let mut span = tracer.start("briefcase.telemetry.enqueue");
    span.set_attribute(KeyValue::new(
        "briefcase.sdk_version",
        metrics.sdk_version.clone(),
    ));
    span.set_attribute(KeyValue::new("briefcase.os", metrics.os.clone()));
    span.set_attribute(KeyValue::new("briefcase.arch", metrics.arch.clone()));
    span.set_attribute(KeyValue::new(
        "briefcase.backend",
        metrics.backend_type.clone(),
    ));
    span.set_attribute(KeyValue::new("briefcase.mode", metrics.mode.clone()));
    span.end();
}

impl Default for TelemetryEmitter {
    fn default() -> Self {
        Self::new()
    }
}

impl TelemetryEmitter {
    pub fn new() -> Self {
        let (tx, mut rx) = mpsc::channel(10);

        // Background thread for non-blocking emission
        std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();

            rt.block_on(async {
                let client = reqwest::Client::new();
                let url = get_telemetry_url();
                while let Some(metrics) = rx.recv().await {
                    let _ = client.post(&url).json(&metrics).send().await;
                }
            });
        });

        Self { sender: tx }
    }

    pub fn emit(&self, metrics: AnonymousMetrics) {
        if telemetry_disabled() {
            return;
        }
        record_usage_span(&metrics);
        let _ = self.sender.try_send(metrics);
    }
}

// NOTE: `GLOBAL_EMITTER` currently has no callers — telemetry is wired but not
// invoked anywhere, so the SDK does not emit usage metrics today. Enabling it
// (e.g. from `init()`) is a deliberate behavior change: it begins network
// egress and must (a) be gated on `telemetry_disabled()` *before* the emitter
// is constructed, and (b) target an HTTPS endpoint via `BRIEFCASE_API_URL`.
pub static GLOBAL_EMITTER: Lazy<Arc<TelemetryEmitter>> =
    Lazy::new(|| Arc::new(TelemetryEmitter::new()));
