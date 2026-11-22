import Anthropic from "@anthropic-ai/sdk";
import { APIPromise } from "@anthropic-ai/sdk/core/api-promise";
import { Stream } from "@anthropic-ai/sdk/core/streaming";
import { MessageStream } from "@anthropic-ai/sdk/lib/MessageStream";
import { afterAll, describe, expect, it } from "vitest";

import { undo } from "../src/auto.js";
import { shutdown } from "../src/observe.js";
import { SECRET_KEY, assertNoSecrets, fakeFetch, fresh, json, records, sse } from "./helpers.js";

const MODEL = "claude-sonnet-5-5-20260915";

const message = {
  id: "msg_01",
  type: "message",
  role: "assistant",
  model: MODEL,
  content: [{ type: "text", text: "Approved." }],
  stop_reason: "end_turn",
  stop_sequence: null,
  usage: { input_tokens: 20, output_tokens: 4, cache_read_input_tokens: 8, cache_creation_input_tokens: 0 },
};

function event(name: string, data: unknown): string {
  return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
}

const streamEvents = [
  event("message_start", { type: "message_start", message: { ...message, content: [], stop_reason: null, usage: { input_tokens: 20, output_tokens: 1, cache_read_input_tokens: 8, cache_creation_input_tokens: 0 } } }),
  event("content_block_start", { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } }),
  event("ping", { type: "ping" }),
  event("content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Appro" } }),
  event("content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "ved." } }),
  event("content_block_stop", { type: "content_block_stop", index: 0 }),
  event("message_delta", { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 4 } }),
  event("message_stop", { type: "message_stop" }),
];

function server(options: { hang?: boolean } = {}) {
  return fakeFetch(({ body, init }) => {
    if (body?.["stream"]) {
      return sse(options.hang ? streamEvents.slice(0, 4) : streamEvents, { "request-id": "req_stream_a" }, { ...options, signal: init.signal });
    }
    return json(message, { "request-id": "req_a" });
  });
}

function client(fetchImpl: typeof fetch) {
  return new Anthropic({ apiKey: SECRET_KEY, fetch: fetchImpl, maxRetries: 0, baseURL: "https://api.anthropic.test" });
}

const params = { model: "claude-sonnet-5-5", max_tokens: 64, messages: [{ role: "user" as const, content: "Is claim 123 covered?" }] };

afterAll(async () => {
  undo();
  await shutdown();
});

describe("anthropic messages", () => {
  it("messages.create: one record, same object back, no secrets", async () => {
    const mem = await fresh();
    const promise = client(server()).messages.create(params);
    expect(promise).toBeInstanceOf(APIPromise);
    const { data, request_id } = await promise.withResponse();
    expect(data.content[0]).toEqual({ type: "text", text: "Approved." });
    expect(request_id).toBe("req_a");
    const [record, ...rest] = await records(mem, 1);
    expect(rest).toHaveLength(0);
    expect(record).toMatchObject({
      decision_type: "llm.anthropic.messages",
      function_name: "anthropic.messages.create",
      status: "ok",
      model_parameters: { provider: "anthropic", model_name: MODEL, requested_model: "claude-sonnet-5-5", max_tokens: 64 },
      token_usage: { prompt_tokens: 20, completion_tokens: 4, total_tokens: 24, cache_read_input_tokens: 8, cache_creation_input_tokens: 0 },
      finish_reason: "end_turn",
      response_id: "msg_01",
      request_id: "req_a",
      stream: false,
    });
    assertNoSecrets(mem.records);
  });

  it("create({ stream: true }) returns the SDK Stream and records usage from the events", async () => {
    const mem = await fresh({ captureContent: true });
    const stream = await client(server()).messages.create({ ...params, stream: true });
    expect(stream).toBeInstanceOf(Stream);
    const kinds: string[] = [];
    for await (const e of stream) kinds.push(e.type);
    expect(kinds).toContain("message_stop");
    const [record] = await records(mem, 1);
    expect(record).toMatchObject({
      stream: true,
      token_usage: { prompt_tokens: 20, completion_tokens: 4, total_tokens: 24 },
      finish_reason: "end_turn",
      request_id: "req_stream_a",
      outputs: { content: "Approved.", chunk_count: 7, stream_completed: true },
    });
  });

  it("messages.stream(): .on('text') and finalMessage() keep working; one record", async () => {
    const mem = await fresh();
    const stream = client(server()).messages.stream(params);
    expect(stream).toBeInstanceOf(MessageStream);
    const texts: string[] = [];
    stream.on("text", (t) => texts.push(t));
    const final = await stream.finalMessage();
    expect(texts.join("")).toBe("Approved.");
    expect(final.usage.output_tokens).toBe(4);
    const all = await records(mem, 1);
    expect(all).toHaveLength(1);
    expect(all[0]).toMatchObject({ function_name: "anthropic.messages.stream", stream: true, outputs: { stream_completed: true } });
  });

  it("MessageStream.controller.abort() records one cancelled, incomplete call", async () => {
    const mem = await fresh();
    const stream = client(server({ hang: true })).messages.stream(params);
    stream.on("text", () => stream.controller.abort());
    await stream.done().catch(() => undefined);
    const all = await records(mem, 1);
    expect(all).toHaveLength(1);
    expect(all[0]!.status).toBe("cancelled");
    expect(all[0]!.outputs).toMatchObject({ stream_completed: false });
    await shutdown();
    expect(mem.records).toHaveLength(1);
  });

  it("API errors propagate unchanged and are recorded with status and request id", async () => {
    const mem = await fresh();
    const failing = fakeFetch(() =>
      json({ type: "error", error: { type: "overloaded_error", message: "Overloaded" } }, { "request-id": "req_529" }, 529),
    );
    const error = await client(failing).messages.create(params).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(Anthropic.APIError);
    expect((error as { status: number }).status).toBe(529);
    const [record] = await records(mem, 1);
    expect(record).toMatchObject({ status: "error", http_status: 529, request_id: "req_529", token_usage: null });
    expect(record!.error_type).toBe((error as Error).constructor.name);
  });

  it("messages.parse() goes through create and is recorded once", async () => {
    const mem = await fresh();
    const parsed = await client(server()).messages.parse(params);
    expect(parsed.id).toBe("msg_01");
    expect(await records(mem, 1)).toHaveLength(1);
  });

  it("beta.messages.create is recorded under its own function name", async () => {
    const mem = await fresh();
    await client(server()).beta.messages.create(params);
    const [record] = await records(mem, 1);
    expect(record!.function_name).toBe("anthropic.beta.messages.create");
  });
});
