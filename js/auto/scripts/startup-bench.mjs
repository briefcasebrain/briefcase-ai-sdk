#!/usr/bin/env node
/**
 * Cold-start cost of `--import @briefcase-ai/auto/register`.
 *
 *     npm run build && node scripts/startup-bench.mjs [runs=20]
 *
 * Each scenario starts a fresh `node` process (the same binary running this
 * script) `runs` times and reports p50 / p95 wall-clock milliseconds from
 * spawn to exit. Apps are written to `.test-apps/bench/` inside this package,
 * so the SDKs resolve from its node_modules.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const runs = Number(process.argv[2] ?? 20);
const pkg = fileURLToPath(new URL("..", import.meta.url));
const dir = join(pkg, ".test-apps", "bench");
mkdirSync(dir, { recursive: true });
const register = join(pkg, "dist", "register.js");
const PROVIDERS = ["openai", "anthropic", "google-genai", "bedrock"];
const IMPORTS = {
  openai: `import OpenAI from "openai";`,
  anthropic: `import Anthropic from "@anthropic-ai/sdk";`,
  "google-genai": `import { GoogleGenAI } from "@google/genai";`,
  bedrock: `import { BedrockRuntimeClient } from "@aws-sdk/client-bedrock-runtime";`,
};
writeFileSync(join(dir, "trivial.mjs"), `console.log("ok");\n`);
for (const [name, line] of Object.entries(IMPORTS)) writeFileSync(join(dir, `uses-${name}.mjs`), `${line}\nconsole.log("ok");\n`);
writeFileSync(join(dir, "uses-all.mjs"), `${Object.values(IMPORTS).join("\n")}\nconsole.log("ok");\n`);

function time(args, env = {}) {
  const clean = { ...process.env };
  for (const k of Object.keys(clean)) if (k.startsWith("BRIEFCASE_") || k === "NODE_OPTIONS") delete clean[k];
  const samples = [];
  spawnSync(process.execPath, args, { cwd: dir, env: { ...clean, ...env } }); // warm the file cache
  for (let i = 0; i < runs; i++) {
    const t0 = process.hrtime.bigint();
    const r = spawnSync(process.execPath, args, { cwd: dir, env: { ...clean, ...env }, encoding: "utf8" });
    samples.push(Number(process.hrtime.bigint() - t0) / 1e6);
    if (r.status !== 0) throw new Error(`${args.join(" ")} failed: ${r.stderr}`);
  }
  samples.sort((a, b) => a - b);
  const q = (p) => samples[Math.min(samples.length - 1, Math.ceil((p / 100) * samples.length) - 1)];
  return { p50: q(50), p95: q(95) };
}

const rows = [];
const row = (label, args, env) => {
  const r = time(args, env);
  rows.push([label, r.p50.toFixed(1), r.p95.toFixed(1)]);
  process.stderr.write(".");
};
const extra = (process.env.BENCH_FLAGS ?? "").split(" ").filter(Boolean);
row("node trivial.mjs (no preload)", ["trivial.mjs"]);
row("--import register, trivial.mjs (all 4 SDKs installed)", ["--import", register, ...extra, "trivial.mjs"]);
for (const p of PROVIDERS) {
  const skip = PROVIDERS.filter((x) => x !== p).join(",");
  row(`--import register, trivial.mjs, only ${p}`, ["--import", register, ...extra, "trivial.mjs"], { BRIEFCASE_AUTO_SKIP: skip });
}
for (const p of [...PROVIDERS, "all"]) {
  row(`node uses-${p}.mjs (no preload)`, [`uses-${p}.mjs`]);
  row(`--import register, uses-${p}.mjs`, ["--import", register, ...extra, `uses-${p}.mjs`]);
}
process.stderr.write("\n");
console.log(`node ${process.version}, ${runs} runs per row (ms, spawn to exit)\n`);
console.log("| scenario | p50 | p95 |\n|---|---|---|");
for (const r of rows) console.log(`| ${r.join(" | ")} |`);
