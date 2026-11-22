import { ApiError, GoogleGenAI, Models } from "@google/genai";
import { afterAll, describe, expect, it } from "vitest";

import { undo } from "../src/auto.js";
import { shutdown } from "../src/observe.js";
import { SECRET_KEY, assertNoSecrets, fakeFetch, fresh, json, records, sse } from "./helpers.js";

function response(text: string, finish: string | undefined, usage?: Record<string, number>) {
  return {
    candidates: [{ content: { role: "model", parts: [{ text }] }, ...(finish ? { finishReason: finish } : {}), index: 0 }],
    ...(usage ? { usageMetadata: usage } : {}),
    modelVersion: "gemini-2.5-flash",
    responseId: "resp-g-1",
  };
}

const usage = { promptTokenCount: 10, candidatesTokenCount: 2, totalTokenCount: 12, thoughtsTokenCount: 3 };

function server(options: { hang?: boolean } = {}) {
  return fakeFetch(({ url, init }) => {
    if (url.includes(":streamGenerateContent")) {
      const chunks = [response("Appro", undefined), response("ved.", "STOP", usage)];
      return sse(options.hang ? chunks.slice(0, 1) : chunks, {}, { ...options, signal: init.signal });
    }
    return json(response("Approved.", "STOP", usage));
  });
}

function client(fetchImpl: typeof fetch) {
  return new GoogleGenAI({ apiKey: SECRET_KEY, httpOptions: { fetch: fetchImpl, retryOptions: { attempts: 1 } } });
}

const params = { model: "gemini-2.5-flash", contents: "Is claim 123 covered?", config: { temperature: 0.1, maxOutputTokens: 64 } };

afterAll(async () => {
  undo();
  await shutdown();
});

describe("@google/genai models", () => {
  it("generateContent: one record with Python's field names", async () => {
    const mem = await fresh({ captureContent: true });
    const result = await client(server()).models.generateContent(params);
    expect(result.text).toBe("Approved.");
    const [record, ...rest] = await records(mem, 1);
    expect(rest).toHaveLength(0);
    expect(record).toMatchObject({
      decision_type: "llm.google_genai.generate_content",
      function_name: "google.genai.models.generate_content",
      status: "ok",
      model_parameters: { provider: "google-genai", model_name: "gemini-2.5-flash", temperature: 0.1, max_output_tokens: 64 },
      token_usage: { prompt_tokens: 10, completion_tokens: 2, total_tokens: 12, thoughts_token_count: 3 },
      finish_reason: "STOP",
      response_id: "resp-g-1",
      inputs: { contents: "Is claim 123 covered?" },
      outputs: { content: "Approved." },
      stream: false,
    });
    assertNoSecrets(mem.records);
  });

  it("the instance property keeps the SDK's own shape", async () => {
    await fresh();
    const ai = client(server());
    const desc = Object.getOwnPropertyDescriptor(ai.models, "generateContent")!;
    expect(desc).toMatchObject({ writable: true, enumerable: true, configurable: true });
    expect(ai.models).toBeInstanceOf(Models);
  });

  it("generateContentStream: same generator object, one record at the end", async () => {
    undo();
    const bare = await client(server()).models.generateContentStream(params);
    const mem = await fresh();
    const stream = await client(server()).models.generateContentStream(params);
    // Same kind of object the SDK returns (its own downlevelled async generator), iterating itself.
    expect(Object.getPrototypeOf(stream)).toBe(Object.getPrototypeOf(bare));
    expect(stream[Symbol.asyncIterator]()).toBe(stream);
    let text = "";
    for await (const chunk of stream) text += chunk.text ?? "";
    expect(text).toBe("Approved.");
    const [record, ...rest] = await records(mem, 1);
    expect(rest).toHaveLength(0);
    expect(record).toMatchObject({
      function_name: "google.genai.models.generate_content_stream",
      stream: true,
      outputs: { chunk_count: 2, stream_completed: true },
      token_usage: { prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 },
      finish_reason: "STOP",
    });
  });

  it("an abandoned stream records once, incomplete", async () => {
    const mem = await fresh();
    const stream = await client(server()).models.generateContentStream(params);
    for await (const _ of stream) break;
    const all = await records(mem, 1);
    expect(all).toHaveLength(1);
    expect(all[0]!.outputs).toMatchObject({ chunk_count: 1, stream_completed: false });
  });

  it("an aborted stream records once, cancelled", async () => {
    const mem = await fresh();
    const abort = new AbortController();
    const stream = await client(server({ hang: true })).models.generateContentStream({
      ...params,
      config: { ...params.config, abortSignal: abort.signal },
    });
    const error = await (async () => {
      for await (const _ of stream) abort.abort();
    })().catch((e: unknown) => e);
    expect(error).toBeDefined();
    const all = await records(mem, 1);
    expect(all).toHaveLength(1);
    expect(all[0]).toMatchObject({ status: "cancelled", outputs: { stream_completed: false } });
  });

  it("chats.sendMessage goes through the instrumented Models instance", async () => {
    const mem = await fresh();
    const chat = client(server()).chats.create({ model: "gemini-2.5-flash" });
    await chat.sendMessage({ message: "hello" });
    expect(await records(mem, 1)).toHaveLength(1);
  });

  it("errors propagate unchanged", async () => {
    const mem = await fresh();
    const failing = fakeFetch(() => json({ error: { code: 429, message: "Resource exhausted", status: "RESOURCE_EXHAUSTED" } }, {}, 429));
    const error = await client(failing).models.generateContent(params).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(429);
    const [record] = await records(mem, 1);
    expect(record).toMatchObject({ status: "error", error_type: "ApiError", http_status: 429, token_usage: null });
  });

  it("clients created before auto() are not instrumented (documented limit)", async () => {
    undo();
    const early = client(server());
    const mem = await fresh();
    await early.models.generateContent(params);
    expect(await records(mem)).toHaveLength(0);
  });
});
