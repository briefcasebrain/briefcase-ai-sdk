import { createRequire } from "node:module";

import OpenAI from "openai";
import { Completions } from "openai/resources/chat/completions/completions";
import { afterAll, afterEach, describe, expect, it, vi } from "vitest";

import { auto, setCopyLoaderForTests, status, undo } from "../src/auto.js";
import { WRAPPED } from "../src/instrument/core.js";
import { shutdown } from "../src/observe.js";
import { SECRET_KEY, fakeFetch, fresh, json, records } from "./helpers.js";

const require = createRequire(import.meta.url);

const completion = {
  id: "chatcmpl-1",
  object: "chat.completion",
  created: 1,
  model: "gpt-4o-mini",
  choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
};

afterEach(() => setCopyLoaderForTests(null));
afterAll(async () => {
  undo();
  await shutdown();
});

describe("patching", () => {
  it("patches both the ESM and the CommonJS copy of a dual package", async () => {
    const mem = await fresh({ only: ["openai"] });
    const report = status().providers.find((p) => p.name === "openai")!;
    expect(report.state).toBe("patched");
    expect(report.sdkVersion).toBe("7.62.0");
    expect(report.patched.filter((t) => t.startsWith("esm "))).toHaveLength(4);
    expect(report.patched.filter((t) => t.startsWith("cjs "))).toHaveLength(4);

    const CjsOpenAI = require("openai") as typeof OpenAI;
    expect(CjsOpenAI).not.toBe(OpenAI); // separate module instances
    const fetchImpl = fakeFetch(() => json(completion));
    for (const Ctor of [OpenAI, CjsOpenAI]) {
      await new Ctor({ apiKey: SECRET_KEY, fetch: fetchImpl, maxRetries: 0 }).chat.completions.create({ model: "gpt-4o-mini", messages: [] });
    }
    expect(await records(mem, 2)).toHaveLength(2);
  });

  it("a second auto() does not wrap anything twice", async () => {
    const mem = await fresh({ only: ["openai"] });
    const first = Completions.prototype.create;
    auto();
    auto({ only: ["openai"] });
    expect(Completions.prototype.create).toBe(first);
    expect((first as unknown as Record<symbol, unknown>)[WRAPPED]).toBeTypeOf("function");
    await new OpenAI({ apiKey: SECRET_KEY, fetch: fakeFetch(() => json(completion)), maxRetries: 0 }).chat.completions.create({
      model: "gpt-4o-mini",
      messages: [],
    });
    expect(await records(mem, 1)).toHaveLength(1);
  });

  it("undo() restores the original functions", async () => {
    undo();
    const original = Completions.prototype.create;
    await fresh({ only: ["openai"] });
    expect(Completions.prototype.create).not.toBe(original);
    const result = undo("openai");
    expect(Completions.prototype.create).toBe(original);
    expect(result.providers.find((p) => p.name === "openai")!.state).toBe("undone");
  });

  it("undo() refuses to clobber a patch someone else installed after us", async () => {
    const mem = await fresh({ only: ["openai"] });
    const ours = Completions.prototype.create;
    const theirs = function (this: unknown, ...args: unknown[]) {
      return (ours as (...a: unknown[]) => unknown).apply(this, args);
    };
    Completions.prototype.create = theirs as typeof Completions.prototype.create;
    const stderr = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const result = undo("openai");
    stderr.mockRestore();
    expect(Completions.prototype.create).toBe(theirs); // left in place
    const report = result.providers.find((p) => p.name === "openai")!;
    expect(report.refusedUndo.some((t) => t.includes("chat/completions"))).toBe(true);
    // Our wrapper underneath now passes calls straight through.
    await new OpenAI({ apiKey: SECRET_KEY, fetch: fakeFetch(() => json(completion)), maxRetries: 0 }).chat.completions.create({
      model: "gpt-4o-mini",
      messages: [],
    });
    expect(await records(mem)).toHaveLength(0);
    // Restore the real original for the next tests.
    Completions.prototype.create = (ours as unknown as Record<symbol, typeof Completions.prototype.create>)[WRAPPED]!;
  });

  it("a reshaped SDK is skipped with one warning and never half-patched", async () => {
    undo();
    class Completions2 {
      create() {
        return "chat";
      }
    }
    class Responses2 {} // create() is missing in this "future" layout
    class Embeddings2 {
      create() {
        return "embeddings";
      }
    }
    const chatCreate = Completions2.prototype.create;
    setCopyLoaderForTests((specifier) => {
      if (!specifier.startsWith("openai/")) return null;
      const ns: Record<string, unknown> = specifier.endsWith("chat/completions/completions")
        ? { Completions: Completions2 }
        : specifier.endsWith("responses/responses")
          ? { Responses: Responses2 }
          : specifier.endsWith("embeddings")
            ? { Embeddings: Embeddings2 }
            : { Completions: class {
                create() {
                  return "legacy";
                }
              } };
      return { copies: [{ format: "esm", path: "/fake/node_modules/openai/x.mjs", ns }], problems: [] };
    });
    const lines: string[] = [];
    const stderr = vi.spyOn(process.stderr, "write").mockImplementation((chunk: unknown) => {
      lines.push(String(chunk));
      return true;
    });
    await fresh({ only: ["openai"] });
    stderr.mockRestore();
    const warnings = lines.filter((l) => l.includes("openai"));
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain("Responses.create: create not found");
    expect(Completions2.prototype.create).toBe(chatCreate); // untouched
    const report = status().providers.find((p) => p.name === "openai")!;
    expect(report.state).toBe("skipped");
    expect(report.patched).toEqual([]);
    expect(report.skipped.some((s) => s.reason === "create not found")).toBe(true);
  });

  it("a missing SDK is reported as not installed, silently", async () => {
    undo();
    setCopyLoaderForTests(() => null);
    const lines: string[] = [];
    const stderr = vi.spyOn(process.stderr, "write").mockImplementation((chunk: unknown) => {
      lines.push(String(chunk));
      return true;
    });
    await fresh();
    stderr.mockRestore();
    expect(lines).toEqual([]);
    expect(status().providers.map((p) => p.state)).toEqual(["not-installed", "not-installed", "not-installed", "not-installed"]);
  });

  it("without observe() the wrappers add no work and record nothing", async () => {
    await fresh({ only: ["openai"] });
    await shutdown();
    const fetchImpl = fakeFetch(() => json(completion));
    const result = await new OpenAI({ apiKey: SECRET_KEY, fetch: fetchImpl, maxRetries: 0 }).chat.completions.create({
      model: "gpt-4o-mini",
      messages: [],
    });
    expect(result.id).toBe("chatcmpl-1");
    expect(status().observing).toBe(false);
  });

  it("rejects unknown provider names", () => {
    expect(() => auto({ only: ["mistral"] })).toThrow(/Unknown provider/);
    expect(() => undo("mistral")).toThrow(/Unknown provider/);
  });
});
