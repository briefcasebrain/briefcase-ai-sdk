/**
 * Helpers for the Stainless-generated SDKs (`openai`, `@anthropic-ai/sdk`).
 *
 * Verified by reading `core/api-promise.mjs` and `core/streaming.mjs` in
 * openai 7.62.0 and @anthropic-ai/sdk 1.12.0:
 *
 * - A resource method returns an `APIPromise` (a `Promise` subclass) holding
 *   two own fields: `responsePromise` (the raw response, body unread) and
 *   `parseResponse(client, props)`. `.then` / `.withResponse()` / `.parse()`
 *   parse through `this.parseResponse`; `.asResponse()` reads
 *   `this.responsePromise` and never parses. `_thenUnwrap` builds a new
 *   APIPromise from `this.responsePromise` and `this.parseResponse`.
 * - So we keep the caller's APIPromise object and replace only those two
 *   fields on it: the response promise is observed (errors, request id)
 *   without reading the body, and the parse step reports the parsed value.
 *   A response that is only ever read with `.asResponse()` is recorded with
 *   its request id but no usage, when the promise is garbage-collected or
 *   the process exits.
 * - `stream: true` parses to a `Stream` whose own `iterator` field is the
 *   source generator factory used by `[Symbol.asyncIterator]`, `tee()` and
 *   `toReadableStream()`. We replace that field on the same object.
 */

import {
  type CallInfo,
  type Instrumentor,
  type ProviderCall,
  type StreamState,
  finishWhenCollected,
  isAbortError,
  observeAsyncIterator,
} from "./core.js";
import { debug, describeError, read, safe } from "../util.js";

export interface StainlessHooks {
  /** Header carrying the request id (`x-request-id` / `request-id`). */
  requestIdHeader: string;
  /** Record fields from a parsed, non-streamed result. */
  info(parsed: unknown, requestId: string | null): CallInfo;
  /** Bookkeeping for a streamed result. */
  streamState(call: ProviderCall, requestId: string | null): StreamState;
}

type ParseFn = (this: unknown, client: unknown, props: unknown) => Promise<unknown>;

function isApiPromise(value: unknown): value is { responsePromise: Promise<unknown>; parseResponse: ParseFn } {
  if (!value || typeof value !== "object") return false;
  const rp = (value as Record<string, unknown>)["responsePromise"];
  const parse = (value as Record<string, unknown>)["parseResponse"];
  return !!rp && typeof (rp as { then?: unknown }).then === "function" && typeof parse === "function";
}

function header(props: unknown, name: string): string | null {
  try {
    const headers = read(props, "response", "headers") as { get?: (n: string) => string | null } | undefined;
    const value = headers?.get?.(name);
    return typeof value === "string" && value ? value : null;
  } catch {
    return null;
  }
}

function isStainlessStream(value: unknown): value is { iterator: (...args: unknown[]) => AsyncIterator<unknown>; controller?: AbortController } {
  return !!value && typeof value === "object" && typeof (value as Record<string, unknown>)["iterator"] === "function";
}

/** Reports a streamed result's chunks and end; keeps the caller's Stream object. */
export function instrumentStainlessStream(stream: unknown, call: ProviderCall, state: StreamState): void {
  if (!isStainlessStream(stream)) {
    debug("stream result has no iterator field; recorded without its chunks");
    call.finish({}, { stream: { chunk_count: 0, stream_completed: false } });
    return;
  }
  const original = stream.iterator;
  const target = stream as { iterator: unknown; controller?: AbortController };
  target.iterator = function (this: unknown, ...args: unknown[]) {
    const inner = original.apply(this, args);
    return observeAsyncIterator(inner, {
      onItem: (item) => state.onItem(item),
      onEnd: (kind, error) => {
        const aborted = !!target.controller?.signal?.aborted;
        state.onEnd(kind, error, aborted);
      },
    });
  };
  finishWhenCollected(stream as object, call);
}

/**
 * Observes an APIPromise in place. Returns false when `result` does not have
 * the verified APIPromise shape (the caller then finishes the call itself).
 */
export function observeApiPromise(
  result: unknown,
  call: ProviderCall,
  inst: Instrumentor,
  hooks: StainlessHooks,
): boolean {
  if (!isApiPromise(result)) return false;
  const promise = result;
  const originalResponse = promise.responsePromise;
  const originalParse = promise.parseResponse;
  let requestId: string | null = null;
  let parsedOnce = false;

  promise.responsePromise = originalResponse.then(
    (props) => {
      safe(() => {
        requestId = header(props, hooks.requestIdHeader);
        call.fallback = { request_id: requestId };
      }, "response hook");
      return props;
    },
    (error: unknown) => {
      call.finish({}, { error });
      throw error;
    },
  );

  promise.parseResponse = async function (this: unknown, client: unknown, props: unknown): Promise<unknown> {
    let parsed: unknown;
    try {
      parsed = await originalParse.call(this, client, props);
    } catch (error) {
      call.finish({ request_id: requestId }, { error });
      throw error;
    }
    if (!parsedOnce && inst.active) {
      parsedOnce = true;
      try {
        if (read(props, "options", "stream") === true || isStainlessStream(parsed)) {
          call.markStream();
          instrumentStainlessStream(parsed, call, hooks.streamState(call, requestId));
        } else {
          const info = hooks.info(parsed, requestId);
          if (info.request_id === undefined || info.request_id === null) info.request_id = requestId;
          call.finish(info);
        }
      } catch (error) {
        debug(`could not read the parsed response: ${describeError(error)}`);
        call.finish({ request_id: requestId });
      }
    }
    return parsed;
  };
  finishWhenCollected(promise as object, call);
  return true;
}

/** Wraps a Stainless resource method (`create`). */
export function wrapStainlessMethod(
  inst: Instrumentor,
  begin: (args: unknown[]) => ProviderCall | null,
  hooks: StainlessHooks,
  prepareArgs?: (args: unknown[]) => unknown[],
): (original: (...args: unknown[]) => unknown) => (...args: unknown[]) => unknown {
  return (original) =>
    function (this: unknown, ...args: unknown[]): unknown {
      const call = safe(() => begin(args), "begin") ?? null;
      if (!call) return original.apply(this, args);
      const callArgs = prepareArgs ? (safe(() => prepareArgs(args), "prepare args") ?? args) : args;
      let result: unknown;
      try {
        result = original.apply(this, callArgs);
      } catch (error) {
        call.finish({}, { error });
        throw error;
      }
      const observed = safe(() => observeApiPromise(result, call, inst, hooks), "observe") ?? false;
      if (!observed) {
        // Not the verified APIPromise shape: follow it as a plain thenable without changing it.
        const then = (result as { then?: unknown } | null)?.then;
        if (typeof then === "function") {
          (result as Promise<unknown>).then(
            (value) => call.finish(safe(() => hooks.info(value, null), "info") ?? {}),
            (error: unknown) => call.finish({}, { error }),
          );
        } else {
          call.finish({});
        }
      }
      return result;
    };
}

export { isAbortError };
