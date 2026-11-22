/**
 * Instrumentation for the `@anthropic-ai/sdk` Node SDK.
 *
 * Verified against @anthropic-ai/sdk 1.12.0 (npm), by reading the installed
 * package's `.mjs` / `.js` sources and `.d.ts` types, and by the offline
 * tests in `test/anthropic.test.ts`.
 *
 * Patch targets (prototype methods, patched in both the CommonJS and the ESM
 * copy of the package), all recorded as `decision_type: "llm.anthropic.messages"`:
 *
 * - `@anthropic-ai/sdk/resources/messages/messages` `Messages.prototype.create`
 * - `@anthropic-ai/sdk/resources/beta/messages/messages` `Messages.prototype.create`
 *
 * Layout facts relied on, as found in 1.12.0:
 *
 * - `messages.parse()` calls `this.create()`; `messages.stream()` builds a
 *   `MessageStream` whose request is `messages.create({ ...params, stream: true })`
 *   with header `x-stainless-helper-method: stream`. Both are covered by the
 *   `create` patch; the `MessageStream` object (`.on()`, `.finalMessage()`,
 *   `.controller.abort()`) is not touched.
 * - `create()` returns an `APIPromise`; `stream: true` parses to a `Stream`
 *   of raw events (see `stainless.ts`).
 * - Usage: `Message.usage` (`input_tokens`, `output_tokens`,
 *   `cache_read_input_tokens`, `cache_creation_input_tokens`). In a stream,
 *   `message_start.message.usage` carries the input side and each
 *   `message_delta.usage` the cumulative `output_tokens`; `message_delta.delta.stop_reason`
 *   the stop reason; `content_block_delta.delta` (`type: "text_delta"`) the text.
 *   `input_tokens` excludes cached tokens, so `total_tokens` is
 *   `input_tokens + output_tokens` with the cache counters reported next to it.
 * - The request id is the `request-id` response header (`_request_id` on
 *   parsed bodies).
 */

import { tokenUsage } from "../record.js";
import { TextAccumulator, read, scalarParams } from "../util.js";
import { type CallInfo, type Instrumentor, type PatchTarget, type ProviderCall, StreamState } from "./core.js";
import { type StainlessHooks, wrapStainlessMethod } from "./stainless.js";

export const PROVIDER = "anthropic";
export const PACKAGE = "@anthropic-ai/sdk";
export const VERIFIED_VERSION = "1.12.0";
export const DECISION_TYPE = "llm.anthropic.messages";

const PARAMS = ["max_tokens", "service_tier", "speed", "inference_geo", "temperature", "top_p", "top_k"] as const;
const INPUT_KEYS = ["system", "messages", "tools"] as const;

function usage(value: unknown) {
  return tokenUsage(read(value, "input_tokens"), read(value, "output_tokens"), undefined, {
    cache_read_input_tokens: read(value, "cache_read_input_tokens"),
    cache_creation_input_tokens: read(value, "cache_creation_input_tokens"),
  });
}

function contentBlocks(blocks: unknown): Record<string, unknown> {
  const texts: string[] = [];
  const toolCalls: unknown[] = [];
  for (const block of Array.isArray(blocks) ? blocks : []) {
    const kind = read(block, "type");
    if (kind === "text") texts.push(String(read(block, "text") ?? ""));
    else if (kind === "tool_use") toolCalls.push({ id: read(block, "id"), name: read(block, "name"), input: read(block, "input") });
  }
  const content: Record<string, unknown> = { content: texts.join("") };
  if (toolCalls.length) content["tool_calls"] = toolCalls;
  return content;
}

function messageInfo(message: unknown, wantContent: boolean): CallInfo {
  const info: CallInfo = {
    model: read(message, "model"),
    response_id: read(message, "id"),
    request_id: read(message, "_request_id"),
    finish_reason: read(message, "stop_reason"),
    usage: usage(read(message, "usage")),
  };
  if (wantContent) info.content = contentBlocks(read(message, "content"));
  return info;
}

class MessageStreamState extends StreamState {
  private readonly text: TextAccumulator;
  private readonly data: CallInfo;
  private counters: Record<string, number> = {};

  constructor(call: ProviderCall, private readonly inst: Instrumentor, requestId: string | null) {
    super(call);
    this.text = new TextAccumulator(inst.options.maxContentChars);
    this.data = { request_id: requestId };
  }

  private readUsage(value: unknown): void {
    for (const key of ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]) {
      const n = read(value, key);
      if (typeof n === "number" && Number.isFinite(n)) this.counters[key] = n;
    }
  }

  observe(event: unknown): void {
    const kind = read(event, "type");
    if (kind === "message_start") {
      const message = read(event, "message");
      this.data.model = read(message, "model");
      this.data.response_id = read(message, "id");
      this.readUsage(read(message, "usage"));
    } else if (kind === "message_delta") {
      this.readUsage(read(event, "usage"));
      const stop = read(event, "delta", "stop_reason");
      if (stop) this.data.finish_reason = stop;
    } else if (kind === "content_block_delta" && this.inst.capturesContent) {
      if (read(event, "delta", "type") === "text_delta") this.text.add(read(event, "delta", "text"));
    }
  }

  info(): CallInfo {
    const info: CallInfo = { ...this.data, usage: usage(this.counters) };
    if (this.inst.capturesContent) info.content = { content: this.text.text };
    return info;
  }
}

function helperMethod(options: unknown): string | null {
  const headers = read(options, "headers");
  if (!headers || typeof headers !== "object") return null;
  const get = (headers as { get?: unknown }).get;
  if (typeof get === "function") {
    const value = (get as (n: string) => unknown).call(headers, "x-stainless-helper-method");
    return typeof value === "string" ? value : null;
  }
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === "x-stainless-helper-method" && typeof value === "string") return value;
  }
  return null;
}

const SURFACES = [
  { specifier: "@anthropic-ai/sdk/resources/messages/messages", functionBase: "anthropic.messages" },
  { specifier: "@anthropic-ai/sdk/resources/beta/messages/messages", functionBase: "anthropic.beta.messages" },
] as const;

export const SPECIFIERS = SURFACES.map((s) => s.specifier);

export function targets(inst: Instrumentor, modules: Record<string, Record<string, unknown> | undefined>): PatchTarget[] {
  return SURFACES.map((surface) => {
    const begin = (args: unknown[]): ProviderCall | null => {
      const body = args[0];
      if (!body || typeof body !== "object") return null;
      const params = scalarParams(body, PARAMS);
      const effort = read(body, "output_config", "effort");
      if (typeof effort === "string") params["effort"] = effort;
      const helper = helperMethod(args[1]);
      return inst.begin(DECISION_TYPE, `${surface.functionBase}.${helper === "stream" ? "stream" : "create"}`, {
        model: read(body, "model"),
        params,
        inputs: () => {
          const out: Record<string, unknown> = {};
          for (const key of INPUT_KEYS) if (read(body, key) !== undefined) out[key] = read(body, key);
          return out;
        },
        stream: read(body, "stream") === true,
      });
    };
    const hooks: StainlessHooks = {
      requestIdHeader: "request-id",
      info: (parsed) => messageInfo(parsed, inst.capturesContent),
      streamState: (call, requestId) => new MessageStreamState(call, inst, requestId),
    };
    return {
      kind: "method",
      owner: read(modules[surface.specifier]?.["Messages"], "prototype"),
      key: "create",
      label: `${surface.specifier}#Messages.create`,
      wrap: wrapStainlessMethod(inst, begin, hooks),
    } satisfies PatchTarget;
  });
}
