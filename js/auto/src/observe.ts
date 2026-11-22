/**
 * `observe()`: choose where records go, with the same shorthands and the
 * same environment variables, in the same order, as Python's
 * `briefcase.observe()` (`briefcase/_observe.py`):
 *
 * 1. `BRIEFCASE_EXPORTER`: `console`, `memory`, a path ending in `.jsonl`,
 *    an `http(s)://` URL (sent with `BRIEFCASE_EXPORTER_TOKEN` as a bearer
 *    token when set), or `otlp` / `otel` for the OpenTelemetry variables.
 * 2. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (used as is) or
 *    `OTEL_EXPORTER_OTLP_ENDPOINT` (`/v1/traces` appended), with
 *    `OTEL_EXPORTER_OTLP_(TRACES_)HEADERS` and `OTEL_SERVICE_NAME`: OTLP/HTTP JSON.
 * 3. The console (JSON lines on stderr).
 *
 * Difference from Python: this package speaks OTLP over HTTP with the JSON
 * encoding only. `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` falls back to the
 * console with a warning; `http/protobuf` is sent as `http/json` to the
 * same URL, with a warning.
 */

import { BroadcastChannel, getEnvironmentData, isMainThread, setEnvironmentData } from "node:worker_threads";

import {
  ConsoleExporter,
  type Exporter,
  HttpExporter,
  JsonlFileExporter,
  MemoryExporter,
  OtlpHttpExporter,
  WorkerForwardExporter,
} from "./exporters.js";
import type { DecisionRecord } from "./record.js";
import { Pipeline, type PipelineStats } from "./pipeline.js";
import { state } from "./state.js";
import { debug, describeError, safe, warn } from "./util.js";

export const ENV_EXPORTER = "BRIEFCASE_EXPORTER";
export const ENV_EXPORTER_TOKEN = "BRIEFCASE_EXPORTER_TOKEN";

type Env = Record<string, string | undefined>;

/** Resolves a shorthand or exporter instance (Python's `_resolve_exporter`). */
export function resolveExporter(exporter: string | Exporter | null | undefined, token?: string): Exporter {
  if (exporter && typeof exporter === "object") return exporter;
  if (exporter === null || exporter === undefined || exporter === "console") return new ConsoleExporter();
  if (exporter === "memory") return new MemoryExporter();
  if (/^https?:\/\//i.test(exporter)) return new HttpExporter(exporter, token ? { token } : {});
  if (exporter.endsWith(".jsonl")) return new JsonlFileExporter(exporter);
  throw new Error(
    `Unknown exporter shorthand: ${JSON.stringify(exporter)}. Pass an exporter instance, ` +
      "'console', 'memory', a path ending in '.jsonl', or an http(s):// URL.",
  );
}

/** `key1=value1,key2=value2` (W3C baggage style, values percent-decoded). */
export function parseOtlpHeaders(raw: string | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const pair of (raw ?? "").split(",")) {
    const index = pair.indexOf("=");
    if (index <= 0) continue;
    const key = pair.slice(0, index).trim();
    let value = pair.slice(index + 1).trim();
    try {
      value = decodeURIComponent(value);
    } catch {
      // keep it as written
    }
    if (key) out[key] = value;
  }
  return out;
}

export function otlpFromEnv(env: Env): Exporter {
  const traces = (env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] ?? "").trim();
  const base = (env["OTEL_EXPORTER_OTLP_ENDPOINT"] ?? "").trim();
  const protocol = (env["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] || env["OTEL_EXPORTER_OTLP_PROTOCOL"] || "").trim().toLowerCase();
  if (protocol === "grpc") {
    warn(
      "OTEL_EXPORTER_OTLP_PROTOCOL=grpc is not supported by @briefcase-ai/auto (it sends OTLP/HTTP " +
        "with JSON); records go to the console instead. Set OTEL_EXPORTER_OTLP_PROTOCOL=http/json and " +
        "point the endpoint at the collector's HTTP port (4318).",
    );
    return new ConsoleExporter();
  }
  if (protocol && protocol !== "http/json") {
    warn(`OTLP protocol ${JSON.stringify(protocol)} is not supported; sending OTLP/HTTP JSON to the same endpoint.`);
  }
  let endpoint: string;
  if (traces) endpoint = traces;
  else if (base) endpoint = base.replace(/\/+$/, "").endsWith("/v1/traces") ? base : `${base.replace(/\/+$/, "")}/v1/traces`;
  else endpoint = "http://localhost:4318/v1/traces";
  const headers = parseOtlpHeaders(env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] || env["OTEL_EXPORTER_OTLP_HEADERS"]);
  const serviceName = (env["OTEL_SERVICE_NAME"] ?? "").trim() || "briefcase-ai";
  // Plain http to a collector inside a cluster is the common OTel setup, as the OTel SDKs allow.
  return new OtlpHttpExporter(endpoint, { headers, serviceName, allowInsecure: true });
}

/** Python's `_exporter_from_env`. */
export function exporterFromEnv(env: Env = process.env): Exporter {
  const choice = (env[ENV_EXPORTER] ?? "").trim();
  if (["otlp", "otel"].includes(choice.toLowerCase())) return otlpFromEnv(env);
  if (choice) {
    const token = (env[ENV_EXPORTER_TOKEN] ?? "").trim();
    return resolveExporter(choice, token || undefined);
  }
  if ((env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] ?? "").trim() || (env["OTEL_EXPORTER_OTLP_ENDPOINT"] ?? "").trim()) {
    return otlpFromEnv(env);
  }
  return new ConsoleExporter();
}

export interface ObserveOptions {
  /** Most records held in memory before new ones are dropped (default 10 000). */
  maxQueue?: number;
}

/**
 * Sets the destination for decision records and returns the exporter (so a
 * `MemoryExporter` can be inspected). With no argument the destination
 * comes from the environment. Calling it again replaces the destination;
 * records already queued for the old one are flushed to it.
 */
export function observe<E extends Exporter = Exporter>(exporter?: string | E, options: ObserveOptions = {}): E {
  let resolved: E;
  if (exporter === undefined) {
    const channel = isMainThread ? undefined : safe(() => getEnvironmentData(WORKER_CHANNEL_KEY), "environment data");
    resolved = (typeof channel === "string" ? new WorkerForwardExporter(channel) : exporterFromEnv()) as E;
  } else {
    resolved = resolveExporter(exporter) as E;
  }
  const s = state();
  const previous = s.pipeline;
  s.pipeline = new Pipeline(resolved, options.maxQueue ?? 10_000);
  if (previous && previous !== s.pipeline) void previous.close();
  installExitHooks();
  if (isMainThread) listenForWorkers();
  return resolved;
}

// -- worker threads ------------------------------------------------------------

/**
 * Worker threads have their own module graph and their own copy of this
 * package. When the main thread is observing, a worker's `observe()` (no
 * argument, as the preload calls it) forwards each finished record to the
 * main thread on this channel; the main thread exports it. The channel name
 * reaches workers through `setEnvironmentData`, which only workers created
 * after `observe()` inherit (child processes do not).
 */
export const WORKER_CHANNEL = "briefcase-ai.auto.records.v1";
const WORKER_CHANNEL_KEY = "briefcase-ai.auto.channel";
function listenForWorkers(): void {
  const s = state();
  if (s.workerChannel) return; // one listener per thread, even with two copies of this package loaded
  safe(() => {
    const channel = new BroadcastChannel(WORKER_CHANNEL);
    channel.unref(); // never keeps the process alive
    channel.onmessage = (event: unknown) => {
      safe(() => {
        const record = (event as { data?: unknown }).data;
        if (!record || typeof record !== "object") return;
        const pipeline = state().pipeline;
        if (pipeline) pipeline.enqueue(record as DecisionRecord); // closed: counted as dropped
      }, "worker record");
    };
    s.workerChannel = channel;
    setEnvironmentData(WORKER_CHANNEL_KEY, WORKER_CHANNEL);
  }, "worker channel");
}

/** Sends every queued record now. */
export async function flush(): Promise<void> {
  await state().pipeline?.flush(false);
}

/**
 * Emits the records of calls still in flight (unread streams are recorded
 * as incomplete), sends everything queued, and stops exporting. Later
 * provider calls pass through unrecorded until `observe()` is called again.
 */
export async function shutdown(): Promise<void> {
  // Workers created from now on export on their own; it never waits for running workers.
  if (isMainThread && state().workerChannel) safe(() => setEnvironmentData(WORKER_CHANNEL_KEY, undefined), "environment data");
  abandonPending();
  const pipeline = state().pipeline;
  if (pipeline) await pipeline.close();
}

export function exportStats(): PipelineStats {
  const s = state();
  const p = s.pipeline;
  return {
    queued: p?.queue.length ?? 0,
    exported: p?.exported ?? 0,
    dropped: p?.dropped ?? 0,
    failed_batches: p?.failedBatches ?? 0,
    pending_calls: s.pending.size,
  };
}

function abandonPending(): void {
  for (const call of [...state().pending]) safe(() => call.abandon(), "abandon");
}

// -- process hooks -------------------------------------------------------------

const SIGNAL_FLUSH_MS = 2000;
let beforeExitFlushes = 0;

function onBeforeExit(): void {
  const pipeline = state().pipeline;
  if (!pipeline || pipeline.closed) return;
  // Nothing can make progress any more: unread streams and unparsed responses are final now.
  abandonPending();
  if (pipeline.queue.length === 0 || beforeExitFlushes >= 3) return;
  beforeExitFlushes++;
  void pipeline.flush(true);
}

function onExit(): void {
  safe(() => {
    abandonPending();
    state().pipeline?.drainSync();
  }, "exit flush");
}

function makeSignalHandler(signal: NodeJS.Signals): () => void {
  const handler = (): void => {
    const others = process.listeners(signal).filter((l) => l !== handler);
    const pipeline = state().pipeline;
    abandonPending();
    const flushed = pipeline ? pipeline.flush(true) : Promise.resolve();
    const timeout = new Promise<void>((resolve) => setTimeout(resolve, SIGNAL_FLUSH_MS).unref());
    if (others.length > 0) {
      // The application handles this signal; it decides when to exit.
      void flushed;
      return;
    }
    void Promise.race([flushed, timeout]).finally(() => {
      process.removeListener(signal, handler);
      // Re-raise so the default action (exit with 128 + signal number) happens as it would have.
      process.kill(process.pid, signal);
    });
  };
  return handler;
}

function installExitHooks(): void {
  const s = state();
  if (s.hooksInstalled) return;
  s.hooksInstalled = true;
  try {
    process.on("beforeExit", onBeforeExit);
    process.on("exit", onExit);
    // Signals are delivered to the main thread only.
    if (isMainThread) for (const signal of ["SIGTERM", "SIGINT"] as const) process.on(signal, makeSignalHandler(signal));
  } catch (error) {
    debug(`exit hooks not installed: ${describeError(error)}`);
  }
}
