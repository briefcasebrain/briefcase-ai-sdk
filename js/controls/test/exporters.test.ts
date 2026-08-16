import { mkdtempSync, readFileSync, statSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { ConsoleExporter, JsonlFileExporter, MemoryExporter } from "../src/exporters.js";

const onWindows = process.platform === "win32";
const dirs: string[] = [];

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "briefcase-controls-"));
  dirs.push(dir);
  return dir;
}

afterEach(() => {
  while (dirs.length > 0) rmSync(dirs.pop()!, { recursive: true, force: true });
});

describe("MemoryExporter", () => {
  it("collects records and clears them", async () => {
    const mem = new MemoryExporter();
    await mem.export({ decision_id: "1" });
    await mem.export({ decision_id: "2" });
    expect(mem.records).toHaveLength(2);
    mem.clear();
    expect(mem.records).toHaveLength(0);
  });

  it("keeps records after close", async () => {
    const mem = new MemoryExporter();
    await mem.export({ decision_id: "1" });
    await mem.close();
    expect(mem.records).toHaveLength(1);
  });
});

describe("ConsoleExporter", () => {
  it("writes one JSON line per record to the stream", async () => {
    const lines: string[] = [];
    const exporter = new ConsoleExporter({ write: (s: string) => lines.push(s) });
    expect(await exporter.export({ decision_id: "1" })).toBe(true);
    expect(lines).toEqual(['{"decision_id":"1"}\n']);
  });

  it("pretty-prints when asked", async () => {
    const lines: string[] = [];
    const exporter = new ConsoleExporter({ write: (s: string) => lines.push(s) }, { pretty: true });
    await exporter.export({ decision_id: "1" });
    expect(lines[0]).toContain("\n  ");
  });
});

describe("JsonlFileExporter", () => {
  it("appends JSON lines and reopens after close", async () => {
    const path = join(tempDir(), "runs.jsonl");
    const exporter = new JsonlFileExporter(path);
    await exporter.export({ decision_id: "1" });
    await exporter.close();
    await exporter.export({ decision_id: "2" });
    await exporter.close();
    expect(readFileSync(path, "utf8")).toBe('{"decision_id":"1"}\n{"decision_id":"2"}\n');
  });

  it("creates missing parent directories", async () => {
    const path = join(tempDir(), "nested", "deep", "runs.jsonl");
    const exporter = new JsonlFileExporter(path);
    await exporter.export({ decision_id: "1" });
    await exporter.close();
    expect(existsSync(path)).toBe(true);
  });

  it.skipIf(onWindows)("creates parent directories with mode 0700", async () => {
    const base = tempDir();
    const path = join(base, "nested", "deep", "runs.jsonl");
    const exporter = new JsonlFileExporter(path);
    await exporter.export({ decision_id: "1" });
    await exporter.close();
    expect(statSync(join(base, "nested")).mode & 0o777).toBe(0o700);
    expect(statSync(join(base, "nested", "deep")).mode & 0o777).toBe(0o700);
  });

  it.skipIf(onWindows)("opens the file owner-only (0600)", async () => {
    const path = join(tempDir(), "runs.jsonl");
    const exporter = new JsonlFileExporter(path);
    await exporter.export({ decision_id: "1" });
    await exporter.close();
    expect(statSync(path).mode & 0o777).toBe(0o600);
  });

  it.skipIf(onWindows)("tightens a pre-existing file to 0600", async () => {
    const path = join(tempDir(), "runs.jsonl");
    writeFileSync(path, "", { mode: 0o644 });
    expect(statSync(path).mode & 0o777).toBe(0o644);
    const exporter = new JsonlFileExporter(path);
    await exporter.export({ decision_id: "1" });
    await exporter.close();
    expect(statSync(path).mode & 0o777).toBe(0o600);
  });
});
