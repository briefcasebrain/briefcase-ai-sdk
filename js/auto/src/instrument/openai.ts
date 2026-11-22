/**
 * Instrumentation for the `openai` Node SDK.
 *
 * Verified against openai 7.62.0 (npm), by reading the installed package's
 * `.mjs` / `.js` sources and `.d.ts` types, and by the offline tests in
 * `test/openai.test.ts`.
 *
 * Patch targets (prototype methods; every client instance shares them, so
 * clients created before or after `auto()` are covered). The package ships
 * separate CommonJS (`*.js`) and ESM (`*.mjs`) builds, which Node loads as
 * separate module instances with separate classes, so each target is patched
 * in both copies:
 *
 * | module                                  | class.method          | decision_type             |
 * |-----------------------------------------|-----------------------|---------------------------|
 * | `openai/resources/chat/completions/completions` | `Completions.create` | `llm.openai.chat`     |
 * | `openai/resources/responses/responses`  | `Responses.create`    | `llm.openai.responses`    |
 * | `openai/resources/completions`          | `Completions.create`  | `llm.openai.completions`  |
 * | `openai/resources/embeddings`           | `Embeddings.create`   | `llm.openai.embeddings`   |
 *
 * Layout facts relied on, as found in 7.62.0:
 *
 * - `chat.completions.parse()` calls `client.chat.completions.create()`;
 *   `chat.completions.stream()` (ChatCompletionStream) and
 *   `responses.stream()` / `responses.parse()` call `create()` too, with
 *   `options.__metadata.helperMethod` naming the helper. So they are
 *   covered by the `create` patches (one record per HTTP call) and the
 *   helper objects (`.on()`, `.finalChatCompletion()`) are untouched.
 * - `create()` returns an `APIPromise` (see `stainless.ts`); with
 *   `stream: true` it parses to a `Stream` (chat: after
 *   `normalizeChatCompletionStream`, which sets an own
 *   `[Symbol.asyncIterator]` that still calls `this.iterator()`).
 * - Usage: `CompletionUsage` (`prompt_tokens`, `completion_tokens`,
 *   `total_tokens`, `prompt_tokens_details.cached_tokens`,
 *   `completion_tokens_details.reasoning_tokens`) and `ResponseUsage`
 *   (`input_tokens`, `output_tokens`, `total_tokens`,
 *   `input_tokens_details.cached_tokens`,
 *   `output_tokens_details.reasoning_tokens`). The request id is the
 *   `x-request-id` response header (also `_request_id` on parsed bodies).
 * - A chat stream carries usage only when the request sets
 *   `stream_options: { include_usage: true }`; it is added (on a copy of
 *   the request body) only with `auto({ streamUsage: true })`, because it
 *   adds a final chunk with empty `choices` to the stream the app reads.
 * - Responses stream events: `response.created` / `response.completed` /
 *   `response.incomplete` / `response.failed` carry `response`;
 *   `response.output_text.delta` carries `delta`.
 */

import { tokenUsage } from "../record.js";
import { TextAccumulator, read, scalarParams } from "../util.js";
import {
  type CallInfo,
  type Instrumentor,
  type PatchTarget,
  type ProviderCall,
  type SdkFile,
  StreamState,
  stainlessFiles,
} from "./core.js";
import { type StainlessHooks, wrapStainlessMethod } from "./stainless.js";

export const PROVIDER = "openai";
export const PACKAGE = "openai";
export const VERIFIED_VERSION = "7.62.0";

const CHAT_PARAMS = [
  "temperature", "top_p", "max_tokens", "max_completion_tokens", "n", "seed",
  "frequency_penalty", "presence_penalty", "reasoning_effort", "service_tier",
] as const;
const RESPONSES_PARAMS = ["temperature", "top_p", "max_output_tokens", "service_tier", "truncation"] as const;
const COMPLETIONS_PARAMS = ["temperature", "top_p", "max_tokens", "n", "seed", "frequency_penalty", "presence_penalty"] as const;
const EMBEDDINGS_PARAMS = ["dimensions", "encoding_format"] as const;

function completionUsage(usage: unknown) {
  return tokenUsage(read(usage, "prompt_tokens"), read(usage, "completion_tokens"), read(usage, "total_tokens"), {
    cached_tokens: read(usage, "prompt_tokens_details", "cached_tokens"),
    reasoning_tokens: read(usage, "completion_tokens_details", "reasoning_tokens"),
  });
}

function responseUsage(usage: unknown) {
  return tokenUsage(read(usage, "input_tokens"), read(usage, "output_tokens"), read(usage, "total_tokens"), {
    cached_tokens: read(usage, "input_tokens_details", "cached_tokens"),
    reasoning_tokens: read(usage, "output_tokens_details", "reasoning_tokens"),
  });
}

function firstChoice(obj: unknown): unknown {
  const choices = read(obj, "choices");
  return Array.isArray(choices) && choices.length > 0 ? choices[0] : undefined;
}

function chatInfo(result: unknown, wantContent: boolean): CallInfo {
  const choice = firstChoice(result);
  const info: CallInfo = {
    model: read(result, "model"),
    response_id: read(result, "id"),
    request_id: read(result, "_request_id"),
    finish_reason: read(choice, "finish_reason"),
    usage: completionUsage(read(result, "usage")),
    extra: { service_tier: read(result, "service_tier") },
  };
  if (wantContent) {
    const message = read(choice, "message");
    const content: Record<string, unknown> = { content: read(message, "content") ?? null };
    const toolCalls = read(message, "tool_calls");
    if (Array.isArray(toolCalls) && toolCalls.length) content["tool_calls"] = toolCalls;
    const refusal = read(message, "refusal");
    if (refusal) content["refusal"] = refusal;
    info.content = content;
  }
  return info;
}

function responsesInfo(result: unknown, wantContent: boolean): CallInfo {
  const info: CallInfo = {
    model: read(result, "model"),
    response_id: read(result, "id"),
    request_id: read(result, "_request_id"),
    finish_reason: read(result, "incomplete_details", "reason") ?? read(result, "status"),
    usage: responseUsage(read(result, "usage")),
    extra: { service_tier: read(result, "service_tier"), response_error_code: read(result, "error", "code") },
  };
  if (wantContent) info.content = { content: read(result, "output_text") ?? null };
  return info;
}

function completionsInfo(result: unknown, wantContent: boolean): CallInfo {
  const choice = firstChoice(result);
  const info: CallInfo = {
    model: read(result, "model"),
    response_id: read(result, "id"),
    request_id: read(result, "_request_id"),
    finish_reason: read(choice, "finish_reason"),
    usage: completionUsage(read(result, "usage")),
  };
  if (wantContent) info.content = { content: read(choice, "text") ?? null };
  return info;
}

function embeddingsInfo(result: unknown): CallInfo {
  const usage = read(result, "usage");
  const data = read(result, "data");
  // Embeddings have no completion side: 0 is what the API reports, not a guess.
  return {
    model: read(result, "model"),
    request_id: read(result, "_request_id"),
    usage: tokenUsage(read(usage, "prompt_tokens"), read(usage, "prompt_tokens") === undefined ? undefined : 0, read(usage, "total_tokens")),
    extra: { embedding_count: Array.isArray(data) ? data.length : undefined },
  };
}

class OpenAIStream extends StreamState {
  protected readonly text: TextAccumulator;
  protected data: CallInfo;
  constructor(call: ProviderCall, protected readonly inst: Instrumentor, requestId: string | null) {
    super(call);
    this.text = new TextAccumulator(inst.options.maxContentChars);
    this.data = { request_id: requestId };
  }
  observe(_chunk: unknown): void {}
  info(): CallInfo {
    const info: CallInfo = { ...this.data };
    if (this.inst.capturesContent) info.content = { content: this.text.text };
    return info;
  }
}

/** `ChatCompletionChunk` (and legacy `Completion`) chunks. */
class ChatStream extends OpenAIStream {
  override observe(chunk: unknown): void {
    const model = read(chunk, "model");
    if (model) this.data.model = model;
    const id = read(chunk, "id");
    if (id) this.data.response_id = id;
    const usage = completionUsage(read(chunk, "usage"));
    if (usage) this.data.usage = usage;
    const choice = firstChoice(chunk);
    if (choice !== undefined) {
      const finish = read(choice, "finish_reason");
      if (finish) this.data.finish_reason = finish;
      if (this.inst.capturesContent) this.text.add(read(choice, "delta", "content") ?? read(choice, "text"));
    }
  }
}

const TERMINAL = new Set(["response.created", "response.completed", "response.incomplete", "response.failed"]);

/** `ResponseStreamEvent` events. */
class ResponsesStream extends OpenAIStream {
  override observe(event: unknown): void {
    const kind = read(event, "type");
    if (typeof kind === "string" && TERMINAL.has(kind)) {
      const response = read(event, "response");
      if (response) {
        const requestId = this.data.request_id;
        const next = responsesInfo(response, false);
        this.data = { ...next, request_id: next.request_id ?? requestId };
      }
    } else if (kind === "response.output_text.delta" && this.inst.capturesContent) {
      this.text.add(read(event, "delta"));
    }
  }
}

interface Surface {
  specifier: string;
  className: string;
  decisionType: string;
  functionBase: string;
  params: readonly string[];
  inputKeys: readonly string[];
  info: (result: unknown, wantContent: boolean) => CallInfo;
  stream?: new (call: ProviderCall, inst: Instrumentor, requestId: string | null) => StreamState;
  streamUsage?: boolean;
}

export const SURFACES: Surface[] = [
  {
    specifier: "openai/resources/chat/completions/completions",
    className: "Completions",
    decisionType: "llm.openai.chat",
    functionBase: "openai.chat.completions",
    params: CHAT_PARAMS,
    inputKeys: ["messages", "tools"],
    info: chatInfo,
    stream: ChatStream,
    streamUsage: true,
  },
  {
    specifier: "openai/resources/responses/responses",
    className: "Responses",
    decisionType: "llm.openai.responses",
    functionBase: "openai.responses",
    params: RESPONSES_PARAMS,
    inputKeys: ["instructions", "input", "tools"],
    info: responsesInfo,
    stream: ResponsesStream,
  },
  {
    specifier: "openai/resources/completions",
    className: "Completions",
    decisionType: "llm.openai.completions",
    functionBase: "openai.completions",
    params: COMPLETIONS_PARAMS,
    inputKeys: ["prompt"],
    info: completionsInfo,
    stream: ChatStream,
  },
  {
    specifier: "openai/resources/embeddings",
    className: "Embeddings",
    decisionType: "llm.openai.embeddings",
    functionBase: "openai.embeddings",
    params: EMBEDDINGS_PARAMS,
    inputKeys: ["input"],
    info: (result) => embeddingsInfo(result),
  },
];

/** `options.__metadata.helperMethod` (e.g. "stream", "chat.completions.parse") to a method name. */
function methodName(options: unknown): string {
  const helper = read(options, "__metadata", "helperMethod");
  if (typeof helper === "string" && helper) return helper.split(".").pop() || "create";
  return "create";
}

function given(value: unknown): boolean {
  return value !== undefined && value !== null;
}

function targetFor(inst: Instrumentor, surface: Surface, owner: unknown): PatchTarget {
  const begin = (args: unknown[]): ProviderCall | null => {
    const body = args[0];
    if (!body || typeof body !== "object") return null;
    return inst.begin(surface.decisionType, `${surface.functionBase}.${methodName(args[1])}`, {
      model: read(body, "model"),
      params: scalarParams(body, surface.params),
      inputs: () => {
        const out: Record<string, unknown> = {};
        for (const key of surface.inputKeys) if (given(read(body, key))) out[key] = read(body, key);
        return out;
      },
      stream: read(body, "stream") === true,
    });
  };
  const hooks: StainlessHooks = {
    requestIdHeader: "x-request-id",
    info: (parsed) => surface.info(parsed, inst.capturesContent),
    streamState: (call, requestId) =>
      surface.stream ? new surface.stream(call, inst, requestId) : new (class extends OpenAIStream {})(call, inst, requestId),
  };
  const prepare =
    surface.streamUsage && inst.options.streamUsage
      ? (args: unknown[]): unknown[] => {
          const body = args[0] as Record<string, unknown>;
          if (body["stream"] !== true || (body["stream_options"] && typeof body["stream_options"] === "object")) return args;
          return [{ ...body, stream_options: { include_usage: true } }, ...args.slice(1)];
        }
      : undefined;
  return {
    kind: "method",
    owner: read(owner, "prototype"),
    key: "create",
    label: `${surface.specifier}#${surface.className}.create`,
    wrap: wrapStainlessMethod(inst, begin, hooks, prepare),
  };
}

/** Builds the patch targets for one module copy (`modules[specifier]` is that copy's namespace). */
export function targets(inst: Instrumentor, modules: Record<string, Record<string, unknown> | undefined>): PatchTarget[] {
  return SURFACES.map((surface) => targetFor(inst, surface, modules[surface.specifier]?.[surface.className]));
}

export const SPECIFIERS = SURFACES.map((s) => s.specifier);
export const FILES: SdkFile[] = stainlessFiles(PACKAGE, SPECIFIERS);
