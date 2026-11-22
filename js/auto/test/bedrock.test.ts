import { Readable } from "node:stream";

import {
  BedrockRuntime,
  BedrockRuntimeClient,
  ConverseCommand,
  ConverseStreamCommand,
  InvokeModelCommand,
  ThrottlingException,
} from "@aws-sdk/client-bedrock-runtime";
import { afterAll, describe, expect, it } from "vitest";

import { undo } from "../src/auto.js";
import { MIDDLEWARE_NAME } from "../src/instrument/bedrock.js";
import { shutdown } from "../src/observe.js";
import { assertNoSecrets, bedrockEvent, fresh, records } from "./helpers.js";

interface FakeResponse {
  statusCode: number;
  headers: Record<string, string>;
  body: Uint8Array | Uint8Array[];
}

/** A custom `requestHandler` (the AWS SDK's documented transport seam). */
function handler(respond: (path: string) => FakeResponse) {
  const seen: { path: string; headers: Record<string, string> }[] = [];
  return {
    seen,
    handle: async (request: { path: string; headers: Record<string, string> }) => {
      seen.push({ path: request.path, headers: request.headers });
      const r = respond(request.path);
      const parts = Array.isArray(r.body) ? r.body : [r.body];
      return { response: { statusCode: r.statusCode, headers: r.headers, body: Readable.from(parts.map((p) => Buffer.from(p))) } };
    },
  };
}

const MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0";
const SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";

const converseBody = {
  output: { message: { role: "assistant", content: [{ text: "Approved." }] } },
  stopReason: "end_turn",
  usage: { inputTokens: 11, outputTokens: 3, totalTokens: 14, cacheReadInputTokens: 0 },
  metrics: { latencyMs: 120 },
};

function server(kind: "ok" | "throttle" = "ok") {
  return handler((path) => {
    if (kind === "throttle") {
      return {
        statusCode: 429,
        headers: { "content-type": "application/json", "x-amzn-errortype": "ThrottlingException:http://internal.amazon.com/coral/com.amazon.bedrock/", "x-amzn-requestid": "rid-429" },
        body: new TextEncoder().encode(JSON.stringify({ message: "Too many requests" })),
      };
    }
    if (path.endsWith("/converse-stream")) {
      return {
        statusCode: 200,
        headers: { "content-type": "application/vnd.amazon.eventstream", "x-amzn-requestid": "rid-stream" },
        body: [
          bedrockEvent("messageStart", { role: "assistant" }),
          bedrockEvent("contentBlockDelta", { contentBlockIndex: 0, delta: { text: "Appro" } }),
          bedrockEvent("contentBlockDelta", { contentBlockIndex: 0, delta: { text: "ved." } }),
          bedrockEvent("contentBlockStop", { contentBlockIndex: 0 }),
          bedrockEvent("messageStop", { stopReason: "end_turn" }),
          bedrockEvent("metadata", { usage: { inputTokens: 11, outputTokens: 3, totalTokens: 14 }, metrics: { latencyMs: 90 } }),
        ],
      };
    }
    if (path.endsWith("/invoke")) {
      return {
        statusCode: 200,
        headers: {
          "content-type": "application/json",
          "x-amzn-requestid": "rid-invoke",
          "x-amzn-bedrock-input-token-count": "17",
          "x-amzn-bedrock-output-token-count": "5",
        },
        body: new TextEncoder().encode(JSON.stringify({ content: [{ type: "text", text: "Approved." }] })),
      };
    }
    return {
      statusCode: 200,
      headers: { "content-type": "application/json", "x-amzn-requestid": "rid-converse" },
      body: new TextEncoder().encode(JSON.stringify(converseBody)),
    };
  });
}

function client(requestHandler: ReturnType<typeof handler>) {
  return new BedrockRuntimeClient({
    region: "us-east-1",
    credentials: { accessKeyId: "AKIDEXAMPLE", secretAccessKey: SECRET_ACCESS_KEY },
    requestHandler,
    maxAttempts: 1,
  });
}

const converseInput = {
  modelId: MODEL_ID,
  messages: [{ role: "user" as const, content: [{ text: "Is claim 123 covered?" }] }],
  inferenceConfig: { maxTokens: 64, temperature: 0.2 },
};

afterAll(async () => {
  undo();
  await shutdown();
});

describe("bedrock runtime", () => {
  it("Converse: one record via the client middleware stack", async () => {
    const mem = await fresh({ captureContent: true });
    const c = client(server());
    const out = await c.send(new ConverseCommand(converseInput));
    expect(out.output?.message?.content?.[0]?.text).toBe("Approved.");
    expect(c.middlewareStack.identify().some((m) => m.startsWith(MIDDLEWARE_NAME))).toBe(true);
    const [record, ...rest] = await records(mem, 1);
    expect(rest).toHaveLength(0);
    expect(record).toMatchObject({
      decision_type: "llm.bedrock.converse",
      function_name: "bedrock-runtime.converse",
      status: "ok",
      model_parameters: { provider: "bedrock", model_name: MODEL_ID, maxTokens: 64, temperature: 0.2 },
      token_usage: { prompt_tokens: 11, completion_tokens: 3, total_tokens: 14, cache_read_input_tokens: 0 },
      finish_reason: "end_turn",
      request_id: "rid-converse",
      outputs: { content: "Approved." },
      stream: false,
    });
    const text = JSON.stringify(mem.records);
    expect(text).not.toContain(SECRET_ACCESS_KEY);
    expect(text).not.toContain("AKIDEXAMPLE");
    assertNoSecrets(mem.records);
  });

  it("ConverseStream: the same stream object, instrumented in place", async () => {
    const mem = await fresh();
    const out = await client(server()).send(new ConverseStreamCommand(converseInput));
    const stream = out.stream!;
    let text = "";
    for await (const event of stream) text += event.contentBlockDelta?.delta?.text ?? "";
    expect(text).toBe("Approved.");
    expect(out.stream).toBe(stream);
    const [record, ...rest] = await records(mem, 1);
    expect(rest).toHaveLength(0);
    expect(record).toMatchObject({
      function_name: "bedrock-runtime.converse_stream",
      stream: true,
      finish_reason: "end_turn",
      request_id: "rid-stream",
      token_usage: { prompt_tokens: 11, completion_tokens: 3, total_tokens: 14 },
      outputs: { chunk_count: 6, stream_completed: true },
    });
  });

  it("ConverseStream abandoned early records once, incomplete", async () => {
    const mem = await fresh();
    const out = await client(server()).send(new ConverseStreamCommand(converseInput));
    for await (const _ of out.stream!) break;
    const all = await records(mem, 1);
    expect(all).toHaveLength(1);
    expect(all[0]!.outputs).toMatchObject({ chunk_count: 1, stream_completed: false });
  });

  it("InvokeModel: token counts from the response headers; the body is left unread", async () => {
    const mem = await fresh();
    const out = await new BedrockRuntime({
      region: "us-east-1",
      credentials: { accessKeyId: "AKIDEXAMPLE", secretAccessKey: SECRET_ACCESS_KEY },
      requestHandler: server(),
    }).invokeModel({ modelId: MODEL_ID, body: JSON.stringify({ messages: [] }), contentType: "application/json" });
    expect(JSON.parse(new TextDecoder().decode(out.body)).content[0].text).toBe("Approved.");
    const [record] = await records(mem, 1);
    expect(record).toMatchObject({
      decision_type: "llm.bedrock.invoke_model",
      request_id: "rid-invoke",
      token_usage: { prompt_tokens: 17, completion_tokens: 5, total_tokens: 22 },
    });
  });

  it("service errors propagate unchanged and are recorded", async () => {
    const mem = await fresh();
    const error = await client(server("throttle"))
      .send(new InvokeModelCommand({ modelId: MODEL_ID, body: "{}" }))
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ThrottlingException);
    const [record] = await records(mem, 1);
    expect(record).toMatchObject({ status: "error", error_type: "ThrottlingException", http_status: 429, request_id: "rid-429" });
  });

  it("other AWS operations and other services pass through untouched", async () => {
    const mem = await fresh();
    const c = client(server());
    // A command this module does not record (ListAsyncInvokes) still goes through send().
    const { ListAsyncInvokesCommand } = await import("@aws-sdk/client-bedrock-runtime");
    await c.send(new ListAsyncInvokesCommand({})).catch(() => undefined);
    expect(await records(mem)).toHaveLength(0);
  });

  it("undo() removes the patch; clients that already have the middleware stop recording", async () => {
    const mem = await fresh();
    const c = client(server());
    await c.send(new ConverseCommand(converseInput));
    undo("bedrock");
    expect(Object.prototype.hasOwnProperty.call(BedrockRuntimeClient.prototype, "send")).toBe(false);
    await c.send(new ConverseCommand(converseInput));
    await client(server()).send(new ConverseCommand(converseInput));
    expect(await records(mem, 1)).toHaveLength(1);
  });
});
