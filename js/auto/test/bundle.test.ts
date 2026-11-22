/**
 * Bundled applications (esbuild), run end to end as child processes against
 * a local HTTP server that answers like the providers. The SDKs make real
 * HTTP requests through Node's global fetch, so request detection sees them.
 */

import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { createServer as createHttp2Server, type Http2Server } from "node:http2";
import type { AddressInfo } from "node:net";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { SDK_PACKAGES, briefcaseAuto } from "../src/esbuild.js";

const PKG = fileURLToPath(new URL("..", import.meta.url));
const DIR = join(PKG, ".test-apps", "bundles");
const REGISTER = join(PKG, "dist", "register.js");
const DIST_INDEX = join(PKG, "dist", "index.js").replaceAll("\\", "/");

const CALLS: Record<string, { imports: string; client: string; call: string }> = {
  openai: {
    imports: `import OpenAI from "openai";`,
    client: `new OpenAI({ apiKey: "sk-test", baseURL: process.env.BASE_URL + "/v1", maxRetries: 0 })`,
    call: `client.chat.completions.create({ model: "gpt-4o-mini", messages: [] })`,
  },
  anthropic: {
    imports: `import Anthropic from "@anthropic-ai/sdk";`,
    client: `new Anthropic({ apiKey: "sk-test", baseURL: process.env.BASE_URL, maxRetries: 0 })`,
    call: `client.messages.create({ model: "claude-x", max_tokens: 5, messages: [{ role: "user", content: "hi" }] })`,
  },
  bedrock: {
    imports: `import { BedrockRuntimeClient, ConverseCommand } from "@aws-sdk/client-bedrock-runtime";`,
    client: `new BedrockRuntimeClient({ region: "us-east-1", endpoint: process.env.H2_URL, maxAttempts: 1, credentials: { accessKeyId: "a", secretAccessKey: "b" } })`,
    call: `client.send(new ConverseCommand({ modelId: "m", messages: [{ role: "user", content: [{ text: "hi" }] }] })).finally(() => client.destroy())`,
  },
  genai: {
    imports: `import { GoogleGenAI } from "@google/genai";`,
    client: `new GoogleGenAI({ apiKey: "k", httpOptions: { baseUrl: process.env.BASE_URL, retryOptions: { attempts: 1 } } })`,
    call: `client.models.generateContent({ model: "gemini-2.5-flash", contents: "hi" })`,
  },
};

/** App source; `prelude` / `after` run around the client's creation (no top-level await: CJS output). */
function app(sdk: string, prelude = "", after = ""): string {
  const c = CALLS[sdk]!;
  return `${c.imports}
${prelude}
const client = ${c.client};
${after}
${c.call}.then(async () => { console.log("app done"); if (globalThis.__done) await globalThis.__done(); });
`;
}

let server: Server;
let base = "";
let h2: Http2Server;
let h2Url = "";

function respond(url: string): unknown {
  if (url.startsWith("/v1/messages")) {
    return { id: "msg_1", type: "message", role: "assistant", model: "claude-x", content: [{ type: "text", text: "ok" }], stop_reason: "end_turn", usage: { input_tokens: 3, output_tokens: 1 } };
  }
  if (url.includes(":generateContent")) {
    return { candidates: [{ content: { role: "model", parts: [{ text: "ok" }] }, finishReason: "STOP" }], usageMetadata: { promptTokenCount: 1, candidatesTokenCount: 1, totalTokenCount: 2 }, modelVersion: "gemini-2.5-flash" };
  }
  return { id: "chatcmpl-b", object: "chat.completion", created: 1, model: "gpt-4o-mini", choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }], usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 } };
}

interface Run {
  code: number | null;
  stdout: string;
  stderr: string;
  records: Record<string, unknown>[];
  warnings: string[];
}

function run(args: string[]): Promise<Run> {
  return new Promise((resolve) => {
    const env: Record<string, string | undefined> = { ...process.env };
    for (const key of Object.keys(env)) if (key.startsWith("BRIEFCASE_") || key.startsWith("OTEL_") || key === "NODE_OPTIONS") delete env[key];
    env["BASE_URL"] = base;
    env["H2_URL"] = h2Url;
    const child = spawn(process.execPath, args, { cwd: DIR, env });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    child.on("close", (code) => {
      const lines = stderr.split("\n");
      resolve({
        code,
        stdout,
        stderr,
        records: lines.filter((l) => l.startsWith("{")).map((l) => JSON.parse(l) as Record<string, unknown>),
        warnings: lines.filter((l) => l.startsWith("briefcase.auto:")),
      });
    });
  });
}

async function bundle(
  name: string,
  source: string,
  format: "esm" | "cjs",
  options: { plugin?: boolean; external?: string[]; mainFields?: string[] } = {},
): Promise<string> {
  const entry = join(DIR, `${name}.src.mjs`);
  writeFileSync(entry, source);
  const outfile = join(DIR, `${name}.${format === "esm" ? "mjs" : "cjs"}`);
  await build({
    entryPoints: [entry],
    bundle: true,
    platform: "node",
    format,
    outfile,
    logLevel: "error",
    plugins: options.plugin ? [briefcaseAuto()] : [],
    external: options.external ?? [],
    ...(options.mainFields ? { mainFields: options.mainFields } : {}),
    // esbuild's usual recipe for ESM bundles of packages that call require() (@google/genai does).
    ...(format === "esm" ? { banner: { js: `import { createRequire as __cr } from "node:module"; const require = __cr(import.meta.url);` } } : {}),
  });
  return outfile;
}

beforeAll(async () => {
  mkdirSync(DIR, { recursive: true });
  server = createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      res.writeHead(200, { "content-type": "application/json", "x-request-id": "req_b", "request-id": "req_b" });
      res.end(JSON.stringify(respond(req.url ?? "")));
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  // Bedrock Runtime 3.1175.0 talks HTTP/2 (NodeHttp2Handler); a cleartext HTTP/2 server answers Converse.
  h2 = createHttp2Server((req, res) => {
    req.resume();
    req.on("end", () => {
      res.writeHead(200, { "content-type": "application/json", "x-amzn-requestid": "rid-b" });
      res.end(JSON.stringify({ output: { message: { role: "assistant", content: [{ text: "ok" }] } }, stopReason: "end_turn", usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 } }));
    });
  });
  await new Promise<void>((r) => h2.listen(0, "127.0.0.1", r));
  h2Url = `http://127.0.0.1:${(h2.address() as AddressInfo).port}`;
});

afterAll(() => {
  server.close();
  h2.close();
});

const FORMATS = ["esm", "cjs"] as const;

describe("bundled apps (esbuild)", () => {
  it("SDK inlined, no plugin: nothing can be patched; the app runs, one warning per provider, no record", async () => {
    for (const sdk of ["openai", "anthropic", "bedrock"]) {
      for (const format of FORMATS) {
        const out = await bundle(`plain-${sdk}-${format}`, app(sdk), format);
        const r = await run(["--import", REGISTER, out]);
        expect(r.code, r.stderr).toBe(0);
        expect(r.stdout).toContain("app done");
        expect(r.records, `${sdk} ${format}`).toHaveLength(0);
        expect(r.warnings.filter((w) => w.includes(`through the ${sdk} SDK`))).toHaveLength(1);
      }
    }
  });

  it("SDK inlined with the briefcaseAuto() esbuild plugin + the preload: one record per call", async () => {
    for (const sdk of ["openai", "anthropic", "genai", "bedrock"]) {
      for (const format of FORMATS) {
        const out = await bundle(`plugin-${sdk}-${format}`, app(sdk), format, { plugin: true });
        const r = await run(["--import", REGISTER, out]);
        expect(r.code, r.stderr).toBe(0);
        expect(r.warnings, `${sdk} ${format}`).toEqual([]);
        expect(r.records, `${sdk} ${format}`).toHaveLength(1);
        expect(r.records[0]!["status"]).toBe("ok");
      }
    }
  });

  it("plugin, Bedrock's ES build (picked through the package's `module` field)", async () => {
    const out = await bundle("plugin-bedrock-es", app("bedrock"), "esm", { plugin: true, mainFields: ["module", "main"] });
    const r = await run(["--import", REGISTER, out]);
    expect(r.code, r.stderr).toBe(0);
    expect(r.records).toHaveLength(1);
  });

  it("plugin, with @briefcase-ai/auto bundled too and auto() called after the SDK loaded (no preload)", async () => {
    const prelude = `import { observe, auto, status, shutdown } from "${DIST_INDEX}";
observe(); auto(); globalThis.__done = async () => { console.log(JSON.stringify(status().providers.find((p) => p.name === "openai").patched)); await shutdown(); };`;
    for (const format of FORMATS) {
      const out = await bundle(`plugin-inapp-${format}`, app("openai", prelude), format, { plugin: true });
      const r = await run([out]);
      expect(r.code, r.stderr).toBe(0);
      expect(r.records, format).toHaveLength(1);
      expect(r.stdout).toContain("bundle-esm openai/resources/chat/completions/completions#Completions.create");
    }
  });

  it("no plugin: instrument(client) patches the bundled SDK copy through the client's prototypes", async () => {
    const prelude = `import { observe, auto, instrument, shutdown } from "${DIST_INDEX}";
observe(); auto(); globalThis.__done = () => shutdown();`;
    for (const sdk of ["openai", "anthropic", "genai", "bedrock"]) {
      for (const format of FORMATS) {
        const before = `const early = ${CALLS[sdk]!.client};`; // created before instrument(): covered too
        const after = `instrument(client); globalThis.__early = early;`;
        const out = await bundle(`instrument-${sdk}-${format}`, app(sdk, `${prelude}\n${before}`, after), format);
        const r = await run([out]);
        expect(r.code, r.stderr).toBe(0);
        expect(r.warnings, `${sdk} ${format}`).toEqual([]);
        expect(r.records, `${sdk} ${format}`).toHaveLength(1);
      }
    }
  });

  it("external recipe: external: SDK_PACKAGES keeps the SDKs in node_modules, where the preload patches them", async () => {
    expect(SDK_PACKAGES).toEqual(["openai", "@anthropic-ai/sdk", "@google/genai", "@aws-sdk/client-bedrock-runtime"]);
    for (const format of FORMATS) {
      const out = await bundle(`external-openai-${format}`, app("openai"), format, { external: SDK_PACKAGES });
      const r = await run(["--import", REGISTER, out]);
      expect(r.code, r.stderr).toBe(0);
      expect(r.records, format).toHaveLength(1);
    }
  });
});
