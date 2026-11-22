/**
 * Instrumentation for Amazon Bedrock Runtime through the AWS SDK for
 * JavaScript v3 (`@aws-sdk/client-bedrock-runtime`).
 *
 * Verified against @aws-sdk/client-bedrock-runtime 3.1175.0 (with
 * @smithy/core 3.41.0), by reading `dist-cjs/index.js`,
 * `@smithy/core/dist-cjs/submodules/client/index.js` and
 * `dist-types/models/models_0.d.ts`, and by the offline tests in
 * `test/bedrock.test.ts`.
 *
 * Seam: the client middleware stack (`client.middlewareStack.add`), the
 * documented AWS SDK v3 extension point. The package has no `exports` map
 * and resolves to its CommonJS build for both `import` and `require`, so
 * there is one copy. `BedrockRuntimeClient.prototype` gets an own `send`
 * (the method is inherited from `@smithy/core`'s `Client`) that adds an
 * `initialize`-step middleware named `briefcaseAutoMiddleware` to the
 * client's stack once, then calls the inherited `send`. `BedrockRuntime`
 * (the aggregated client) extends `BedrockRuntimeClient` and is covered.
 * `undo()` removes the own `send`; clients that already carry the
 * middleware keep it, switched to pass-through.
 *
 * | command                                | decision_type              |
 * |----------------------------------------|----------------------------|
 * | `ConverseCommand`                      | `llm.bedrock.converse`     |
 * | `ConverseStreamCommand`                | `llm.bedrock.converse`     |
 * | `InvokeModelCommand`                   | `llm.bedrock.invoke_model` |
 * | `InvokeModelWithResponseStreamCommand` | `llm.bedrock.invoke_model` |
 *
 * Fields read (3.1175.0 types): Converse `usage` (`inputTokens`,
 * `outputTokens`, `totalTokens`, `cacheReadInputTokens`,
 * `cacheWriteInputTokens`), `stopReason`, `output.message.content[].text`;
 * ConverseStream `output.stream` events `messageStop.stopReason`,
 * `metadata.usage`, `contentBlockDelta.delta.text` (the stream object is
 * instrumented in place); InvokeModel token counts from the
 * `x-amzn-bedrock-input-token-count` / `x-amzn-bedrock-output-token-count`
 * response headers (the body is never read); the request id from
 * `$metadata.requestId`. InvokeModelWithResponseStream: the final chunk's
 * `amazon-bedrock-invocationMetrics` (provider JSON, parsed defensively,
 * not described by the SDK types; mirrors the Python module).
 */

import { tokenUsage } from "../record.js";
import { TextAccumulator, debug, read, safe, scalarParams } from "../util.js";
import {
  type CallInfo,
  type Instrumentor,
  type PatchTarget,
  type ProviderCall,
  StreamState,
  finishWhenCollected,
  instrumentAsyncIterable,
} from "./core.js";

export const PROVIDER = "bedrock";
export const PACKAGE = "@aws-sdk/client-bedrock-runtime";
export const VERIFIED_VERSION = "3.1175.0";
export const SPECIFIERS = ["@aws-sdk/client-bedrock-runtime"];
export const MIDDLEWARE_NAME = "briefcaseAutoMiddleware";

type Operation = { name: string; decisionType: string; functionName: string; stream: boolean };

const OPERATIONS: Record<string, Operation> = {
  ConverseCommand: { name: "Converse", decisionType: "llm.bedrock.converse", functionName: "bedrock-runtime.converse", stream: false },
  ConverseStreamCommand: {
    name: "ConverseStream",
    decisionType: "llm.bedrock.converse",
    functionName: "bedrock-runtime.converse_stream",
    stream: true,
  },
  InvokeModelCommand: {
    name: "InvokeModel",
    decisionType: "llm.bedrock.invoke_model",
    functionName: "bedrock-runtime.invoke_model",
    stream: false,
  },
  InvokeModelWithResponseStreamCommand: {
    name: "InvokeModelWithResponseStream",
    decisionType: "llm.bedrock.invoke_model",
    functionName: "bedrock-runtime.invoke_model_with_response_stream",
    stream: true,
  },
};

const INFERENCE_PARAMS = ["maxTokens", "temperature", "topP"] as const;

function usage(value: unknown) {
  return tokenUsage(read(value, "inputTokens"), read(value, "outputTokens"), read(value, "totalTokens"), {
    cache_read_input_tokens: read(value, "cacheReadInputTokens"),
    cache_write_input_tokens: read(value, "cacheWriteInputTokens"),
  });
}

function headerInt(headers: unknown, name: string): number | undefined {
  const raw = read(headers, name);
  const n = typeof raw === "string" ? Number(raw) : NaN;
  return Number.isFinite(n) ? n : undefined;
}

function converseInfo(output: unknown, wantContent: boolean): CallInfo {
  const info: CallInfo = {
    request_id: read(output, "$metadata", "requestId"),
    finish_reason: read(output, "stopReason"),
    usage: usage(read(output, "usage")),
  };
  if (wantContent) {
    const blocks = read(output, "output", "message", "content");
    const texts = (Array.isArray(blocks) ? blocks : []).map((b) => read(b, "text")).filter((t) => typeof t === "string");
    info.content = { content: texts.join("") };
  }
  return info;
}

class ConverseStreamState extends StreamState {
  private readonly acc: TextAccumulator;
  private readonly data: CallInfo;
  constructor(call: ProviderCall, private readonly inst: Instrumentor, requestId: unknown) {
    super(call);
    this.acc = new TextAccumulator(inst.options.maxContentChars);
    this.data = { request_id: requestId };
  }
  observe(event: unknown): void {
    if (!event || typeof event !== "object") return;
    if ("messageStop" in event) this.data.finish_reason = read(event, "messageStop", "stopReason");
    else if ("metadata" in event) {
      const u = usage(read(event, "metadata", "usage"));
      if (u) this.data.usage = u;
    } else if ("contentBlockDelta" in event && this.inst.capturesContent) {
      this.acc.add(read(event, "contentBlockDelta", "delta", "text"));
    }
  }
  info(): CallInfo {
    const info: CallInfo = { ...this.data };
    if (this.inst.capturesContent) info.content = { content: this.acc.text };
    return info;
  }
}

class InvokeStreamState extends StreamState {
  private readonly data: CallInfo;
  constructor(call: ProviderCall, requestId: unknown) {
    super(call);
    this.data = { request_id: requestId };
  }
  observe(event: unknown): void {
    const bytes = read(event, "chunk", "bytes");
    if (!(bytes instanceof Uint8Array)) return;
    const text = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString("utf8");
    if (!text.includes("invocationMetrics")) return;
    const metrics = read(JSON.parse(text), "amazon-bedrock-invocationMetrics");
    const u = tokenUsage(read(metrics, "inputTokenCount"), read(metrics, "outputTokenCount"));
    if (u) this.data.usage = u;
  }
  info(): CallInfo {
    return { ...this.data };
  }
}

function beginCall(inst: Instrumentor, op: Operation, input: unknown): ProviderCall | null {
  return inst.begin(op.decisionType, op.functionName, {
    model: read(input, "modelId"),
    params: scalarParams(read(input, "inferenceConfig") ?? {}, INFERENCE_PARAMS),
    inputs: () => {
      if (op.name.startsWith("Converse")) {
        const out: Record<string, unknown> = {};
        for (const key of ["system", "messages"]) if (read(input, key) !== undefined) out[key] = read(input, key);
        return out;
      }
      let body = read(input, "body");
      if (body instanceof Uint8Array) body = Buffer.from(body).toString("utf8");
      return { body };
    },
    stream: op.stream,
  });
}

function endCall(inst: Instrumentor, call: ProviderCall, op: Operation, result: unknown): void {
  const output = read(result, "output");
  const requestId = read(output, "$metadata", "requestId");
  if (op.name === "Converse") return call.finish(converseInfo(output, inst.capturesContent));
  if (op.name === "InvokeModel") {
    const headers = read(result, "response", "headers");
    return call.finish({
      request_id: requestId,
      usage: tokenUsage(
        headerInt(headers, "x-amzn-bedrock-input-token-count"),
        headerInt(headers, "x-amzn-bedrock-output-token-count"),
      ),
    });
  }
  const key = op.name === "ConverseStream" ? "stream" : "body";
  const events = read(output, key);
  const state = op.name === "ConverseStream" ? new ConverseStreamState(call, inst, requestId) : new InvokeStreamState(call, requestId);
  call.fallback = { request_id: requestId };
  const ok = instrumentAsyncIterable(events, {
    onItem: (event) => state.onItem(event),
    onEnd: (kind, error) => state.onEnd(kind, error),
  });
  if (!ok) call.finish({ request_id: requestId }, { stream: { chunk_count: 0, stream_completed: false } });
  else finishWhenCollected(events as object, call);
}

type Handler = (args: { input?: unknown }) => Promise<unknown>;

function middleware(inst: Instrumentor) {
  return (next: Handler, context: { commandName?: string }): Handler =>
    async (args) => {
      const op = OPERATIONS[context?.commandName ?? ""];
      if (!op || !inst.active) return next(args);
      const call = safe(() => beginCall(inst, op, args?.input), "begin") ?? null;
      if (!call) return next(args);
      let result: unknown;
      try {
        result = await next(args);
      } catch (error) {
        call.finish({}, { error });
        throw error;
      }
      try {
        endCall(inst, call, op, result);
      } catch (error) {
        debug(`bedrock record could not be completed: ${String(error)}`);
        call.finish({});
      }
      return result;
    };
}

export function targets(inst: Instrumentor, modules: Record<string, Record<string, unknown> | undefined>): PatchTarget[] {
  const Client = modules["@aws-sdk/client-bedrock-runtime"]?.["BedrockRuntimeClient"];
  const seen = new WeakSet<object>();
  const mw = middleware(inst);
  return [
    {
      kind: "method",
      owner: read(Client, "prototype"),
      key: "send",
      inherited: true,
      label: "@aws-sdk/client-bedrock-runtime#BedrockRuntimeClient.send",
      wrap: (original) =>
        function (this: unknown, ...args: unknown[]): unknown {
          if (inst.active && this && typeof this === "object" && !seen.has(this)) {
            seen.add(this);
            safe(() => {
              const stack = read(this, "middlewareStack") as { add?: (...a: unknown[]) => void } | undefined;
              if (typeof stack?.add !== "function") {
                debug("BedrockRuntimeClient has no middlewareStack.add; call not recorded");
                return;
              }
              stack.add(mw, { step: "initialize", name: MIDDLEWARE_NAME, priority: "high", override: true });
              delete (this as Record<string, unknown>)["handlers"]; // drop a cached handler chain built without us
            }, "add middleware");
          }
          return original.apply(this, args);
        },
    },
  ];
}
