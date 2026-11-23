/**
 * Cross-language contract: a record emitted by this package must satisfy the
 * shared golden schema (`tests/fixtures/decision_record.json`; a copy of the
 * main-branch version lives in `test/fixtures/` until the shared file is
 * merged), and its batches / spans must be what the Python side produces
 * and consumes. The Python checks run the real `briefcase` package from this
 * repository (and the OpenTelemetry SDK) when they are importable, and are
 * skipped otherwise.
 */

import { spawn, spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { createServer, type IncomingHttpHeaders } from "node:http";
import type { AddressInfo } from "node:net";
import { fileURLToPath } from "node:url";

import OpenAI from "openai";
import { afterAll, describe, expect, it } from "vitest";

import { undo } from "../src/auto.js";
import { HttpExporter, decisionAttributes, otlpTracesBody } from "../src/exporters.js";
import { shutdown } from "../src/observe.js";
import type { DecisionRecord } from "../src/record.js";
import { SECRET_KEY, fakeFetch, fresh, json, records } from "./helpers.js";

const REPO_ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const PY_HELPER = fileURLToPath(new URL("./fixtures/python_contract.py", import.meta.url));

const fixture = JSON.parse(readFileSync(new URL("./fixtures/decision_record.json", import.meta.url), "utf8")) as {
  schema_version: string;
  required_fields: string[];
  optional_fields: string[];
};

/** Fields Python's provider instrumentation adds (briefcase/integrations/providers/_base.py docstring and modules). */
const PROVIDER_FIELDS = [
  "model_parameters", "token_usage", "error_type", "http_status", "finish_reason", "response_id", "request_id",
  "stream", "time_to_first_chunk_ms", "service_tier", "response_error_code", "embedding_count",
];

const pythonOk =
  spawnSync("python3", ["-c", "import briefcase.receiver.otlp, briefcase.exporters.otel, opentelemetry.sdk"], {
    env: { ...process.env, PYTHONPATH: REPO_ROOT },
    encoding: "utf8",
  }).status === 0;

function python(request: unknown): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const child = spawn("python3", [PY_HELPER], { env: { ...process.env, PYTHONPATH: REPO_ROOT } });
    let out = "";
    let err = "";
    child.stdout.on("data", (d: Buffer) => (out += d.toString()));
    child.stderr.on("data", (d: Buffer) => (err += d.toString()));
    child.on("close", (code) => (code === 0 ? resolve(JSON.parse(out) as Record<string, unknown>) : reject(new Error(err))));
    child.stdin.end(JSON.stringify(request));
  });
}

const completion = {
  id: "chatcmpl-1",
  object: "chat.completion",
  created: 1,
  model: "gpt-4o-mini",
  choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 },
};

async function tsRecords(): Promise<DecisionRecord[]> {
  const mem = await fresh({ only: ["openai"], contextVersion: "v3" });
  const client = new OpenAI({ apiKey: SECRET_KEY, maxRetries: 0, fetch: fakeFetch(({ body }) => (body?.["model"] === "fail" ? json({ error: { message: "bad" } }, {}, 400) : json(completion))) });
  await client.chat.completions.create({ model: "gpt-4o-mini", messages: [] });
  await client.chat.completions.create({ model: "fail", messages: [] }).catch(() => undefined);
  return records(mem, 2);
}

afterAll(async () => {
  undo();
  await shutdown();
});

describe("golden decision-record schema", () => {
  it("TS records carry every required field and the shared schema_version", async () => {
    const [ok, failed] = await tsRecords();
    for (const r of [ok!, failed!]) {
      for (const field of fixture.required_fields) expect(r, field).toHaveProperty(field);
      expect(r.schema_version).toBe(fixture.schema_version);
      const allowed = new Set([...fixture.required_fields, ...fixture.optional_fields, ...PROVIDER_FIELDS]);
      for (const key of Object.keys(r)) expect(allowed.has(key), `unexpected field ${key}`).toBe(true);
      expect(typeof r.execution_time_ms).toBe("number");
      expect(r.started_at).toMatch(/\+00:00$/);
    }
    expect(failed).toMatchObject({ status: "error", error: "BadRequestError", http_status: 400 });
  });
});

describe.skipIf(!pythonOk)("against the Python implementation", () => {
  it("Python's OTelExporter derives the same span attributes, parent and status from a TS record", async () => {
    const [, failed] = await tsRecords();
    const withParent = { ...failed!, trace_id: "4bf92f3577b34da6a3ce929d0e0e4736", parent_span_id: "00f067aa0ba902b7", trace_flags: 1 };
    const py = await python({ op: "otel_attrs", record: withParent });
    const ours = Object.fromEntries(
      decisionAttributes(withParent).map(({ key, value }) => [key, Object.values(value)[0]]),
    );
    expect(py["attributes"]).toEqual(ours);
    expect(py["name"]).toBe("briefcase.decision");
    expect(py["status"]).toBe("ERROR");
    expect(py["trace_id"]).toBe(withParent.trace_id);
    expect(py["parent_span_id"]).toBe(withParent.parent_span_id);
    const span = (otlpTracesBody([withParent], "briefcase-ai") as { resourceSpans: { scopeSpans: { spans: Record<string, string>[] }[] }[] })
      .resourceSpans[0]!.scopeSpans[0]!.spans[0]!;
    expect(BigInt(span["startTimeUnixNano"]!)).toBe(BigInt(py["start"] as string));
    expect(BigInt(span["endTimeUnixNano"]!)).toBe(BigInt(py["end"] as string));
  });

  it("briefcase-ai receive's OTLP/JSON parser accepts our trace body", async () => {
    const [ok] = await tsRecords();
    const body = otlpTracesBody([ok!], "svc");
    const py = await python({ op: "otlp_parse", body });
    const signals = py["signals"] as Record<string, unknown>[];
    expect(signals).toHaveLength(1);
    expect(signals[0]).toMatchObject({
      kind: "span",
      name: "briefcase.decision",
      resource: { "service.name": "svc" },
      attrs: { "briefcase.decision.id": ok!.decision_id, "briefcase.model.provider": "openai" },
    });
  });

  it("our {\"records\": [...]} batch has the shape Python's HTTPExporter sends", async () => {
    const [ok] = await tsRecords();
    const received: { headers: IncomingHttpHeaders; body: Record<string, unknown> }[] = [];
    const server = createServer((req, res) => {
      let data = "";
      req.on("data", (c: Buffer) => (data += c.toString()));
      req.on("end", () => {
        received.push({ headers: req.headers, body: JSON.parse(data) as Record<string, unknown> });
        res.writeHead(200).end();
      });
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1/decisions`;
    try {
      await python({ op: "http_post", url, token: "tok", record: ok });
      await new HttpExporter(url, { token: "tok" }).exportBatch([ok!], true);
    } finally {
      server.close();
    }
    expect(received).toHaveLength(2);
    const [py, ts] = received;
    expect(Object.keys(ts!.body)).toEqual(Object.keys(py!.body));
    expect(ts!.body).toEqual(py!.body);
    expect(ts!.headers["content-type"]).toBe(py!.headers["content-type"]);
    expect(ts!.headers["authorization"]).toBe(py!.headers["authorization"]);
  });
});
