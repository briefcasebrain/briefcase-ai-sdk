/**
 * `instrument(client)`: patching the SDK copy a client belongs to, without
 * `auto()` finding the package (as for an SDK bundled into the app). Here
 * the clients come from node_modules, with `auto()` not called (or undone).
 */

import { Readable } from "node:stream";

import Anthropic from "@anthropic-ai/sdk";
import { BedrockRuntime, BedrockRuntimeClient, ConverseCommand } from "@aws-sdk/client-bedrock-runtime";
import { GoogleGenAI } from "@google/genai";
import OpenAI from "openai";
import { Completions } from "openai/resources/chat/completions/completions";
import { afterAll, describe, expect, it, vi } from "vitest";

import { auto, instrument, status, undo } from "../src/auto.js";
import { shutdown } from "../src/observe.js";
import { SECRET_KEY, bare, fakeFetch, json, records } from "./helpers.js";

const completion = {
  id: "chatcmpl-1",
  object: "chat.completion",
  created: 1,
  model: "gpt-4o-mini",
  choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
};
const message = { id: "msg_1", type: "message", role: "assistant", model: "claude-x", content: [{ type: "text", text: "ok" }], stop_reason: "end_turn", usage: { input_tokens: 3, output_tokens: 1 } };
const gemini = { candidates: [{ content: { role: "model", parts: [{ text: "ok" }] }, finishReason: "STOP" }], usageMetadata: { promptTokenCount: 1, candidatesTokenCount: 1, totalTokenCount: 2 } };

const openaiClient = () => new OpenAI({ apiKey: SECRET_KEY, fetch: fakeFetch(() => json(completion)), maxRetries: 0 });
const anthropicClient = () => new Anthropic({ apiKey: SECRET_KEY, fetch: fakeFetch(() => json(message)), maxRetries: 0 });
const genaiClient = () => new GoogleGenAI({ apiKey: SECRET_KEY, httpOptions: { fetch: fakeFetch(() => json(gemini)), retryOptions: { attempts: 1 } } });
function bedrockClient<C extends typeof BedrockRuntimeClient | typeof BedrockRuntime>(Ctor: C): InstanceType<C> {
  const handle = async () => ({
    response: {
      statusCode: 200,
      headers: { "content-type": "application/json", "x-amzn-requestid": "rid-1" },
      body: Readable.from([Buffer.from(JSON.stringify({ output: { message: { role: "assistant", content: [{ text: "ok" }] } }, stopReason: "end_turn", usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 } }))]),
    },
  });
  return new Ctor({ region: "us-east-1", credentials: { accessKeyId: "AKIDEXAMPLE", secretAccessKey: "secret" }, requestHandler: { handle }, maxAttempts: 1 }) as InstanceType<C>;
}

const calls: Record<string, () => Promise<unknown>> = {};

afterAll(async () => {
  undo();
  await shutdown();
});

describe("instrument(client)", () => {
  it("openai: patches the client's copy; clients created before and after are covered; no double wrapping", async () => {
    const mem = await bare();
    const early = openaiClient();
    const original = Completions.prototype.create;
    expect(instrument(early)).toBe(early);
    expect(Completions.prototype.create).not.toBe(original);
    instrument(openaiClient()); // same copy again: nothing changes
    instrument(OpenAI); // the class: same prototypes
    const wrapped = Completions.prototype.create;
    await early.chat.completions.create({ model: "gpt-4o-mini", messages: [] });
    await openaiClient().chat.completions.create({ model: "gpt-4o-mini", messages: [] });
    expect(await records(mem, 2)).toHaveLength(2);
    expect(Completions.prototype.create).toBe(wrapped);
    const report = status().providers.find((p) => p.name === "openai")!;
    expect(report.patched.filter((t) => t.startsWith("client-esm "))).toHaveLength(4);
    undo("openai");
    expect(Completions.prototype.create).toBe(original);
  });

  it("openai: a copy auto() already patched is left alone", async () => {
    await bare();
    auto({ only: ["openai"] });
    const wrapped = Completions.prototype.create;
    const before = status().providers.find((p) => p.name === "openai")!.patched.length;
    instrument(openaiClient());
    expect(Completions.prototype.create).toBe(wrapped);
    expect(status().providers.find((p) => p.name === "openai")!.patched).toHaveLength(before);
  });

  it("anthropic, google-genai and bedrock clients (and the Anthropic / BedrockRuntime classes)", async () => {
    const mem = await bare();
    const early = genaiClient();
    instrument(Anthropic);
    instrument(early);
    instrument(BedrockRuntime);
    calls["anthropic"] = () => anthropicClient().messages.create({ model: "claude-x", max_tokens: 5, messages: [{ role: "user", content: "hi" }] });
    calls["genai-early"] = () => early.models.generateContent({ model: "gemini-2.5-flash", contents: "hi" });
    calls["genai-late"] = () => genaiClient().models.generateContent({ model: "gemini-2.5-flash", contents: "hi" });
    calls["bedrock"] = () => bedrockClient(BedrockRuntimeClient).send(new ConverseCommand({ modelId: "m", messages: [{ role: "user", content: [{ text: "hi" }] }] }));
    calls["bedrock-aggregated"] = () => bedrockClient(BedrockRuntime).converse({ modelId: "m", messages: [{ role: "user", content: [{ text: "hi" }] }] });
    for (const fn of Object.values(calls)) await fn();
    const all = await records(mem, 5);
    expect(all.map((r) => r.decision_type)).toEqual([
      "llm.anthropic.messages",
      "llm.google_genai.generate_content",
      "llm.google_genai.generate_content",
      "llm.bedrock.converse",
      "llm.bedrock.converse",
    ]);
  });

  it("anything else: returned unchanged, one warning, never throws", async () => {
    await bare();
    const lines: string[] = [];
    const stderr = vi.spyOn(process.stderr, "write").mockImplementation((chunk: unknown) => {
      lines.push(String(chunk));
      return true;
    });
    const thing = { chat: {} };
    expect(instrument(thing)).toBe(thing);
    expect(instrument(null)).toBe(null);
    instrument(42);
    stderr.mockRestore();
    expect(lines.filter((l) => l.includes("instrument()"))).toHaveLength(1);
  });
});
