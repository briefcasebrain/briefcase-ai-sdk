import OpenAI from "openai";
import { APIPromise } from "openai/core/api-promise";
import { Stream } from "openai/core/streaming";
import { afterAll, describe, expect, it } from "vitest";

import { undo } from "../src/auto.js";
import { shutdown } from "../src/observe.js";
import { state } from "../src/state.js";
import { SECRET_KEY, assertNoSecrets, fakeFetch, fresh, json, records, sse } from "./helpers.js";

const MODEL = "gpt-4o-mini-2024-07-18";

const completion = {
  id: "chatcmpl-abc",
  object: "chat.completion",
  created: 1_760_000_000,
  model: MODEL,
  service_tier: "default",
  choices: [{ index: 0, message: { role: "assistant", content: "Approved.", refusal: null }, logprobs: null, finish_reason: "stop" }],
  usage: {
    prompt_tokens: 12,
    completion_tokens: 3,
    total_tokens: 15,
    prompt_tokens_details: { cached_tokens: 4 },
    completion_tokens_details: { reasoning_tokens: 0 },
  },
};

function chunk(content: string | null, finish: string | null, extra: Record<string, unknown> = {}) {
  return {
    id: "chatcmpl-stream",
    object: "chat.completion.chunk",
    created: 1_760_000_000,
    model: MODEL,
    choices:
      content === null && finish === null
        ? []
        : [{ index: 0, delta: content === null ? {} : content === "Ap" ? { role: "assistant", content } : { content }, finish_reason: finish }],
    ...extra,
  };
}

const chatChunks = [
  chunk("Ap", null),
  chunk("proved", null),
  chunk(null, "stop"),
  chunk(null, null, { usage: { prompt_tokens: 12, completion_tokens: 2, total_tokens: 14 } }),
];

function chatServer(options: { hang?: boolean } = {}) {
  return fakeFetch(({ body }) => {
    if (body?.["stream"]) return sse([...chatChunks, "data: [DONE]\n\n"], { "x-request-id": "req_stream_1" }, options);
    return json(completion, { "x-request-id": "req_chat_1" });
  });
}

function client(fetchImpl: typeof fetch) {
  return new OpenAI({ apiKey: SECRET_KEY, fetch: fetchImpl, maxRetries: 0, baseURL: "https://api.openai.test/v1" });
}

afterAll(async () => {
  undo();
  await shutdown();
});

describe("openai chat.completions.create", () => {
  it("emits one record with Python's field names; content off by default", async () => {
    const mem = await fresh();
    const result = await client(chatServer()).chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "Is claim 123 covered?" }],
      temperature: 0.2,
      max_completion_tokens: 50,
    });
    expect(result.choices[0]!.message.content).toBe("Approved.");
    const [record, ...rest] = await records(mem, 1);
    expect(rest).toHaveLength(0);
    expect(record).toMatchObject({
      schema_version: "1.2",
      decision_type: "llm.openai.chat",
      function_name: "openai.chat.completions.create",
      inputs: {},
      outputs: {},
      status: "ok",
      model_parameters: {
        provider: "openai",
        model_name: MODEL,
        requested_model: "gpt-4o-mini",
        temperature: 0.2,
        max_completion_tokens: 50,
      },
      token_usage: { prompt_tokens: 12, completion_tokens: 3, total_tokens: 15, cached_tokens: 4, reasoning_tokens: 0 },
      finish_reason: "stop",
      response_id: "chatcmpl-abc",
      request_id: "req_chat_1",
      stream: false,
      service_tier: "default",
    });
    expect(record!.started_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$/);
    expect(typeof record!.execution_time_ms).toBe("number");
    assertNoSecrets(mem.records);
  });

  it("captures content only when asked, bounded by maxContentChars", async () => {
    const mem = await fresh({ captureContent: true, maxContentChars: 5 });
    await client(chatServer()).chat.completions.create({ model: "gpt-4o-mini", messages: [{ role: "user", content: "Is claim 123 covered?" }] });
    const [record] = await records(mem, 1);
    expect(record!.inputs).toEqual({ messages: [{ role: "user", content: "Is cl..." }] });
    expect(record!.outputs).toEqual({ content: "Appro..." });
  });

  it("hash mode records a digest, never the text", async () => {
    const mem = await fresh({ captureContent: "hash" });
    await client(chatServer()).chat.completions.create({ model: "gpt-4o-mini", messages: [{ role: "user", content: "secret prompt" }] });
    const [record] = await records(mem, 1);
    expect(Object.keys(record!.inputs)).toEqual(["sha256", "chars"]);
    expect(JSON.stringify(record)).not.toContain("secret prompt");
  });

  it("returns the SDK's own APIPromise, with .withResponse() and .asResponse() intact", async () => {
    const mem = await fresh();
    const promise = client(chatServer()).chat.completions.create({ model: "gpt-4o-mini", messages: [{ role: "user", content: "hi" }] });
    expect(promise).toBeInstanceOf(APIPromise);
    const { data, response, request_id } = await promise.withResponse();
    expect(data.id).toBe("chatcmpl-abc");
    expect(response.status).toBe(200);
    expect(request_id).toBe("req_chat_1");

    const raw = await client(chatServer()).chat.completions.create({ model: "gpt-4o-mini", messages: [] }).asResponse();
    // The body was not consumed by us: the application can still read it.
    expect(((await raw.json()) as { id: string }).id).toBe("chatcmpl-abc");
    const all = await records(mem, 1);
    expect(all).toHaveLength(1); // the asResponse() call is recorded later (GC or exit), without usage
    await shutdown();
    expect(mem.records).toHaveLength(2);
    expect(mem.records[1]).toMatchObject({ request_id: "req_chat_1", token_usage: null, status: "ok" });
  });

  it("propagates errors unchanged and records them", async () => {
    const mem = await fresh();
    const failing = fakeFetch(() =>
      json({ error: { message: "Rate limit reached", type: "requests", code: "rate_limit_exceeded" } }, { "x-request-id": "req_429" }, 429),
    );
    const error = await client(failing)
      .chat.completions.create({ model: "gpt-4o-mini", messages: [] })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OpenAI.RateLimitError);
    expect((error as InstanceType<typeof OpenAI.RateLimitError>).status).toBe(429);

    // The same call without instrumentation fails the same way.
    undo();
    const bare = await client(failing)
      .chat.completions.create({ model: "gpt-4o-mini", messages: [] })
      .catch((e: unknown) => e);
    expect((bare as Error).constructor).toBe((error as Error).constructor);
    expect((bare as Error).message).toBe((error as Error).message);

    const [record] = await records(mem, 1);
    expect(record).toMatchObject({
      status: "error",
      error: "RateLimitError",
      error_type: "RateLimitError",
      http_status: 429,
      request_id: "req_429",
      token_usage: null,
    });
    expect(mem.records).toHaveLength(1);
  });

  it("usage unknown is null, not 0", async () => {
    const mem = await fresh();
    const { usage: _drop, ...noUsage } = completion;
    await client(fakeFetch(() => json(noUsage))).chat.completions.create({ model: "gpt-4o-mini", messages: [] });
    const [record] = await records(mem, 1);
    expect(record!.token_usage).toBeNull();
  });

  it("our own failures never surface", async () => {
    const mem = await fresh();
    const pipeline = state().pipeline!;
    pipeline.enqueue = () => {
      throw new Error("exporter exploded");
    };
    const weird = fakeFetch(() => json({ ...completion, usage: "not-an-object", choices: "nope" }));
    const result = await client(weird).chat.completions.create({ model: "gpt-4o-mini", messages: [] });
    expect(result.choices).toBe("nope");
    expect(mem.records).toHaveLength(0);
  });
});

describe("openai streams", () => {
  it("records once when the stream is exhausted; the caller gets the SDK Stream", async () => {
    const mem = await fresh();
    const stream = await client(chatServer()).chat.completions.create({
      model: "gpt-4o-mini",
      messages: [],
      stream: true,
      stream_options: { include_usage: true },
    });
    expect(stream).toBeInstanceOf(Stream);
    expect(stream.controller).toBeInstanceOf(AbortController);
    let text = "";
    for await (const c of stream) text += c.choices[0]?.delta?.content ?? "";
    expect(text).toBe("Approved");
    const [record, ...rest] = await records(mem, 1);
    expect(rest).toHaveLength(0);
    expect(record).toMatchObject({
      stream: true,
      status: "ok",
      outputs: { chunk_count: 4, stream_completed: true },
      token_usage: { prompt_tokens: 12, completion_tokens: 2, total_tokens: 14 },
      finish_reason: "stop",
      request_id: "req_stream_1",
      response_id: "chatcmpl-stream",
    });
    expect(record!.time_to_first_chunk_ms).toBeGreaterThan(0);
  });

  it("streamUsage adds include_usage on a copy of the request body", async () => {
    const mem = await fresh({ streamUsage: true });
    const server = chatServer();
    const body = { model: "gpt-4o-mini", messages: [], stream: true as const };
    const stream = await client(server).chat.completions.create(body);
    for await (const _ of stream) void _;
    expect(server.calls[0]!.body!["stream_options"]).toEqual({ include_usage: true });
    expect(body).not.toHaveProperty("stream_options");
    await records(mem, 1);
  });

  it("a stream the consumer abandons (break) emits exactly one incomplete record", async () => {
    const mem = await fresh();
    const stream = await client(chatServer()).chat.completions.create({ model: "gpt-4o-mini", messages: [], stream: true });
    for await (const _ of stream) break;
    const all = await records(mem, 1);
    expect(all).toHaveLength(1);
    expect(all[0]).toMatchObject({ status: "ok", outputs: { chunk_count: 1, stream_completed: false } });
    await shutdown();
    expect(mem.records).toHaveLength(1);
  });

  it("controller.abort() mid-stream emits one record marked cancelled and incomplete", async () => {
    const mem = await fresh();
    const stream = await client(chatServer({ hang: true })).chat.completions.create({ model: "gpt-4o-mini", messages: [], stream: true });
    let seen = 0;
    for await (const _ of stream) {
      seen++;
      if (seen === 2) stream.controller.abort();
    }
    const all = await records(mem, 1);
    expect(all).toHaveLength(1);
    expect(all[0]!.outputs).toMatchObject({ stream_completed: false });
    expect(all[0]!.status).toBe("cancelled");
  });

  it("tee() splits the stream and still records once", async () => {
    const mem = await fresh();
    const stream = await client(chatServer()).chat.completions.create({ model: "gpt-4o-mini", messages: [], stream: true });
    const [left, right] = stream.tee();
    const read = async (s: typeof left) => {
      let n = 0;
      for await (const _ of s) n++;
      return n;
    };
    expect(await Promise.all([read(left), read(right)])).toEqual([4, 4]);
    const all = await records(mem, 1);
    expect(all).toHaveLength(1);
    expect(all[0]!.outputs).toMatchObject({ chunk_count: 4, stream_completed: true });
  });

  it("toReadableStream() still works and is recorded", async () => {
    const mem = await fresh();
    const stream = await client(chatServer()).chat.completions.create({ model: "gpt-4o-mini", messages: [], stream: true });
    const text = await new Response(stream.toReadableStream()).text();
    expect(text.trim().split("\n")).toHaveLength(4);
    expect((await records(mem, 1))[0]!.outputs).toMatchObject({ stream_completed: true });
  });

  it("a stream that is never read is recorded once, incomplete, at shutdown", async () => {
    const mem = await fresh();
    await client(chatServer()).chat.completions.create({ model: "gpt-4o-mini", messages: [], stream: true });
    await records(mem);
    expect(mem.records).toHaveLength(0);
    await shutdown();
    expect(mem.records).toHaveLength(1);
    expect(mem.records[0]).toMatchObject({ stream: true, outputs: { chunk_count: 0, stream_completed: false } });
  });

  it("chat.completions.stream(): .on() and finalChatCompletion() keep working", async () => {
    const mem = await fresh();
    const runner = client(chatServer()).chat.completions.stream({ model: "gpt-4o-mini", messages: [] });
    const deltas: string[] = [];
    runner.on("content", (delta) => deltas.push(delta));
    const final = await runner.finalChatCompletion();
    expect(deltas.join("")).toBe("Approved");
    expect(final.choices[0]!.message.content).toBe("Approved");
    const [record, ...rest] = await records(mem, 1);
    expect(rest).toHaveLength(0);
    expect(record).toMatchObject({ function_name: "openai.chat.completions.stream", stream: true, outputs: { stream_completed: true } });
  });
});

describe("openai responses and embeddings", () => {
  const response = {
    id: "resp_1",
    object: "response",
    created_at: 1_760_000_000,
    status: "completed",
    model: "gpt-4.1-2025-04-14",
    output: [
      { id: "msg_1", type: "message", role: "assistant", status: "completed", content: [{ type: "output_text", text: "Covered.", annotations: [] }] },
    ],
    incomplete_details: null,
    error: null,
    usage: {
      input_tokens: 9,
      output_tokens: 2,
      total_tokens: 11,
      input_tokens_details: { cached_tokens: 0 },
      output_tokens_details: { reasoning_tokens: 0 },
    },
  };

  it("responses.create", async () => {
    const mem = await fresh({ captureContent: true });
    const r = await client(fakeFetch(() => json(response, { "x-request-id": "req_resp" }))).responses.create({
      model: "gpt-4.1",
      input: "Is claim 123 covered?",
      max_output_tokens: 100,
    });
    expect(r.output_text).toBe("Covered.");
    const [record] = await records(mem, 1);
    expect(record).toMatchObject({
      decision_type: "llm.openai.responses",
      function_name: "openai.responses.create",
      finish_reason: "completed",
      response_id: "resp_1",
      request_id: "req_resp",
      token_usage: { prompt_tokens: 9, completion_tokens: 2, total_tokens: 11 },
      model_parameters: { model_name: "gpt-4.1-2025-04-14", max_output_tokens: 100 },
      outputs: { content: "Covered." },
      inputs: { input: "Is claim 123 covered?" },
    });
  });

  it("responses.create stream reads usage from the terminal event", async () => {
    const mem = await fresh({ captureContent: true });
    const events = [
      { type: "response.created", sequence_number: 0, response: { ...response, status: "in_progress", usage: null } },
      { type: "response.output_text.delta", sequence_number: 1, item_id: "msg_1", output_index: 0, content_index: 0, delta: "Cov", logprobs: [] },
      { type: "response.output_text.delta", sequence_number: 2, item_id: "msg_1", output_index: 0, content_index: 0, delta: "ered.", logprobs: [] },
      { type: "response.completed", sequence_number: 3, response },
    ];
    const stream = await client(fakeFetch(() => sse(events, { "x-request-id": "req_rs" }))).responses.create({
      model: "gpt-4.1",
      input: "q",
      stream: true,
    });
    for await (const _ of stream) void _;
    const [record] = await records(mem, 1);
    expect(record).toMatchObject({
      stream: true,
      request_id: "req_rs",
      token_usage: { prompt_tokens: 9, completion_tokens: 2, total_tokens: 11 },
      outputs: { content: "Covered.", chunk_count: 4, stream_completed: true },
    });
  });

  it("embeddings.create (default base64 decoding path)", async () => {
    const mem = await fresh();
    const vector = Buffer.from(new Float32Array([0.1, 0.2, 0.3]).buffer).toString("base64");
    const r = await client(
      fakeFetch(() =>
        json({ object: "list", model: "text-embedding-3-small", data: [{ object: "embedding", index: 0, embedding: vector }], usage: { prompt_tokens: 4, total_tokens: 4 } }),
      ),
    ).embeddings.create({ model: "text-embedding-3-small", input: "claim 123", dimensions: 3 });
    expect(r.data[0]!.embedding).toHaveLength(3);
    const [record] = await records(mem, 1);
    expect(record).toMatchObject({
      decision_type: "llm.openai.embeddings",
      token_usage: { prompt_tokens: 4, completion_tokens: 0, total_tokens: 4 },
      embedding_count: 1,
      model_parameters: { dimensions: 3 },
    });
  });
});
