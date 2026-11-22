/**
 * Worker threads, with real `worker_threads` in child processes running the
 * compiled package (dist/). Provider calls use an in-process fake `fetch`.
 */

import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { beforeAll, describe, expect, it } from "vitest";

const PKG = fileURLToPath(new URL("..", import.meta.url));
const DIR = join(PKG, ".test-apps", "workers");
const REGISTER = join(PKG, "dist", "register.js");
const INDEX = join(PKG, "dist", "index.js").replaceAll("\\", "/");

const CALL = `
import OpenAI from "openai";
const fakeFetch = async () => new Response(JSON.stringify({
  id: "chatcmpl-w", object: "chat.completion", created: 1, model: "gpt-4o-mini",
  choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
}), { status: 200, headers: { "content-type": "application/json" } });
const call = () => new OpenAI({ apiKey: "sk-test", fetch: fakeFetch, maxRetries: 0 }).chat.completions.create({ model: "gpt-4o-mini", messages: [] });
`;

const APPS: Record<string, string> = {
  // Main thread: three workers that end on their own, one that is terminated right after its call.
  "main.mjs": `import { Worker } from "node:worker_threads";
const worker = (mode) => new Promise((resolve) => {
  const w = new Worker(new URL("./worker.mjs", import.meta.url), { workerData: { mode } });
  w.on("message", () => w.terminate());
  w.on("exit", resolve);
});
await Promise.all([worker("natural"), worker("natural"), worker("natural"), worker("terminate")]);
console.log("main done");
`,
  "worker.mjs": `import { parentPort, workerData } from "node:worker_threads";
${CALL}
await call();
if (workerData.mode === "terminate") { parentPort.postMessage("called"); setInterval(() => {}, 1000); }
`,
  // No instrumentation in the main thread; the worker instruments itself and is terminated.
  "main-plain.mjs": `import { Worker } from "node:worker_threads";
const w = new Worker(new URL("./worker-self.mjs", import.meta.url), { workerData: { dest: process.env.DEST } });
w.on("message", () => w.terminate());
w.on("exit", () => console.log("main done"));
`,
  "worker-self.mjs": `import { parentPort, workerData } from "node:worker_threads";
import { observe, auto } from "${INDEX}";
observe(workerData.dest || undefined);
auto();
${CALL}
await call();
parentPort.postMessage("called");
setInterval(() => {}, 1000);
`,
  // Workflow context: AsyncLocalStorage does not cross threads; pass the id in workerData.
  "main-workflow.mjs": `import { Worker } from "node:worker_threads";
import { withWorkflow, currentWorkflow } from "${INDEX}";
${CALL}
const run = (data) => new Promise((resolve) => new Worker(new URL("./worker-workflow.mjs", import.meta.url), { workerData: data }).on("exit", resolve));
await withWorkflow("claim-42", async () => {
  await call();
  await run({ workflowId: currentWorkflow().workflowId });
  await run({});
}, { workflowId: "wf_claim42" });
`,
  "worker-workflow.mjs": `import { workerData } from "node:worker_threads";
import { withWorkflow } from "${INDEX}";
${CALL}
if (workerData.workflowId) await withWorkflow("claim-42", call, { workflowId: workerData.workflowId });
else await call();
`,
  // shutdown() in the main thread while a worker keeps running.
  "main-shutdown.mjs": `import { Worker } from "node:worker_threads";
import { shutdown } from "${INDEX}";
const w = new Worker(new URL("./worker-forever.mjs", import.meta.url));
await new Promise((r) => w.once("message", r));
const t0 = Date.now();
await shutdown();
console.log("shutdown ms", Date.now() - t0);
await w.terminate();
`,
  "worker-forever.mjs": `import { parentPort } from "node:worker_threads";
${CALL}
await call();
parentPort.postMessage("called");
setInterval(() => {}, 1000);
`,
};

interface Run {
  code: number | null;
  stdout: string;
  stderr: string;
  records: Record<string, unknown>[];
}

function run(args: string[], env: Record<string, string> = {}): Promise<Run> {
  return new Promise((resolve) => {
    const clean: Record<string, string | undefined> = { ...process.env };
    for (const key of Object.keys(clean)) if (key.startsWith("BRIEFCASE_") || key.startsWith("OTEL_") || key === "NODE_OPTIONS") delete clean[key];
    const child = spawn(process.execPath, args, { cwd: DIR, env: { ...clean, ...env } });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    child.on("close", (code) => {
      const records = stderr.split("\n").filter((l) => l.startsWith("{")).map((l) => JSON.parse(l) as Record<string, unknown>);
      resolve({ code, stdout, stderr, records });
    });
  });
}

beforeAll(() => {
  mkdirSync(DIR, { recursive: true });
  for (const [name, source] of Object.entries(APPS)) writeFileSync(join(DIR, name), source);
});

describe("worker threads", () => {
  it("--import applies to workers; each worker's records reach the main thread's destination, also from a terminated worker", async () => {
    for (let i = 0; i < 3; i++) {
      const r = await run(["--import", REGISTER, "main.mjs"]);
      expect(r.code, r.stderr).toBe(0);
      expect(r.stdout).toContain("main done");
      expect(r.records, r.stderr).toHaveLength(4);
      expect(new Set(r.records.map((x) => x["decision_id"])).size).toBe(4);
    }
  });

  it("NODE_OPTIONS with BRIEFCASE_AUTO=1 applies to workers too", async () => {
    const r = await run(["main.mjs"], { NODE_OPTIONS: `--import=${REGISTER}`, BRIEFCASE_AUTO: "1" });
    expect(r.code, r.stderr).toBe(0);
    expect(r.records).toHaveLength(4);
  });

  it("an HTTP destination: worker records are sent by the main thread", async () => {
    const { createServer } = await import("node:http");
    const got: unknown[] = [];
    const server = createServer((req, res) => {
      let body = "";
      req.on("data", (c: Buffer) => (body += c.toString()));
      req.on("end", () => {
        got.push(...(JSON.parse(body) as { records: unknown[] }).records);
        res.writeHead(200).end();
      });
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    const port = (server.address() as { port: number }).port;
    const r = await run(["--import", REGISTER, "main.mjs"], { BRIEFCASE_EXPORTER: `http://127.0.0.1:${port}/v1/decisions` });
    server.close();
    expect(r.code, r.stderr).toBe(0);
    expect(got).toHaveLength(4);
  });

  it("a worker that instruments itself (main thread not observing): JSONL and console records survive terminate()", async () => {
    const file = join(mkdtempSync(join(tmpdir(), "bc-worker-")), "runs.jsonl");
    const jsonl = await run(["main-plain.mjs"], { DEST: file });
    expect(jsonl.code, jsonl.stderr).toBe(0);
    expect(readFileSync(file, "utf8").trim().split("\n")).toHaveLength(1);
    const consoleRun = await run(["main-plain.mjs"]);
    expect(consoleRun.code, consoleRun.stderr).toBe(0);
    expect(consoleRun.records).toHaveLength(1);
  });

  it("workflow context does not cross threads by itself; passing the id in workerData carries it", async () => {
    const r = await run(["--import", REGISTER, "main-workflow.mjs"]);
    expect(r.code, r.stderr).toBe(0);
    expect(r.records).toHaveLength(3);
    expect(r.records.filter((x) => x["workflow_id"] === "wf_claim42")).toHaveLength(2);
    expect(r.records.filter((x) => x["workflow_id"] === undefined)).toHaveLength(1);
  });

  it("the main thread's shutdown() does not wait for running workers", async () => {
    const r = await run(["--import", REGISTER, "main-shutdown.mjs"]);
    expect(r.code, r.stderr).toBe(0);
    const ms = Number(/shutdown ms (\d+)/.exec(r.stdout)?.[1]);
    expect(ms).toBeLessThan(500);
    expect(r.records).toHaveLength(1);
  });
});
