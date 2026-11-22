/**
 * Instrumentation for the `@google/genai` Node SDK.
 *
 * Verified against @google/genai 3.6.1 (npm), by reading the installed
 * package's `dist/node/index.mjs` / `index.cjs` and `dist/node/node.d.ts`,
 * and by the offline tests in `test/google-genai.test.ts`.
 *
 * Patch technique: in 3.6.1 `Models.generateContent` and
 * `Models.generateContentStream` are NOT prototype methods. The `Models`
 * constructor assigns them per instance (`this.generateContent = async
 * (params) => ...`, a plain assignment in the compiled output). We install
 * an accessor for each name on `Models.prototype`: the constructor's
 * assignment runs our setter, which stores the wrapped function as the
 * instance's own, writable, enumerable data property (the same shape the
 * SDK creates). After applying, a probe `new Models(undefined)` checks that
 * the assignment really went through the setter; if a future SDK defines
 * them differently, the copy is rolled back and skipped with a warning.
 * Clients constructed BEFORE the patch are covered through the internal
 * methods on the shared `Models.prototype` that those instance functions
 * call (see `targets()` below).
 * Both the ESM (`dist/node/index.mjs`) and CJS (`dist/node/index.cjs`)
 * copies are patched, and the bundler builds (`dist/index.*`) when loaded.
 *
 * Recorded as `decision_type: "llm.google_genai.generate_content"`.
 * `client.chats` sends through the same `Models` instance, so chat sessions
 * are covered. Automatic function calling and automatic continuation make
 * several HTTP requests inside one `generateContent` call; that is one
 * record with the final response's usage (as in Python).
 *
 * Fields read (3.6.1 `GenerateContentResponse`): `usageMetadata`
 * (`promptTokenCount`, `candidatesTokenCount`, `totalTokenCount`,
 * `cachedContentTokenCount`, `thoughtsTokenCount`), `modelVersion`,
 * `responseId`, `candidates[0].finishReason`. Every stream chunk is a
 * `GenerateContentResponse`. No request id is exposed. Request config
 * names are recorded under Python's snake_case names (`top_p`, ...).
 */

import { tokenUsage } from "../record.js";
import { TextAccumulator, read, safe } from "../util.js";
import {
  type CallInfo,
  type Instrumentor,
  type PatchTarget,
  type ProviderCall,
  type SdkFile,
  StreamState,
  WRAPPED,
  callStore,
  finishWhenCollected,
  instrumentAsyncIterable,
} from "./core.js";

export const PROVIDER = "google-genai";
export const PACKAGE = "@google/genai";
export const VERIFIED_VERSION = "3.6.1";
export const DECISION_TYPE = "llm.google_genai.generate_content";
export const SPECIFIERS = ["@google/genai"];
/** `dist/node` is what Node resolves; `dist/index.*` is what a bundler without the `node` condition picks. */
export const FILES: SdkFile[] = [
  { specifier: "@google/genai", rel: "dist/node/index.mjs", format: "esm", group: "node" },
  { specifier: "@google/genai", rel: "dist/node/index.cjs", format: "cjs", group: "node" },
  { specifier: "@google/genai", rel: "dist/index.mjs", format: "esm", group: "default" },
  { specifier: "@google/genai", rel: "dist/index.cjs", format: "cjs", group: "default" },
];

const PARAMS: Record<string, string> = {
  temperature: "temperature",
  topP: "top_p",
  topK: "top_k",
  maxOutputTokens: "max_output_tokens",
  candidateCount: "candidate_count",
  seed: "seed",
};

function configParams(config: unknown): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [js, py] of Object.entries(PARAMS)) {
    const value = read(config, js);
    if (typeof value === "number" && Number.isFinite(value)) out[py] = value;
  }
  return out;
}

function usage(meta: unknown) {
  return tokenUsage(read(meta, "promptTokenCount"), read(meta, "candidatesTokenCount"), read(meta, "totalTokenCount"), {
    cached_content_token_count: read(meta, "cachedContentTokenCount"),
    thoughts_token_count: read(meta, "thoughtsTokenCount"),
  });
}

function firstCandidate(response: unknown): unknown {
  const candidates = read(response, "candidates");
  return Array.isArray(candidates) && candidates.length ? candidates[0] : undefined;
}

/** Text parts of the first candidate, skipping thoughts (read directly: `.text` warns on non-text parts). */
function text(response: unknown): string {
  const parts = read(firstCandidate(response), "content", "parts");
  let out = "";
  for (const part of Array.isArray(parts) ? parts : []) {
    const t = read(part, "text");
    if (typeof t === "string" && !read(part, "thought")) out += t;
  }
  return out;
}

function info(response: unknown, wantContent: boolean): CallInfo {
  const result: CallInfo = {
    model: read(response, "modelVersion"),
    response_id: read(response, "responseId"),
    finish_reason: read(firstCandidate(response), "finishReason"),
    usage: usage(read(response, "usageMetadata")),
  };
  if (wantContent) result.content = { content: text(response) };
  return result;
}

class GenerateStream extends StreamState {
  private readonly acc: TextAccumulator;
  private data: CallInfo = {};
  constructor(call: ProviderCall, private readonly inst: Instrumentor) {
    super(call);
    this.acc = new TextAccumulator(inst.options.maxContentChars);
  }
  observe(chunk: unknown): void {
    for (const [key, value] of Object.entries(info(chunk, false))) {
      if (value !== undefined && value !== null) (this.data as Record<string, unknown>)[key] = value;
    }
    if (this.inst.capturesContent) this.acc.add(text(chunk));
  }
  info(): CallInfo {
    const out: CallInfo = { ...this.data };
    if (this.inst.capturesContent) out.content = { content: this.acc.text };
    return out;
  }
}

function begin(inst: Instrumentor, functionName: string, params: unknown, stream: boolean): ProviderCall | null {
  if (!params || typeof params !== "object") return null;
  const config = read(params, "config");
  return inst.begin(DECISION_TYPE, functionName, {
    model: read(params, "model"),
    params: configParams(config),
    inputs: () => {
      const out: Record<string, unknown> = { contents: read(params, "contents") };
      const system = read(config, "systemInstruction");
      if (system !== undefined) out["system_instruction"] = system;
      return out;
    },
    stream,
  });
}

type Fn = (...args: unknown[]) => unknown;

/** Runs one non-streamed generate call under `call` and records its result. */
function runGenerate(inst: Instrumentor, call: ProviderCall, invoke: () => unknown): unknown {
  let pending: unknown;
  try {
    pending = callStore.run(call, invoke);
  } catch (error) {
    call.finish({}, { error });
    throw error;
  }
  return Promise.resolve(pending).then(
    (response) => {
      safe(() => call.finish(info(response, inst.capturesContent)), "finish");
      return response;
    },
    (error: unknown) => {
      call.finish({}, { error });
      throw error;
    },
  );
}

/** Runs one streamed generate call under `call`; the SDK's generator is instrumented in place. */
function runStream(inst: Instrumentor, call: ProviderCall, invoke: () => unknown): unknown {
  let pending: unknown;
  try {
    pending = callStore.run(call, invoke);
  } catch (error) {
    call.finish({}, { error });
    throw error;
  }
  return Promise.resolve(pending).then(
    (generator) => {
      safe(() => {
        const state = new GenerateStream(call, inst);
        const ok = instrumentAsyncIterable(
          generator,
          { onItem: (chunk) => state.onItem(chunk), onEnd: (kind, error) => state.onEnd(kind, error) },
          // Later hops (automatic continuation / function calling) are requested while the
          // consumer iterates: run them inside the call so they count as covered.
          (fn) => callStore.run(call, fn),
        );
        if (!ok) call.finish({}, { stream: { chunk_count: 0, stream_completed: false } });
        else finishWhenCollected(generator as object, call);
      }, "stream instrumentation");
      return generator;
    },
    (error: unknown) => {
      call.finish({}, { error });
      throw error;
    },
  );
}

/**
 * Patch targets for one copy of the package:
 *
 * 1. `generateContent` / `generateContentStream` accessors on
 *    `Models.prototype` (see the header): every client constructed after
 *    the patch records one record per public call. Each such instance is
 *    remembered in `covered`.
 * 2. Clients constructed BEFORE the patch keep the SDK's own, unwrapped
 *    instance functions. Those functions do their work through methods
 *    that live on the shared `Models.prototype` (read in 3.6.1):
 *    non-streamed calls through `generateContentWithContinuation` (which
 *    calls `generateContentInternal` once per continuation hop) or
 *    `generateContentInternal` directly; streamed calls through
 *    `processAfcStream` (default), `generateContentStreamWithContinuation`
 *    or `generateContentStreamInternal`. Those five are wrapped. A wrapper
 *    records only for an instance that is not in `covered` and only for
 *    the outermost of them (an inner one runs inside the outer call's
 *    context and passes through), so each public call is one record.
 *    Exception, documented: non-streamed automatic function calling
 *    calls `generateContentWithContinuation` once per model round from
 *    the instance function itself, so a pre-existing client records one
 *    record per round there (each with that round's usage).
 */
export function targets(
  inst: Instrumentor,
  modules: Record<string, Record<string, unknown> | undefined>,
): { targets: PatchTarget[]; verify: () => string | null } {
  const Models = modules["@google/genai"]?.["Models"] as (new (client: unknown) => object) | undefined;
  const owner = read(Models, "prototype");
  const fnBase = "google.genai.models";
  const covered = new WeakSet<object>();
  const remember = (instance: object): void => {
    covered.add(instance);
  };

  const generate: PatchTarget = {
    kind: "instance-assign",
    owner,
    key: "generateContent",
    label: "@google/genai#Models.generateContent",
    assigned: remember,
    wrap: (original) =>
      function (this: unknown, ...args: unknown[]): unknown {
        const call = safe(() => begin(inst, `${fnBase}.generate_content`, args[0], false), "begin") ?? null;
        if (!call) return original.apply(this, args);
        call.owner = this;
        return runGenerate(inst, call, () => original.apply(this, args));
      },
  };

  const generateStream: PatchTarget = {
    kind: "instance-assign",
    owner,
    key: "generateContentStream",
    label: "@google/genai#Models.generateContentStream",
    assigned: remember,
    wrap: (original) =>
      function (this: unknown, ...args: unknown[]): unknown {
        const call = safe(() => begin(inst, `${fnBase}.generate_content_stream`, args[0], true), "begin") ?? null;
        if (!call) return original.apply(this, args);
        call.owner = this;
        return runStream(inst, call, () => original.apply(this, args));
      },
  };

  const seam = (key: string, stream: boolean): PatchTarget => ({
    kind: "method",
    owner,
    key,
    label: `@google/genai#Models.${key} (clients created before auto())`,
    wrap: (original: Fn) =>
      function (this: unknown, ...args: unknown[]): unknown {
        const self = this as object;
        if (!inst.active || !self || covered.has(self) || callStore.getStore()?.owner === self) return original.apply(this, args);
        const name = stream ? `${fnBase}.generate_content_stream` : `${fnBase}.generate_content`;
        const call = safe(() => begin(inst, name, args[0], stream), "begin") ?? null;
        if (!call) return original.apply(this, args);
        call.owner = self;
        return stream ? runStream(inst, call, () => original.apply(this, args)) : runGenerate(inst, call, () => original.apply(this, args));
      },
  });

  const verify = (): string | null => {
    if (typeof Models !== "function") return "Models is not a class";
    const probe = new Models(undefined) as Record<string, unknown>;
    for (const key of ["generateContent", "generateContentStream"]) {
      const desc = Object.getOwnPropertyDescriptor(probe, key);
      const fn = desc?.value as Record<symbol, unknown> | undefined;
      if (!desc || typeof fn !== "function" || !fn[WRAPPED]) {
        return `new Models() did not assign ${key} through the prototype (the SDK defines it differently)`;
      }
    }
    return null;
  };

  return {
    targets: [
      generate,
      generateStream,
      seam("generateContentInternal", false),
      seam("generateContentWithContinuation", false),
      seam("generateContentStreamInternal", true),
      seam("generateContentStreamWithContinuation", true),
      seam("processAfcStream", true),
    ],
    verify,
  };
}
