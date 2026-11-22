import { mkdtempSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import {
  ConsoleExporter,
  HttpExporter,
  JsonlFileExporter,
  MemoryExporter,
  OtlpHttpExporter,
  decisionSpan,
  isoToNanos,
  type FetchLike,
} from "../src/exporters.js";
import { exporterFromEnv, parseOtlpHeaders } from "../src/observe.js";
import { Pipeline } from "../src/pipeline.js";
import { type DecisionRecord, SCHEMA_VERSION } from "../src/record.js";

function record(overrides: Partial<DecisionRecord> = {}): DecisionRecord {
  return {
    schema_version: SCHEMA_VERSION,
    decision_id: "6f9619ff-8b86-4d01-b42d-00cf4fc964ff",
    decision_type: "llm.openai.chat",
    function_name: "openai.chat.completions.create",
    inputs: {},
    outputs: {},
    started_at: "2026-08-15T12:00:00.000000+00:00",
    ended_at: "2026-08-15T12:00:00.250000+00:00",
    execution_time_ms: 250,
    status: "ok",
    model_parameters: { provider: "openai", model_name: "gpt-4o-mini" },
    token_usage: null,
    stream: false,
    ...overrides,
  };
}

interface Sent {
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

function fakeHttp(statuses: (number | Error)[], retryAfter?: string): FetchLike & { sent: Sent[] } {
  const sent: Sent[] = [];
  let i = 0;
  const fn = (async (url, init) => {
    sent.push({ url, headers: init.headers, body: JSON.parse(init.body) });
    const next = statuses[Math.min(i++, statuses.length - 1)]!;
    if (next instanceof Error) throw next;
    return {
      status: next,
      headers: { get: (name: string) => (name === "retry-after" ? (retryAfter ?? null) : null) },
      text: async () => "",
    };
  }) as FetchLike & { sent: Sent[] };
  fn.sent = sent;
  return fn;
}

describe("HttpExporter", () => {
  it("POSTs {\"records\": [...]} with a bearer token and JSON content type", async () => {
    const http = fakeHttp([200]);
    const exporter = new HttpExporter("https://collector.example.com/v1/decisions", { token: "tok-1", fetch: http });
    expect(await exporter.exportBatch([record(), record()], false)).toBe("sent");
    expect(http.sent[0]!.headers).toMatchObject({ "Content-Type": "application/json", Authorization: "Bearer tok-1" });
    expect(http.sent[0]!.body).toEqual({ records: [record(), record()] });
    expect(exporter.describe()).toBe("https://collector.example.com/v1/decisions");
  });

  it("requires https off loopback", () => {
    expect(() => new HttpExporter("http://collector.example.com/x")).toThrow(/cleartext/);
    expect(() => new HttpExporter("http://127.0.0.1:8080/x")).not.toThrow();
    expect(() => new HttpExporter("http://localhost/x")).not.toThrow();
    expect(() => new HttpExporter("http://[::1]:9/x")).not.toThrow();
    expect(() => new HttpExporter("http://collector.example.com/x", { allowInsecure: true })).not.toThrow();
    expect(() => new HttpExporter("ftp://x/y")).toThrow(/http\(s\) URL/);
  });

  it("retries 5xx / 429 / network errors with backoff, honouring Retry-After", async () => {
    const http = fakeHttp([503, new TypeError("fetch failed"), 200]);
    const exporter = new HttpExporter("https://c.example.com/x", { fetch: http, backoffBaseMs: 1, backoffMaxMs: 5 });
    expect(await exporter.exportBatch([record()], false)).toBe("sent");
    expect(http.sent).toHaveLength(3);
    const delays = new HttpExporter("https://c.example.com/x", { backoffBaseMs: 100, backoffMaxMs: 30_000 });
    expect(delays.retryDelayMs(0, "7")).toBe(7000);
    const d = delays.retryDelayMs(2, null);
    expect(d).toBeGreaterThanOrEqual(200);
    expect(d).toBeLessThanOrEqual(400);
  });

  it("drops a batch on a non-retryable 4xx and keeps it on exhausted retries", async () => {
    const stderr = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const rejecting = new HttpExporter("https://c.example.com/x", { fetch: fakeHttp([400]) });
    expect(await rejecting.exportBatch([record()], false)).toBe("rejected");
    const flaky = new HttpExporter("https://c.example.com/x", { fetch: fakeHttp([500]), maxRetries: 1, backoffBaseMs: 1 });
    expect(await flaky.exportBatch([record()], false)).toBe("retry");
    stderr.mockRestore();
  });
});

describe("Pipeline", () => {
  it("is bounded: new records are dropped and counted when full", async () => {
    const mem = new MemoryExporter();
    const stderr = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const pipeline = new Pipeline(mem, 3);
    for (let i = 0; i < 5; i++) pipeline.enqueue(record({ decision_id: String(i) }));
    stderr.mockRestore();
    expect(pipeline.dropped).toBe(2);
    await pipeline.flush();
    expect(mem.records.map((r) => r.decision_id)).toEqual(["0", "1", "2"]);
    expect(pipeline.exported).toBe(3);
  });

  it("batches for HTTP and keeps a failed batch for the next flush", async () => {
    const http = fakeHttp([500, 200]);
    const stderr = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const exporter = new HttpExporter("https://c.example.com/x", { fetch: http, maxRetries: 0, batchSize: 2, flushIntervalMs: 60_000 });
    const pipeline = new Pipeline(exporter);
    pipeline.enqueue(record({ decision_id: "a" }));
    pipeline.enqueue(record({ decision_id: "b" }));
    pipeline.enqueue(record({ decision_id: "c" }));
    await pipeline.flush();
    stderr.mockRestore();
    expect(pipeline.queue.map((r) => r.decision_id)).toEqual(["a", "b", "c"]); // first attempt failed, nothing lost
    await pipeline.flush();
    expect(pipeline.queue).toHaveLength(0);
    expect(http.sent.map((s) => (s.body as { records: DecisionRecord[] }).records.length)).toEqual([2, 2, 1]);
    pipeline.stopTimers();
  });
});

describe("console and file exporters", () => {
  it("console writes one JSON line per record", () => {
    const lines: string[] = [];
    new ConsoleExporter({ write: (s: string) => lines.push(s) }).exportBatch([record(), record()]);
    expect(lines).toHaveLength(2);
    expect(JSON.parse(lines[0]!)).toEqual(record());
  });

  it("jsonl appends to an owner-only file", () => {
    const dir = mkdtempSync(join(tmpdir(), "bc-auto-"));
    const path = join(dir, "nested", "runs.jsonl");
    const exporter = new JsonlFileExporter(path);
    exporter.exportBatch([record()]);
    exporter.exportBatch([record({ decision_id: "2" })]);
    exporter.close();
    const lines = readFileSync(path, "utf8").trim().split("\n");
    expect(lines.map((l) => (JSON.parse(l) as DecisionRecord).decision_id)).toEqual([record().decision_id, "2"]);
    expect(statSync(path).mode & 0o777).toBe(0o600);
  });
});

describe("environment resolution (same order and variables as Python)", () => {
  it("BRIEFCASE_EXPORTER first", () => {
    expect(exporterFromEnv({ BRIEFCASE_EXPORTER: "console", OTEL_EXPORTER_OTLP_ENDPOINT: "http://x:4318" }).kind).toBe("console");
    expect(exporterFromEnv({ BRIEFCASE_EXPORTER: "memory" }).kind).toBe("memory");
    const file = join(mkdtempSync(join(tmpdir(), "bc-auto-")), "r.jsonl");
    expect(exporterFromEnv({ BRIEFCASE_EXPORTER: file }).kind).toBe("jsonl");
    const http = exporterFromEnv({ BRIEFCASE_EXPORTER: "https://c.example.com/v1", BRIEFCASE_EXPORTER_TOKEN: "t0k" });
    expect(http.kind).toBe("http");
    expect(JSON.stringify(http.describe())).not.toContain("t0k");
    expect(() => exporterFromEnv({ BRIEFCASE_EXPORTER: "kafka" })).toThrow(/Unknown exporter shorthand/);
  });

  it("then the OTLP variables, with /v1/traces appended to the base endpoint", () => {
    const base = exporterFromEnv({ OTEL_EXPORTER_OTLP_ENDPOINT: "http://collector:4318/" });
    expect(base.describe()).toBe("otlp http://collector:4318/v1/traces");
    const signal = exporterFromEnv({ OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: "https://otel.example.com/custom", OTEL_SERVICE_NAME: "claims" });
    expect(signal.describe()).toBe("otlp https://otel.example.com/custom");
    expect((signal as OtlpHttpExporter).serviceName).toBe("claims");
    expect(exporterFromEnv({ BRIEFCASE_EXPORTER: "otlp" }).describe()).toBe("otlp http://localhost:4318/v1/traces");
  });

  it("grpc is not spoken: falls back to the console with a warning", () => {
    const stderr = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    expect(exporterFromEnv({ OTEL_EXPORTER_OTLP_ENDPOINT: "http://c:4317", OTEL_EXPORTER_OTLP_PROTOCOL: "grpc" }).kind).toBe("console");
    expect(stderr).toHaveBeenCalledWith(expect.stringContaining("grpc is not supported"));
    stderr.mockRestore();
  });

  it("parses OTEL_EXPORTER_OTLP_HEADERS", () => {
    expect(parseOtlpHeaders("api-key=abc%20def, x-tenant = t1,bad")).toEqual({ "api-key": "abc def", "x-tenant": "t1" });
  });

  it("then the console", () => {
    expect(exporterFromEnv({}).kind).toBe("console");
  });
});

describe("OTLP/HTTP JSON", () => {
  it("one briefcase.decision span per record, proto3 JSON mapping", async () => {
    const http = fakeHttp([200]);
    const exporter = new OtlpHttpExporter("http://127.0.0.1:4318/v1/traces", { fetch: http, headers: { "api-key": "k" }, serviceName: "svc" });
    await exporter.exportBatch(
      [record({ error: "RateLimitError", trace_id: "4bf92f3577b34da6a3ce929d0e0e4736", parent_span_id: "00f067aa0ba902b7", trace_flags: 1 })],
      false,
    );
    const body = http.sent[0]!.body as {
      resourceSpans: { resource: { attributes: unknown[] }; scopeSpans: { scope: { name: string }; spans: Record<string, unknown>[] }[] }[];
    };
    expect(http.sent[0]!.headers).toMatchObject({ "Content-Type": "application/json", "api-key": "k" });
    expect(body.resourceSpans[0]!.resource.attributes).toEqual([{ key: "service.name", value: { stringValue: "svc" } }]);
    const span = body.resourceSpans[0]!.scopeSpans[0]!.spans[0]!;
    expect(span).toMatchObject({
      traceId: "4bf92f3577b34da6a3ce929d0e0e4736",
      parentSpanId: "00f067aa0ba902b7",
      name: "briefcase.decision",
      kind: 1,
      startTimeUnixNano: "1786795200000000000",
      endTimeUnixNano: "1786795200250000000",
      status: { code: 2 },
      flags: 1,
    });
    expect(span["spanId"]).toMatch(/^[0-9a-f]{16}$/);
    expect(span["attributes"]).toContainEqual({ key: "briefcase.model.provider", value: { stringValue: "openai" } });
    expect(span["attributes"]).toContainEqual({ key: "briefcase.execution.time_ms", value: { doubleValue: 250 } });
  });

  it("retries only 429/502/503/504 (OTLP spec), not 500", async () => {
    const stderr = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const e500 = new OtlpHttpExporter("http://127.0.0.1:4318/v1/traces", { fetch: fakeHttp([500]) });
    expect(await e500.exportBatch([record()], false)).toBe("rejected");
    const e503 = new OtlpHttpExporter("http://127.0.0.1:4318/v1/traces", { fetch: fakeHttp([503, 200]), backoffBaseMs: 1 });
    expect(await e503.exportBatch([record()], false)).toBe("sent");
    stderr.mockRestore();
  });

  it("a span without a parent gets a fresh trace id; times parse at nanosecond precision", () => {
    const span = decisionSpan(record());
    expect(span["traceId"]).toMatch(/^[0-9a-f]{32}$/);
    expect(span).not.toHaveProperty("parentSpanId");
    expect(isoToNanos("2026-08-15T12:00:00.123456+00:00")).toBe(1786795200123456000n);
    expect(isoToNanos("2026-08-15T12:00:00.123Z")).toBe(1786795200123000000n);
  });
});
