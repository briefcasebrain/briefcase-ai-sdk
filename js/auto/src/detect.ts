/**
 * Notices provider requests that were NOT recorded: a request made by an
 * SDK copy this package did not patch (an SDK bundled into the application
 * without the esbuild plugin, a copy loaded some other way). It warns once
 * per provider and counts them in `status().providers[i].unrecordedRequests`.
 *
 * Seams: Node's built-in diagnostics channels, which only observe:
 * `undici:request:create` (global `fetch`: openai, @anthropic-ai/sdk,
 * @google/genai), `http.client.request.created` and
 * `http2.client.stream.created` (the AWS SDK's Node handlers; Bedrock
 * Runtime 3.1175.0 uses HTTP/2). A request counts as recorded when it is
 * made inside a call our wrappers are recording (`callStore`).
 *
 * Fingerprints, as sent by the verified versions (read from a local test
 * server): Stainless SDKs send `x-stainless-lang`; Anthropic also sends
 * `anthropic-version`, OpenAI a `User-Agent` of `OpenAI/JS <version>`
 * (`<constructor name>/JS`). @google/genai sends `x-goog-api-client:
 * google-genai-sdk/<version>`; the AWS SDK sends `api/bedrock-runtime#<version>`
 * in its user agent. Only the endpoints this package records are counted.
 * A request made with a custom `fetch` is invisible here.
 */

import { subscribe } from "node:diagnostics_channel";

import { callStore } from "./instrument/core.js";
import { state } from "./state.js";
import { safe, warn } from "./util.js";

type Getter = (name: string) => string | undefined;

export function classify(get: Getter, path: string): string | null {
  if (get("x-stainless-lang") !== undefined) {
    if (get("anthropic-version") !== undefined) return /\/v1\/messages(\?|$)/.test(path) ? "anthropic" : null;
    const ua = get("user-agent") ?? "";
    // `<constructor name>/JS`: OpenAI, AzureOpenAI, or a short minified name.
    if (!/^(?:Azure)?OpenAI\/JS |^[A-Za-z_$][\w$]{0,2}\/JS /.test(ua)) return null;
    return /\/(?:chat\/completions|responses|completions|embeddings)(\?|$)/.test(path) ? "openai" : null;
  }
  if ((get("x-goog-api-client") ?? "").includes("google-genai-sdk/")) {
    return /:(?:generateContent|streamGenerateContent)(\?|$)/.test(path) ? "google-genai" : null;
  }
  const ua = `${get("x-amz-user-agent") ?? ""} ${get("user-agent") ?? ""}`;
  if (ua.includes("api/bedrock-runtime")) {
    return /\/model\/[^/]+\/(?:converse|converse-stream|invoke|invoke-with-response-stream)(\?|$)/.test(path) ? "bedrock" : null;
  }
  return null;
}

const warned = new Set<string>();

function seen(provider: string | null): void {
  if (!provider) return;
  const recording = callStore.getStore();
  if (recording && recording.provider === provider) return;
  const s = state();
  const pipeline = s.pipeline;
  if (!pipeline || pipeline.closed) return;
  const report = s.providers.get(provider);
  // Only for providers auto() was asked to cover and that were not undone or skipped by the layout check.
  if (!report || report.state === "undone" || report.state === "skipped" || !report.instrumentor?.active) return;
  report.unrecordedRequests = (report.unrecordedRequests ?? 0) + 1;
  if (warned.has(provider)) return;
  warned.add(provider);
  warn(
    `a request through the ${provider} SDK was made by a copy of the SDK that is not instrumented, so it was not recorded ` +
      "(usually an SDK bundled into the application). Use the @briefcase-ai/auto/esbuild plugin, keep the SDK " +
      "external to the bundle, or call instrument(client). Counted in status().providers[].unrecordedRequests.",
  );
}

function headerArray(headers: unknown): Getter {
  return (name) => {
    if (Array.isArray(headers)) {
      for (let i = 0; i + 1 < headers.length; i += 2) {
        if (String(headers[i]).toLowerCase() === name) return String(headers[i + 1]);
      }
      return undefined;
    }
    if (typeof headers === "string") {
      for (const line of headers.split("\r\n")) {
        const colon = line.indexOf(":");
        if (colon > 0 && line.slice(0, colon).trim().toLowerCase() === name) return line.slice(colon + 1).trim();
      }
    }
    return undefined;
  };
}

/** Subscribes once per thread. The handlers never throw. */
export function installDetection(): void {
  const s = state();
  if (s.detecting) return;
  s.detecting = true;
  safe(() => {
    subscribe("undici:request:create", (message) => {
      safe(() => {
        const request = (message as { request?: { path?: unknown; headers?: unknown } }).request;
        if (!request) return;
        seen(classify(headerArray(request.headers), String(request.path ?? "")));
      }, "request detection");
    });
    subscribe("http.client.request.created", (message) => {
      safe(() => {
        const request = (message as { request?: { path?: unknown; getHeader?: (n: string) => unknown } }).request;
        if (!request || typeof request.getHeader !== "function") return;
        const get: Getter = (name) => {
          const v = request.getHeader!(name);
          return v === undefined ? undefined : String(v);
        };
        seen(classify(get, String(request.path ?? "")));
      }, "request detection");
    });
    subscribe("http2.client.stream.created", (message) => {
      safe(() => {
        const headers = (message as { headers?: Record<string, unknown> }).headers;
        if (!headers) return;
        const get: Getter = (name) => {
          const v = headers[name];
          return v === undefined ? undefined : String(v);
        };
        seen(classify(get, String(headers[":path"] ?? "")));
      }, "request detection");
    });
  }, "detection");
}
