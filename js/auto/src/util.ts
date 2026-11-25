/**
 * Small helpers shared by every module: logging that never throws, bounded
 * JSON-safe copies of content, hashing and Python-compatible timestamps.
 */

import { createHash } from "node:crypto";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

/**
 * This module's URL, or (when this package is bundled into a CommonJS
 * bundle, where esbuild leaves `import.meta.url` undefined) a file in the
 * working directory, so `createRequire` still works.
 */
export function moduleUrl(url: string | undefined): string {
  return typeof url === "string" && url ? url : pathToFileURL(join(process.cwd(), "noop.js")).href;
}

type Level = "debug" | "warning" | "error";
const LEVELS: Record<string, number> = { debug: 10, info: 20, warning: 30, warn: 30, error: 40, critical: 50, off: 100 };

function threshold(): number {
  const raw = (process.env["BRIEFCASE_LOG_LEVEL"] ?? "").trim().toLowerCase();
  return LEVELS[raw] ?? LEVELS["warning"]!;
}

/** Writes one `briefcase.auto: ...` line to stderr. Never throws. */
export function log(level: Level, message: string): void {
  try {
    if ((LEVELS[level] ?? 30) < threshold()) return;
    process.stderr.write(`briefcase.auto: ${message}\n`);
  } catch {
    // logging must never break the application
  }
}

export const warn = (message: string): void => log("warning", message);
export const debug = (message: string): void => log("debug", message);

/** Runs `fn`, swallowing (and debug-logging) anything it throws. */
export function safe<T>(fn: () => T, what = "callback"): T | undefined {
  try {
    return fn();
  } catch (error) {
    debug(`${what} failed: ${describeError(error)}`);
    return undefined;
  }
}

export function describeError(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`;
  return String(error);
}

export function sha256Hex(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

/** JSON text with sorted object keys (the TS analog of Python's json.dumps(sort_keys=True)). */
export function stableText(value: unknown): string {
  try {
    return JSON.stringify(value, (_key, v: unknown) => {
      if (v && typeof v === "object" && !Array.isArray(v)) {
        const sorted: Record<string, unknown> = {};
        for (const k of Object.keys(v as object).sort()) sorted[k] = (v as Record<string, unknown>)[k];
        return sorted;
      }
      return v;
    }) ?? String(value);
  } catch {
    return String(value);
  }
}

export const DEFAULT_MAX_CONTENT_CHARS = 8000;
const MAX_DEPTH = 8;
const MAX_ITEMS = 200;

/**
 * Bounded, JSON-safe copy of request or response content. Strings are cut at
 * `limit` characters, binary data is replaced by its length, functions and
 * iterators are named rather than consumed. Never throws.
 */
export function jsonable(value: unknown, limit = DEFAULT_MAX_CONTENT_CHARS, depth = 0): unknown {
  try {
    if (value === null || value === undefined) return null;
    if (typeof value === "string") return value.length <= limit ? value : value.slice(0, limit) + "...";
    if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
    if (typeof value === "boolean") return value;
    if (typeof value === "bigint") return value.toString();
    if (typeof value === "function") return "<function>";
    if (typeof value === "symbol") return "<symbol>";
    if (value instanceof Uint8Array || value instanceof ArrayBuffer) return `<${value.byteLength} bytes>`;
    if (depth >= MAX_DEPTH) return `<${typeName(value)}>`;
    if (Array.isArray(value)) return value.slice(0, MAX_ITEMS).map((v) => jsonable(v, limit, depth + 1));
    if (value instanceof Date) return value.toISOString();
    if (typeof (value as { [Symbol.asyncIterator]?: unknown })[Symbol.asyncIterator] === "function") {
      return `<${typeName(value)}>`;
    }
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as object).slice(0, MAX_ITEMS)) {
      out[key] = jsonable((value as Record<string, unknown>)[key], limit, depth + 1);
    }
    return out;
  } catch {
    return `<${typeName(value)}>`;
  }
}

export function typeName(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  if (typeof value !== "object" && typeof value !== "function") return typeof value;
  try {
    const ctor = (value as { constructor?: { name?: unknown } }).constructor;
    if (ctor && typeof ctor.name === "string" && ctor.name) return ctor.name;
  } catch {
    // fall through
  }
  return typeof value;
}

/** Follows keys along `path`; `undefined` when any hop is missing. Never throws. */
export function read(obj: unknown, ...path: string[]): unknown {
  let current: unknown = obj;
  for (const key of path) {
    if (current === null || current === undefined) return undefined;
    if (typeof current !== "object" && typeof current !== "function") return undefined;
    try {
      current = (current as Record<string, unknown>)[key];
    } catch {
      return undefined;
    }
  }
  return current;
}

export function asInt(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return Math.trunc(value);
}

export function asString(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

/** Picks the scalar request parameters named in `names`; no content can ride along. */
export function scalarParams(source: unknown, names: readonly string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!source || typeof source !== "object") return out;
  for (const name of names) {
    const value = read(source, name);
    if (typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value))) out[name] = value;
    else if (typeof value === "string" && value.length <= 200) out[name] = value;
  }
  return out;
}

/**
 * Python's `datetime.isoformat()` layout for an aware UTC datetime
 * (`2026-08-15T12:00:00.250000+00:00`), so timestamps from both languages
 * sort and parse the same way.
 */
export function isoTimestamp(epochMs: number): string {
  const whole = Math.floor(epochMs);
  const micros = Math.round((epochMs - whole) * 1000);
  const iso = new Date(whole).toISOString(); // 2026-08-15T12:00:00.250Z
  return `${iso.slice(0, 23)}${String(micros).padStart(3, "0").slice(0, 3)}+00:00`;
}

/** Bounded string accumulator for streamed text. */
export class TextAccumulator {
  private parts: string[] = [];
  private size = 0;
  constructor(private readonly limit: number) {}
  add(piece: unknown): void {
    if (typeof piece !== "string" || !piece || this.size >= this.limit) return;
    this.parts.push(piece.slice(0, this.limit - this.size));
    this.size += piece.length;
  }
  get text(): string {
    return this.parts.join("");
  }
}
