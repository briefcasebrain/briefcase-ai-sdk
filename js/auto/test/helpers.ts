/**
 * Offline fakes: every SDK is given a transport that returns canned JSON or
 * SSE bodies through its documented injection point (`fetch` for the
 * Stainless SDKs, `httpOptions.fetch` for @google/genai, `requestHandler`
 * for the AWS SDK). No test reaches the network.
 */

import { crc32 } from "node:zlib";

import { auto, undo, type AutoOptions } from "../src/auto.js";
import { MemoryExporter } from "../src/exporters.js";
import { observe, shutdown } from "../src/observe.js";
import type { DecisionRecord } from "../src/record.js";
import { state } from "../src/state.js";

export const SECRET_KEY = "sk-test-secret-0123456789";

/** Undo every patch, drop all state, then `observe("memory")` + `auto(options)`. */
export async function fresh(options: AutoOptions = {}): Promise<MemoryExporter> {
  undo();
  await shutdown();
  const s = state();
  s.providers.clear();
  s.pending.clear();
  s.pipeline = null;
  const mem = observe(new MemoryExporter());
  auto(options);
  return mem;
}

/** Waits until queued records have reached the exporter. */
export async function records(mem: MemoryExporter, count?: number): Promise<DecisionRecord[]> {
  for (let i = 0; i < 100; i++) {
    await state().pipeline?.flush();
    if (count === undefined || mem.records.length >= count) break;
    await new Promise((r) => setTimeout(r, 5));
  }
  return mem.records;
}

export interface FetchCall {
  url: string;
  init: RequestInit;
  body: Record<string, unknown> | null;
}

export type Responder = (call: FetchCall) => Response | Promise<Response>;

/** A `fetch` that records each request and answers with `responder`. */
export function fakeFetch(responder: Responder): typeof fetch & { calls: FetchCall[] } {
  const calls: FetchCall[] = [];
  const fn = (async (input: string | URL | Request, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    let body: Record<string, unknown> | null = null;
    if (typeof init.body === "string") {
      try {
        body = JSON.parse(init.body) as Record<string, unknown>;
      } catch {
        body = null;
      }
    }
    const call = { url, init, body };
    calls.push(call);
    return responder(call);
  }) as typeof fetch & { calls: FetchCall[] };
  fn.calls = calls;
  return fn;
}

export function json(payload: unknown, headers: Record<string, string> = {}, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

/**
 * A streamed `text/event-stream` body. Each entry is one SSE event (an
 * object is sent as `data: <json>`; a string is sent verbatim). With
 * `hang`, the body never ends after the last event (for abort tests).
 */
export function sse(
  events: (unknown | string)[],
  headers: Record<string, string> = {},
  options: { hang?: boolean; signal?: AbortSignal | null | undefined } = {},
): Response {
  const encoder = new TextEncoder();
  const frames = events.map((e) => (typeof e === "string" ? e : `data: ${JSON.stringify(e)}\n\n`));
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      // Like a real fetch body: aborting the request errors the body.
      options.signal?.addEventListener("abort", () => {
        try {
          controller.error(new DOMException("This operation was aborted", "AbortError"));
        } catch {
          // already closed
        }
      });
    },
    pull(controller) {
      if (i < frames.length) {
        controller.enqueue(encoder.encode(frames[i++]!));
        return;
      }
      if (options.hang) return new Promise(() => {});
      controller.close();
      return;
    },
  });
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream", ...headers } });
}

/** Encodes one AWS event-stream message (prelude, string headers, payload, CRC32s). */
export function eventStreamMessage(headers: Record<string, string>, payload: unknown): Uint8Array {
  const headerBufs: Buffer[] = [];
  for (const [name, value] of Object.entries(headers)) {
    const n = Buffer.from(name, "utf8");
    const v = Buffer.from(value, "utf8");
    const h = Buffer.alloc(1 + n.length + 1 + 2 + v.length);
    let o = 0;
    h.writeUInt8(n.length, o);
    o += 1;
    n.copy(h, o);
    o += n.length;
    h.writeUInt8(7, o); // string
    o += 1;
    h.writeUInt16BE(v.length, o);
    o += 2;
    v.copy(h, o);
    headerBufs.push(h);
  }
  const headerBytes = Buffer.concat(headerBufs);
  const body = Buffer.from(typeof payload === "string" ? payload : JSON.stringify(payload), "utf8");
  const total = 12 + headerBytes.length + body.length + 4;
  const prelude = Buffer.alloc(8);
  prelude.writeUInt32BE(total, 0);
  prelude.writeUInt32BE(headerBytes.length, 4);
  const preludeCrc = Buffer.alloc(4);
  preludeCrc.writeUInt32BE(crc32(prelude) >>> 0, 0);
  const withoutCrc = Buffer.concat([prelude, preludeCrc, headerBytes, body]);
  const messageCrc = Buffer.alloc(4);
  messageCrc.writeUInt32BE(crc32(withoutCrc) >>> 0, 0);
  return new Uint8Array(Buffer.concat([withoutCrc, messageCrc]));
}

export function bedrockEvent(eventType: string, payload: unknown): Uint8Array {
  return eventStreamMessage({ ":event-type": eventType, ":content-type": "application/json", ":message-type": "event" }, payload);
}

/** Every record, as JSON, must not contain the API key or an auth header. */
export function assertNoSecrets(all: unknown[]): void {
  const text = JSON.stringify(all).toLowerCase();
  if (text.includes(SECRET_KEY.toLowerCase())) throw new Error("API key leaked into a record");
  for (const needle of ["authorization", "x-api-key", "x-goog-api-key", "bearer "]) {
    if (text.includes(needle)) throw new Error(`header ${needle} leaked into a record`);
  }
}
