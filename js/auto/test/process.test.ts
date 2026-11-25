/**
 * Real child processes running the compiled package (dist/, built by the
 * global setup): zero-code activation through `--import` / `NODE_OPTIONS` /
 * `--require`, and records delivered at exit without keeping the process
 * alive. The provider calls use an in-process fake `fetch`; the HTTP
 * collector is a local server in this test process.
 */

import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

const PKG = fileURLToPath(new URL("..", import.meta.url));
const APPS = join(PKG, ".test-apps");
const REGISTER = join(PKG, "dist", "register.js");

const FAKE_FETCH = `
const fakeFetch = async () => new Response(JSON.stringify({
  id: "chatcmpl-p", object: "chat.completion", created: 1, model: "gpt-4o-mini",
  choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
}), { status: 200, headers: { "content-type": "application/json", "x-request-id": "req_p" } });
`;

const APP_SOURCES: Record<string, string> = {
  "esm-app.mjs": `import OpenAI from "openai";
${FAKE_FETCH}
const client = new OpenAI({ apiKey: "sk-test", fetch: fakeFetch, maxRetries: 0 });
await client.chat.completions.create({ model: "gpt-4o-mini", messages: [] });
console.log("app done");
`,
  "cjs-app.cjs": `const OpenAI = require("openai");
${FAKE_FETCH}
const client = new OpenAI({ apiKey: "sk-test", fetch: fakeFetch, maxRetries: 0 });
client.chat.completions.create({ model: "gpt-4o-mini", messages: [] }).then(() => console.log("app done"));
`,
  "signal-app.mjs": `import OpenAI from "openai";
${FAKE_FETCH}
const client = new OpenAI({ apiKey: "sk-test", fetch: fakeFetch, maxRetries: 0 });
await client.chat.completions.create({ model: "gpt-4o-mini", messages: [] });
setInterval(() => {}, 1000); // a server that runs until it is told to stop
console.log("ready");
`,
  "exit-app.mjs": `import OpenAI from "openai";
${FAKE_FETCH}
const client = new OpenAI({ apiKey: "sk-test", fetch: fakeFetch, maxRetries: 0 });
await client.chat.completions.create({ model: "gpt-4o-mini", messages: [] });
process.exit(0);
`,
  "lazy-none.mjs": `import { createRequire } from "node:module";
import { status } from "@briefcase-ai/auto";
const require = createRequire(import.meta.url);
const sdkFiles = Object.keys(require.cache).filter((k) => /node_modules[\\/](openai|@anthropic-ai|@google|@aws-sdk)[\\/]/.test(k));
console.log(JSON.stringify({ states: status().providers.map((p) => p.state), sdkFiles }));
`,
  "lazy-esm.mjs": `import { createRequire } from "node:module";
import OpenAI from "openai";
import { status } from "@briefcase-ai/auto";
const require = createRequire(import.meta.url);
const cjsLoaded = Object.keys(require.cache).some((k) => k.endsWith("completions.js"));
const openai = status().providers.find((p) => p.name === "openai");
console.log(JSON.stringify({ state: openai.state, patched: openai.patched, cjsLoaded }));
`,
  "explicit-api.mjs": `import { observe, auto, shutdown } from "@briefcase-ai/auto";
import OpenAI from "openai";
${FAKE_FETCH}
observe();
auto();
const client = new OpenAI({ apiKey: "sk-test", fetch: fakeFetch, maxRetries: 0 });
await client.chat.completions.create({ model: "gpt-4o-mini", messages: [] });
await shutdown();
`,
};

interface Run {
  code: number | null;
  signal: NodeJS.Signals | null;
  stdout: string;
  stderr: string;
  ms: number;
}

function run(args: string[], env: Record<string, string> = {}, onStdout?: (text: string, kill: (s: NodeJS.Signals) => void) => void): Promise<Run> {
  return new Promise((resolve) => {
    const started = Date.now();
    const clean = { ...process.env };
    for (const key of Object.keys(clean)) if (key.startsWith("BRIEFCASE_") || key.startsWith("OTEL_") || key === "NODE_OPTIONS") delete clean[key];
    const child = spawn(process.execPath, args, { cwd: APPS, env: { ...clean, ...env } });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d: Buffer) => {
      stdout += d.toString();
      onStdout?.(stdout, (s) => child.kill(s));
    });
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    child.on("close", (code, signal) => resolve({ code, signal, stdout, stderr, ms: Date.now() - started }));
  });
}

function recordsIn(stderr: string): Record<string, unknown>[] {
  return stderr
    .split("\n")
    .filter((l) => l.startsWith("{"))
    .map((l) => JSON.parse(l) as Record<string, unknown>);
}

let server: Server;
let collectorUrl = "";
let batches: { auth: string | undefined; body: { records: Record<string, unknown>[] } }[] = [];

beforeAll(async () => {
  mkdirSync(APPS, { recursive: true });
  for (const [name, source] of Object.entries(APP_SOURCES)) writeFileSync(join(APPS, name), source);
  server = createServer((req, res) => {
    let data = "";
    req.on("data", (c: Buffer) => (data += c.toString()));
    req.on("end", () => {
      batches.push({ auth: req.headers.authorization, body: JSON.parse(data) as { records: Record<string, unknown>[] } });
      res.writeHead(200).end();
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  collectorUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1/decisions`;
});

afterAll(() => {
  server.close();
});

describe("zero-code activation", () => {
  it("node --import <register> app.mjs: records on stderr, no code changes", async () => {
    const r = await run(["--import", REGISTER, "esm-app.mjs"]);
    expect(r.code).toBe(0);
    expect(r.stdout).toContain("app done");
    const [record] = recordsIn(r.stderr);
    expect(record).toMatchObject({ decision_type: "llm.openai.chat", request_id: "req_p", token_usage: { total_tokens: 3 } });
  });

  it("the package specifier works too (--import @briefcase-ai/auto/register)", async () => {
    const r = await run(["--import", "@briefcase-ai/auto/register", "esm-app.mjs"]);
    expect(recordsIn(r.stderr)).toHaveLength(1);
  });

  it("a CommonJS app with --import, and with --require", async () => {
    for (const flag of ["--import", "--require"]) {
      const r = await run([flag, REGISTER, "cjs-app.cjs"]);
      expect(r.code, r.stderr).toBe(0);
      expect(recordsIn(r.stderr), flag).toHaveLength(1);
    }
  });

  it("NODE_OPTIONS alone does nothing; with BRIEFCASE_AUTO=1 it activates; BRIEFCASE_AUTO=0 always wins", async () => {
    const options = `--import=${REGISTER}`;
    const quiet = await run(["esm-app.mjs"], { NODE_OPTIONS: options });
    expect(recordsIn(quiet.stderr)).toHaveLength(0);
    const on = await run(["esm-app.mjs"], { NODE_OPTIONS: options, BRIEFCASE_AUTO: "1" });
    expect(recordsIn(on.stderr)).toHaveLength(1);
    const off = await run(["--import", REGISTER, "esm-app.mjs"], { BRIEFCASE_AUTO: "0" });
    expect(recordsIn(off.stderr)).toHaveLength(0);
  });

  it("BRIEFCASE_EXPORTER=*.jsonl, and process.exit() still writes the record (sync drain)", async () => {
    const file = join(mkdtempSync(join(tmpdir(), "bc-auto-")), "runs.jsonl");
    const r = await run(["--import", REGISTER, "exit-app.mjs"], { BRIEFCASE_EXPORTER: file });
    expect(r.code).toBe(0);
    expect(existsSync(file)).toBe(true);
    const lines = readFileSync(file, "utf8").trim().split("\n");
    expect(lines).toHaveLength(1);
  });

  it("an unknown BRIEFCASE_EXPORTER disables capture with one line, the app still runs", async () => {
    const r = await run(["--import", REGISTER, "esm-app.mjs"], { BRIEFCASE_EXPORTER: "kafka" });
    expect(r.code).toBe(0);
    expect(r.stdout).toContain("app done");
    expect(r.stderr).toContain("briefcase: auto-instrumentation is disabled for this process");
  });
});

describe("lazy patching", () => {
  it("an app that imports no SDK loads none of them", async () => {
    const r = await run(["--import", REGISTER, "lazy-none.mjs"]);
    expect(r.code, r.stderr).toBe(0);
    expect(JSON.parse(r.stdout)).toEqual({ states: ["waiting", "waiting", "waiting", "waiting"], sdkFiles: [] });
  });

  it("an ESM app gets the ESM copy patched and never loads the CommonJS copy", async () => {
    const r = await run(["--import", REGISTER, "lazy-esm.mjs"]);
    expect(r.code, r.stderr).toBe(0);
    const out = JSON.parse(r.stdout) as { state: string; patched: string[]; cjsLoaded: boolean };
    expect(out.state).toBe("patched");
    expect(out.patched.every((t) => t.startsWith("esm "))).toBe(true);
    expect(out.patched).toHaveLength(4);
    expect(out.cjsLoaded).toBe(false);
  });

  it("BRIEFCASE_AUTO_EAGER=1 patches every installed copy at startup", async () => {
    const r = await run(["--import", REGISTER, "lazy-none.mjs"], { BRIEFCASE_AUTO_EAGER: "1" });
    expect(r.code, r.stderr).toBe(0);
    expect(JSON.parse(r.stdout).states).toEqual(["patched", "patched", "patched", "patched"]);
  });
});

describe("delivery at exit", () => {
  it("HTTP batches are flushed on beforeExit, with the bearer token, and the process exits promptly", async () => {
    batches = [];
    const r = await run(["--import", REGISTER, "esm-app.mjs"], { BRIEFCASE_EXPORTER: collectorUrl, BRIEFCASE_EXPORTER_TOKEN: "tok-p" });
    expect(r.code).toBe(0);
    expect(r.ms).toBeLessThan(4000); // the 5 s flush timer is unref'd and does not hold the process
    expect(batches).toHaveLength(1);
    expect(batches[0]!.auth).toBe("Bearer tok-p");
    expect(batches[0]!.body.records).toHaveLength(1);
    expect(r.stderr).not.toContain("tok-p");
  });

  it("SIGTERM flushes queued records, then the process dies of SIGTERM as it would have", async () => {
    batches = [];
    const r = await run(["--import", REGISTER, "signal-app.mjs"], { BRIEFCASE_EXPORTER: collectorUrl }, (out, kill) => {
      if (out.includes("ready")) kill("SIGTERM");
    });
    expect(r.signal).toBe("SIGTERM");
    expect(batches.flatMap((b) => b.body.records)).toHaveLength(1);
  });

  it("process.exit() with an HTTP destination says what was lost", async () => {
    batches = [];
    const r = await run(["--import", REGISTER, "exit-app.mjs"], { BRIEFCASE_EXPORTER: collectorUrl });
    expect(r.code).toBe(0);
    expect(r.stderr).toContain("were not sent");
  });

  it("explicit API: observe() + auto() + shutdown() from the application", async () => {
    batches = [];
    const r = await run(["explicit-api.mjs"], { BRIEFCASE_EXPORTER: collectorUrl });
    expect(r.code, r.stderr).toBe(0);
    expect(batches.flatMap((b) => b.body.records)).toHaveLength(1);
  });

  it("OTLP/HTTP JSON from OTEL_EXPORTER_OTLP_ENDPOINT", async () => {
    batches = [];
    const base = collectorUrl.replace("/v1/decisions", "");
    const received: string[] = [];
    const otlp = createServer((req, res) => {
      received.push(req.url ?? "");
      req.resume();
      req.on("end", () => res.writeHead(200, { "content-type": "application/json" }).end("{}"));
    });
    await new Promise<void>((r) => otlp.listen(0, "127.0.0.1", r));
    const endpoint = `http://127.0.0.1:${(otlp.address() as AddressInfo).port}`;
    const r = await run(["--import", REGISTER, "esm-app.mjs"], { OTEL_EXPORTER_OTLP_ENDPOINT: endpoint, OTEL_SERVICE_NAME: "claims" });
    otlp.close();
    expect(r.code).toBe(0);
    expect(received).toEqual(["/v1/traces"]);
    expect(base).toContain("127.0.0.1");
  });
});

describe("module resolution", () => {
  it("the exports-map fallback resolves every patched specifier to the file Node's import resolves to", async () => {
    const script = `
      import { createRequire } from "node:module";
      import { fileURLToPath } from "node:url";
      import { resolveEsmFromExports } from "${join(PKG, "dist", "instrument", "core.js").replaceAll("\\", "/")}";
      const require = createRequire(import.meta.url);
      const specs = ["openai/resources/chat/completions/completions", "openai/resources/responses/responses",
        "openai/resources/completions", "openai/resources/embeddings", "@anthropic-ai/sdk/resources/messages/messages",
        "@anthropic-ai/sdk/resources/beta/messages/messages", "@google/genai", "@aws-sdk/client-bedrock-runtime"];
      const out = specs.map((s) => [s, fileURLToPath(import.meta.resolve(s)) === resolveEsmFromExports(s, require.resolve(s))]);
      console.log(JSON.stringify(out));
    `;
    const file = join(APPS, "resolve-check.mjs");
    writeFileSync(file, script);
    const r = await run([file]);
    const results = JSON.parse(r.stdout) as [string, boolean][];
    expect(results.filter(([, ok]) => !ok)).toEqual([]);
  });
});

