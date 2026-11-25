/**
 * Destinations for decision records. Each exporter receives batches from the
 * bounded queue in `pipeline.ts`:
 *
 * - `ConsoleExporter`: one JSON line per record on stderr.
 * - `JsonlFileExporter`: appends JSON lines to a file (dir 0700, file 0600).
 * - `HttpExporter`: POSTs `{"records": [...]}` batches, the body Python's
 *   `briefcase.exporters.HTTPExporter` sends; https required off loopback,
 *   bearer token, retry with jittered exponential backoff, no redirects.
 * - `OtlpHttpExporter`: OTLP/HTTP JSON, one `briefcase.decision` span per
 *   record with the attributes Python's `OTelExporter` sets.
 * - `MemoryExporter`: keeps records in memory (tests, notebooks).
 */

import { randomBytes } from "node:crypto";
import { closeSync, fchmodSync, mkdirSync, openSync, writeSync } from "node:fs";
import { dirname } from "node:path";
import { BroadcastChannel, isMainThread } from "node:worker_threads";

import type { DecisionRecord } from "./record.js";
import { read, warn } from "./util.js";

export type ExportResult = "sent" | "retry" | "rejected";

export interface Exporter {
  /** Short kind name shown by `status()`: console, jsonl, http, otlp, memory. */
  readonly kind: string;
  /** Records per batch; 1 means "export as soon as a record arrives". */
  readonly batchSize: number;
  /** Also send buffered records this often (ms); 0 disables the timer. */
  readonly flushIntervalMs: number;
  /** Sends one batch. `final` is set during shutdown: no backoff waits. */
  exportBatch(records: DecisionRecord[], final: boolean): ExportResult | Promise<ExportResult>;
  /** Synchronous export, used from the process `exit` handler when available. */
  exportSync?(records: DecisionRecord[]): void;
  /**
   * Export each record synchronously (`exportSync`) the moment it is
   * queued. Set for destinations where that is cheap and where waiting
   * would risk losing it (a worker thread can be terminated at any time).
   */
  readonly immediate?: boolean;
  close?(): void | Promise<void>;
  /** Destination without credentials, for `status()`. */
  describe(): string;
}

interface WritableLike {
  write(chunk: string): unknown;
}

export class ConsoleExporter implements Exporter {
  readonly kind = "console";
  readonly batchSize = 1;
  readonly flushIntervalMs = 0;
  /** In a worker thread, records are written straight to fd 2 (a worker's process.stderr is asynchronous, and lost on terminate()). */
  readonly immediate = !isMainThread;
  private readonly stream: WritableLike;

  constructor(stream?: WritableLike) {
    this.stream = stream ?? (isMainThread ? process.stderr : { write: (chunk: string) => writeSync(2, chunk) });
  }

  exportBatch(records: DecisionRecord[]): ExportResult {
    this.exportSync(records);
    return "sent";
  }

  exportSync(records: DecisionRecord[]): void {
    for (const record of records) this.stream.write(JSON.stringify(record) + "\n");
  }

  describe(): string {
    return "console (stderr)";
  }
}

export class JsonlFileExporter implements Exporter {
  readonly kind = "jsonl";
  readonly batchSize = 1;
  readonly flushIntervalMs = 0;
  /** Each record is appended synchronously when it is queued in a worker thread. */
  readonly immediate = !isMainThread;
  private fd: number | null = null;

  constructor(readonly path: string) {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  }

  private ensureOpen(): number {
    if (this.fd === null) {
      this.fd = openSync(this.path, "a", 0o600);
      fchmodSync(this.fd, 0o600);
    }
    return this.fd;
  }

  exportBatch(records: DecisionRecord[]): ExportResult {
    this.exportSync(records);
    return "sent";
  }

  exportSync(records: DecisionRecord[]): void {
    if (records.length === 0) return;
    const fd = this.ensureOpen();
    writeSync(fd, records.map((r) => JSON.stringify(r) + "\n").join(""));
  }

  close(): void {
    if (this.fd !== null) {
      closeSync(this.fd);
      this.fd = null;
    }
  }

  describe(): string {
    return this.path;
  }
}

export class MemoryExporter implements Exporter {
  readonly kind = "memory";
  readonly batchSize = 1;
  readonly flushIntervalMs = 0;
  readonly records: DecisionRecord[] = [];

  exportBatch(records: DecisionRecord[]): ExportResult {
    this.exportSync(records);
    return "sent";
  }

  exportSync(records: DecisionRecord[]): void {
    this.records.push(...records);
  }

  clear(): void {
    this.records.length = 0;
  }

  describe(): string {
    return "memory";
  }
}

// -- HTTP transport shared by the HTTP and OTLP exporters ---------------------

export type FetchLike = (input: string, init: {
  method: string;
  headers: Record<string, string>;
  body: string;
  redirect: "manual";
  signal: AbortSignal;
}) => Promise<{ status: number; headers: { get(name: string): string | null }; text(): Promise<string> }>;

export function isLoopbackHost(host: string): boolean {
  const h = host.replace(/^\[|\]$/g, "").toLowerCase();
  if (h === "localhost" || h === "::1") return true;
  return /^127(\.\d{1,3}){3}$/.test(h);
}

export interface HttpTransportOptions {
  headers?: Record<string, string>;
  timeoutMs?: number;
  maxRetries?: number;
  backoffBaseMs?: number;
  backoffMaxMs?: number;
  allowInsecure?: boolean;
  /** Replaces the global fetch (tests, proxies). */
  fetch?: FetchLike;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    timer.unref?.();
  });
}

abstract class HttpTransport {
  protected readonly url: string;
  protected readonly logUrl: string;
  protected readonly headers: Record<string, string>;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly backoffBaseMs: number;
  private readonly backoffMaxMs: number;
  private readonly fetchImpl: FetchLike;
  failedBatches = 0;

  constructor(url: string, label: string, options: HttpTransportOptions, defaults: Record<string, string>) {
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      throw new Error(`${label} needs an http(s) URL, got ${JSON.stringify(url)}`);
    }
    if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.hostname) {
      throw new Error(`${label} needs an http(s) URL, got ${JSON.stringify(url)}`);
    }
    if (parsed.protocol === "http:" && !options.allowInsecure && !isLoopbackHost(parsed.hostname)) {
      throw new Error(
        `${label} URL uses http with a non-loopback host; records and any bearer token would be ` +
          "sent in cleartext. Use https, or pass allowInsecure: true.",
      );
    }
    this.url = url;
    this.logUrl = `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
    this.headers = { ...defaults, ...(options.headers ?? {}) };
    this.timeoutMs = options.timeoutMs ?? 10_000;
    this.maxRetries = Math.max(0, options.maxRetries ?? 3);
    this.backoffBaseMs = Math.max(0, options.backoffBaseMs ?? 500);
    this.backoffMaxMs = Math.max(0, options.backoffMaxMs ?? 30_000);
    const globalFetch = globalThis.fetch as unknown as FetchLike | undefined;
    const chosen = options.fetch ?? globalFetch;
    if (!chosen) throw new Error(`${label} needs fetch (Node >= 26 provides it)`);
    this.fetchImpl = chosen;
  }

  protected abstract retryable(status: number): boolean;
  protected abstract label: string;

  retryDelayMs(attempt: number, retryAfter: string | null): number {
    const ceiling = Math.min(this.backoffMaxMs, this.backoffBaseMs * 2 ** attempt);
    let delay = ceiling > 0 ? ceiling / 2 + Math.random() * (ceiling / 2) : 0;
    if (retryAfter) {
      const seconds = Number(retryAfter);
      if (Number.isFinite(seconds) && seconds >= 0) delay = Math.max(delay, Math.min(seconds * 1000, this.backoffMaxMs));
    }
    return delay;
  }

  /** POSTs `body`; returns sent / retry (transient, after all attempts) / rejected. */
  protected async post(body: string, count: number, final: boolean, onOk?: (text: string) => void): Promise<ExportResult> {
    const attempts = 1 + this.maxRetries;
    let problem = "";
    for (let attempt = 0; attempt < attempts; attempt++) {
      let retryAfter: string | null = null;
      try {
        const response = await this.fetchImpl(this.url, {
          method: "POST",
          headers: this.headers,
          body,
          redirect: "manual",
          signal: AbortSignal.timeout(this.timeoutMs),
        });
        const text = await response.text().catch(() => "");
        if (response.status >= 200 && response.status < 300) {
          onOk?.(text);
          return "sent";
        }
        problem = `HTTP ${response.status}`;
        retryAfter = response.headers.get("retry-after");
        if (!this.retryable(response.status)) {
          this.failedBatches++;
          warn(`${this.label}: ${this.logUrl} rejected a batch of ${count} record(s) with ${problem}; not retryable, batch dropped`);
          return "rejected";
        }
      } catch (error) {
        problem = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      }
      if (attempt + 1 < attempts && !final) await sleep(this.retryDelayMs(attempt, retryAfter));
    }
    this.failedBatches++;
    warn(`${this.label}: sending ${count} record(s) to ${this.logUrl} failed (${problem}) after ${attempts} attempt(s); keeping them for the next flush`);
    return "retry";
  }
}

export interface HttpExporterOptions extends HttpTransportOptions {
  token?: string;
  batchSize?: number;
  flushIntervalMs?: number;
}

/** POSTs `{"records": [...]}` batches, like Python's HTTPExporter. */
export class HttpExporter extends HttpTransport implements Exporter {
  readonly kind = "http";
  readonly batchSize: number;
  readonly flushIntervalMs: number;
  protected label = "HttpExporter";

  constructor(url: string, options: HttpExporterOptions = {}) {
    const defaults: Record<string, string> = {
      "Content-Type": "application/json",
      "User-Agent": "briefcase-ai-auto-http-exporter",
    };
    if (options.token) defaults["Authorization"] = `Bearer ${options.token}`;
    super(url, "HttpExporter", options, defaults);
    this.batchSize = Math.max(1, options.batchSize ?? 100);
    this.flushIntervalMs = Math.max(0, options.flushIntervalMs ?? 5000);
  }

  protected retryable(status: number): boolean {
    return status === 429 || status >= 500;
  }

  exportBatch(records: DecisionRecord[], final: boolean): Promise<ExportResult> {
    return this.post(JSON.stringify({ records }), records.length, final);
  }

  describe(): string {
    return this.logUrl;
  }
}

// -- OTLP/HTTP JSON ------------------------------------------------------------

export interface OtlpExporterOptions extends HttpTransportOptions {
  serviceName?: string;
  batchSize?: number;
  flushIntervalMs?: number;
}

type AnyValue = { stringValue: string } | { doubleValue: number } | { intValue: string } | { boolValue: boolean };
interface KeyValue {
  key: string;
  value: AnyValue;
}

function str(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

function num(value: unknown): number {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : 0;
}

/** ISO 8601 timestamp (any fraction length, Z or offset) to epoch nanoseconds. */
export function isoToNanos(value: unknown): bigint | null {
  if (typeof value !== "string") return null;
  const m = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$/.exec(value.trim());
  if (!m) return null;
  const seconds = Date.parse(`${m[1]}${m[3] ?? "Z"}`);
  if (!Number.isFinite(seconds)) return null;
  const fraction = (m[2] ?? "").padEnd(9, "0").slice(0, 9);
  return BigInt(seconds) * 1_000_000n + BigInt(fraction);
}

/**
 * The eight `briefcase.*` span attributes, derived exactly as Python's
 * `OTelExporter._extract_attrs` derives them from a dict record.
 */
export function decisionAttributes(record: Record<string, unknown>): KeyValue[] {
  const outputs = record["outputs"];
  let confidence = 0;
  if (Array.isArray(outputs)) {
    const hit = outputs.find((o) => read(o, "confidence") !== undefined && read(o, "confidence") !== null);
    if (hit) confidence = num(read(hit, "confidence"));
  } else if (outputs && typeof outputs === "object") {
    confidence = num(read(outputs, "confidence") ?? 0);
  }
  if (record["confidence"] !== undefined && record["confidence"] !== null) confidence = num(record["confidence"]);
  const routing = record["routing_decision"];
  const params = record["model_parameters"];
  const contextVersion = str(read(record["context"], "version")) || str(record["context_version"]);
  return [
    { key: "briefcase.decision.id", value: { stringValue: str(record["decision_id"] ?? record["id"]) } },
    { key: "briefcase.context.version", value: { stringValue: contextVersion } },
    { key: "briefcase.confidence", value: { doubleValue: confidence } },
    {
      key: "briefcase.routing.action",
      value: { stringValue: routing ? str(read(routing, "action") ?? "unrouted") : "unrouted" },
    },
    { key: "briefcase.model.name", value: { stringValue: str(read(params, "model_name")) } },
    { key: "briefcase.model.provider", value: { stringValue: str(read(params, "provider")) } },
    { key: "briefcase.execution.time_ms", value: { doubleValue: num(record["execution_time_ms"]) } },
    { key: "briefcase.snapshot.type", value: { stringValue: str(record["snapshot_type"] ?? "decision") } },
  ];
}

const HEX32 = /^[0-9a-f]{32}$/;
const HEX16 = /^[0-9a-f]{16}$/;

/** One OTLP/JSON `Span` for a record (proto3 JSON mapping: lowerCamelCase, hex ids, enums as integers, int64 as strings). */
export function decisionSpan(record: Record<string, unknown>): Record<string, unknown> {
  const traceId = String(record["trace_id"] ?? "").toLowerCase();
  const parentSpanId = String(record["parent_span_id"] ?? "").toLowerCase();
  const hasParent = HEX32.test(traceId) && HEX16.test(parentSpanId) && !/^0+$/.test(traceId) && !/^0+$/.test(parentSpanId);
  let start = isoToNanos(record["started_at"]);
  let end = isoToNanos(record["ended_at"]);
  const execMs = record["execution_time_ms"];
  if (typeof execMs === "number" && Number.isFinite(execMs) && execMs >= 0) {
    const duration = BigInt(Math.round(execMs * 1_000_000));
    if (start !== null && end === null) end = start + duration;
    else if (end !== null && start === null) start = end - duration;
  }
  const now = BigInt(Date.now()) * 1_000_000n;
  start ??= now;
  end ??= start;
  if (end < start) end = start;
  const flags = typeof record["trace_flags"] === "number" ? (record["trace_flags"] as number) & 0xff : 1;
  const span: Record<string, unknown> = {
    traceId: hasParent ? traceId : randomBytes(16).toString("hex"),
    spanId: randomBytes(8).toString("hex"),
    name: "briefcase.decision",
    kind: 1, // SPAN_KIND_INTERNAL
    startTimeUnixNano: start.toString(),
    endTimeUnixNano: end.toString(),
    attributes: decisionAttributes(record),
    flags,
    status: record["error"] ? { code: 2 } : {}, // STATUS_CODE_ERROR / unset
  };
  if (hasParent) span["parentSpanId"] = parentSpanId;
  return span;
}

export const SCOPE_VERSION = "5.0.0";

export function otlpTracesBody(records: DecisionRecord[], serviceName: string): Record<string, unknown> {
  return {
    resourceSpans: [
      {
        resource: { attributes: [{ key: "service.name", value: { stringValue: serviceName } }] },
        scopeSpans: [
          {
            scope: { name: "briefcase", version: SCOPE_VERSION },
            spans: records.map((r) => decisionSpan(r as Record<string, unknown>)),
          },
        ],
      },
    ],
  };
}

/** OTLP/HTTP with the JSON encoding, posting to a `/v1/traces` URL. */
export class OtlpHttpExporter extends HttpTransport implements Exporter {
  readonly kind = "otlp";
  readonly batchSize: number;
  readonly flushIntervalMs: number;
  readonly serviceName: string;
  rejectedSpans = 0;
  protected label = "OtlpHttpExporter";

  constructor(endpoint: string, options: OtlpExporterOptions = {}) {
    super(endpoint, "OtlpHttpExporter", options, {
      "Content-Type": "application/json",
      "User-Agent": "briefcase-ai-auto-otlp-exporter",
    });
    this.serviceName = options.serviceName ?? "briefcase-ai";
    this.batchSize = Math.max(1, options.batchSize ?? 100);
    this.flushIntervalMs = Math.max(0, options.flushIntervalMs ?? 5000);
  }

  /** OTLP/HTTP: only 429, 502, 503 and 504 are retryable. */
  protected retryable(status: number): boolean {
    return status === 429 || status === 502 || status === 503 || status === 504;
  }

  exportBatch(records: DecisionRecord[], final: boolean): Promise<ExportResult> {
    const body = JSON.stringify(otlpTracesBody(records, this.serviceName));
    return this.post(body, records.length, final, (text) => {
      // A partial success is not retried; report what the server dropped.
      try {
        const rejected = Number(read(JSON.parse(text), "partialSuccess", "rejectedSpans") ?? 0);
        if (rejected > 0) {
          this.rejectedSpans += rejected;
          warn(`OtlpHttpExporter: ${this.logUrl} rejected ${rejected} span(s) (partial success)`);
        }
      } catch {
        // empty or non-JSON body: full success
      }
    });
  }

  describe(): string {
    return `otlp ${this.logUrl}`;
  }
}

/**
 * Used in a worker thread when the main thread is observing: each record is
 * posted to the main thread (a `BroadcastChannel`) the moment it is
 * finished, and exported there by the main thread's destination. A posted
 * message survives the worker being terminated.
 */
export class WorkerForwardExporter implements Exporter {
  readonly kind = "main-thread";
  readonly batchSize = 1;
  readonly flushIntervalMs = 0;
  readonly immediate = true;
  private readonly channel: BroadcastChannel;

  constructor(readonly channelName: string) {
    this.channel = new BroadcastChannel(channelName);
    this.channel.unref();
  }

  exportBatch(records: DecisionRecord[]): ExportResult {
    this.exportSync(records);
    return "sent";
  }

  exportSync(records: DecisionRecord[]): void {
    for (const record of records) this.channel.postMessage(record);
  }

  close(): void {
    this.channel.close();
  }

  describe(): string {
    return "the main thread's destination";
  }
}
