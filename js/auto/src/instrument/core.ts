/**
 * Shared machinery for provider instrumentation (the TypeScript counterpart
 * of `briefcase/integrations/providers/_base.py`):
 *
 * - `Instrumentor`: owns the patches of one provider. Every target of a
 *   module copy is checked before any is applied, so an SDK whose layout
 *   differs from the verified one is skipped whole (one warning), never
 *   half-patched. `undo()` restores an original only while our wrapper is
 *   still the installed value; a later patch by someone else is left alone.
 * - `ProviderCall`: one call in flight; `finish()` emits exactly one record
 *   and never throws.
 * - `observeAsyncIterator`: reports the items and the end of an async
 *   iterator without changing what the consumer sees.
 * - `loadCopies`: the ESM and CommonJS copies of an SDK (dual packages load
 *   them as separate module instances, with separate classes).
 */

import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { readFileSync, existsSync } from "node:fs";

import type { PendingCall } from "../pipeline.js";
import { startRecord, type DecisionRecord, type TokenUsage } from "../record.js";
import { state } from "../state.js";
import {
  DEFAULT_MAX_CONTENT_CHARS,
  asInt,
  debug,
  describeError,
  isoTimestamp,
  jsonable,
  read,
  safe,
  sha256Hex,
  stableText,
  typeName,
  warn,
} from "../util.js";

export type ContentMode = "none" | "hash" | "full";

export interface CaptureOptions {
  captureContent: ContentMode;
  maxContentChars: number;
  redact?: (text: string) => string;
  contextVersion?: string;
  /** openai: ask chat streams for a final usage chunk (`stream_options.include_usage`). */
  streamUsage: boolean;
}

export function normalizeContentMode(value: unknown): ContentMode {
  if (value === undefined || value === null || value === false) return "none";
  if (value === true) return "full";
  if (value === "none" || value === "hash" || value === "full") return value;
  throw new Error(`captureContent must be a boolean or one of "none", "hash", "full"; got ${JSON.stringify(value)}`);
}

/** Marker on every wrapper we install; holds the wrapped original. */
export const WRAPPED = Symbol.for("briefcase-ai.auto.wrapped");

export interface ProviderReport {
  name: string;
  state: "patched" | "skipped" | "not-installed" | "undone" | "not-checked";
  packageName: string;
  sdkVersion: string | null;
  verifiedVersion: string;
  patched: string[];
  skipped: { target: string; reason: string }[];
  refusedUndo: string[];
  instrumentor?: Instrumentor;
}

interface AppliedPatch {
  owner: object;
  key: PropertyKey;
  label: string;
  original: PropertyDescriptor | undefined;
  installed: PropertyDescriptor;
}

export interface MethodTarget {
  kind: "method";
  owner: unknown;
  key: string;
  label: string;
  /** Allow a method the owner inherits: our wrapper becomes an own property that undo deletes. */
  inherited?: boolean;
  wrap: (original: (...args: unknown[]) => unknown) => (...args: unknown[]) => unknown;
}

/**
 * An accessor installed on a prototype for a property the SDK assigns per
 * instance in its constructor (`this.generateContent = async (...) => ...`).
 * The setter stores `wrap(value)` as the instance's own data property.
 */
export interface InstanceAssignTarget {
  kind: "instance-assign";
  owner: unknown;
  key: string;
  label: string;
  wrap: (original: (...args: unknown[]) => unknown) => (...args: unknown[]) => unknown;
}

export type PatchTarget = MethodTarget | InstanceAssignTarget;

function markWrapper(wrapper: (...args: unknown[]) => unknown, original: (...args: unknown[]) => unknown): void {
  Object.defineProperty(wrapper, WRAPPED, { value: original });
  safe(() => Object.defineProperty(wrapper, "name", { value: original.name, configurable: true }));
  safe(() => Object.defineProperty(wrapper, "length", { value: original.length, configurable: true }));
}

export class Instrumentor {
  active = true;
  private readonly applied: AppliedPatch[] = [];
  readonly report: ProviderReport;

  constructor(
    readonly provider: string,
    readonly options: CaptureOptions,
    packageName: string,
    verifiedVersion: string,
  ) {
    this.report = {
      name: provider,
      state: "skipped",
      packageName,
      sdkVersion: null,
      verifiedVersion,
      patched: [],
      skipped: [],
      refusedUndo: [],
      instrumentor: this,
    };
  }

  get capturesContent(): boolean {
    return this.options.captureContent !== "none";
  }

  /** Checks one target; returns why it cannot be patched, or null. */
  private problem(target: PatchTarget): string | null {
    const owner = target.owner;
    if (!owner || (typeof owner !== "object" && typeof owner !== "function")) return "owner not found";
    const desc = Object.getOwnPropertyDescriptor(owner, target.key);
    if (target.kind === "method") {
      const fn = desc ? desc.value : target.inherited ? (owner as Record<string, unknown>)[target.key] : undefined;
      if (!desc && !target.inherited) return `${target.key} not found`;
      if (typeof fn !== "function") return `${target.key} is not a method`;
      if ((fn as unknown as Record<symbol, unknown>)[WRAPPED]) return "already instrumented";
      if (desc && !desc.configurable && !desc.writable) return `${target.key} is read-only`;
      return null;
    }
    if (desc) {
      if (desc.get && (desc.get as unknown as Record<symbol, unknown>)[WRAPPED]) return "already instrumented";
      return `${target.key} is already defined on the prototype`;
    }
    return null;
  }

  /**
   * Applies every target, or none: if any target is missing or reshaped the
   * whole set is skipped with one warning. Returns true when applied.
   */
  applyAll(copy: string, targets: PatchTarget[], verify?: () => string | null): boolean {
    const problems = targets
      .map((t) => [t, this.problem(t)] as const)
      .filter((entry): entry is readonly [PatchTarget, string] => entry[1] !== null);
    if (problems.length > 0) {
      const already = problems.every(([, p]) => p === "already instrumented");
      const reason = problems.map(([t, p]) => `${t.label}: ${p}`).join("; ");
      for (const [t, p] of problems) this.report.skipped.push({ target: `${copy} ${t.label}`, reason: p });
      if (!already) {
        warn(
          `${this.provider} (${copy}): the installed SDK does not have the layout this version was verified ` +
            `against (${this.report.packageName} ${this.report.verifiedVersion}); nothing in this copy was patched. ${reason}`,
        );
      } else {
        debug(`${this.provider} (${copy}): already instrumented; not wrapping again`);
      }
      return false;
    }
    const start = this.applied.length;
    try {
      for (const target of targets) this.applyOne(target, copy);
      const failed = verify ? verify() : null;
      if (failed) throw new Error(failed);
    } catch (error) {
      this.rollback(start);
      const reason = describeError(error);
      this.report.skipped.push({ target: `${copy} ${targets.map((t) => t.label).join(", ")}`, reason });
      warn(`${this.provider} (${copy}): could not be instrumented (${reason}); nothing in this copy was patched.`);
      return false;
    }
    for (const target of targets) this.report.patched.push(`${copy} ${target.label}`);
    return true;
  }

  private applyOne(target: PatchTarget, copy: string): void {
    const owner = target.owner as object;
    const original = Object.getOwnPropertyDescriptor(owner, target.key);
    let installed: PropertyDescriptor;
    if (target.kind === "method") {
      const fn = (original ? original.value : (owner as Record<string, unknown>)[target.key]) as (...args: unknown[]) => unknown;
      const wrapper = target.wrap(fn);
      markWrapper(wrapper, fn);
      installed = original
        ? { ...original, value: wrapper }
        : { value: wrapper, writable: true, configurable: true, enumerable: false };
    } else {
      const wrap = target.wrap;
      const inst = this;
      const key = target.key;
      const get = function (this: object): undefined {
        return undefined;
      };
      const set = function (this: object, value: unknown): void {
        let stored = value;
        if (typeof value === "function" && inst.active) {
          const original = value as (...args: unknown[]) => unknown;
          const wrapper = wrap(original);
          markWrapper(wrapper, original);
          stored = wrapper;
        }
        Object.defineProperty(this, key, { value: stored, writable: true, enumerable: true, configurable: true });
      };
      Object.defineProperty(get, WRAPPED, { value: true });
      installed = { get, set, enumerable: false, configurable: true };
    }
    Object.defineProperty(owner, target.key, installed);
    this.applied.push({ owner, key: target.key, label: `${copy} ${target.label}`, original, installed });
  }

  private rollback(start: number): void {
    while (this.applied.length > start) {
      const patch = this.applied.pop()!;
      safe(() => {
        if (patch.original) Object.defineProperty(patch.owner, patch.key, patch.original);
        else delete (patch.owner as Record<PropertyKey, unknown>)[patch.key];
      }, "rollback");
    }
  }

  /**
   * Restores each original whose slot still holds our wrapper. A slot that
   * now holds something else (another tool patched it after us) is left as
   * it is; our wrapper inside it is switched to pass-through.
   */
  undo(): { restored: string[]; refused: string[] } {
    this.active = false;
    const restored: string[] = [];
    const refused: string[] = [];
    while (this.applied.length > 0) {
      const patch = this.applied.pop()!;
      const current = Object.getOwnPropertyDescriptor(patch.owner, patch.key);
      const ours =
        current !== undefined &&
        (current.value !== undefined ? current.value === patch.installed.value : current.get === patch.installed.get);
      if (!ours) {
        refused.push(patch.label);
        warn(
          `undo: ${patch.label} was replaced by someone else after auto(); leaving their patch in place ` +
            "(the briefcase wrapper underneath now passes calls straight through).",
        );
        continue;
      }
      try {
        if (patch.original) Object.defineProperty(patch.owner, patch.key, patch.original);
        else delete (patch.owner as Record<PropertyKey, unknown>)[patch.key];
        restored.push(patch.label);
      } catch (error) {
        refused.push(patch.label);
        warn(`undo: could not restore ${patch.label} (${describeError(error)})`);
      }
    }
    this.report.refusedUndo.push(...refused);
    this.report.patched = [];
    this.report.state = "undone";
    return { restored, refused };
  }

  /** Starts a record, or returns null when nothing is exporting (the call then runs untouched). */
  begin(
    decisionType: string,
    functionName: string,
    request: { model?: unknown; params?: Record<string, unknown>; inputs?: () => unknown; stream?: boolean },
  ): ProviderCall | null {
    try {
      if (!this.active) return null;
      const pipeline = state().pipeline;
      if (!pipeline || pipeline.closed) return null;
      const { record, startedEpochMs, t0 } = startRecord(decisionType, functionName, this.options.contextVersion);
      if (request.inputs && this.capturesContent) {
        try {
          record.inputs = asObject(this.content(jsonable(request.inputs(), this.options.maxContentChars)));
        } catch (error) {
          debug(`inputs could not be captured: ${describeError(error)}`);
        }
      }
      const model = typeof request.model === "string" && request.model ? request.model : null;
      return new ProviderCall(this, record, startedEpochMs, t0, model, request.params ?? {}, request.stream ?? false);
    } catch (error) {
      debug(`capture could not start: ${describeError(error)}`);
      return null;
    }
  }

  /** Applies the content mode to an inputs / outputs value (Python's CaptureOptions.content). */
  content(value: unknown): unknown {
    const mode = this.options.captureContent;
    if (mode === "none") return {};
    if (mode === "hash") {
      if (value === null || value === undefined || value === "" || isEmpty(value)) return {};
      const text = stableText(value);
      return { sha256: sha256Hex(text), chars: text.length };
    }
    const redact = this.options.redact;
    return redact ? redactDeep(value, redact) : value;
  }

  errorText(error: unknown): string {
    const cls = errorClass(error);
    const mode = this.options.captureContent;
    if (mode === "none") return cls;
    const message = errorMessage(error);
    if (mode === "hash") return `${cls}:sha256:${sha256Hex(message)}`;
    const text = message || cls;
    const redacted = this.options.redact ? redactString(text, this.options.redact) : text;
    return redacted.slice(0, this.options.maxContentChars);
  }
}

function isEmpty(value: unknown): boolean {
  if (Array.isArray(value)) return value.length === 0;
  if (value && typeof value === "object") return Object.keys(value).length === 0;
  return false;
}

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : { content: value };
}

function redactString(text: string, redact: (text: string) => string): string {
  try {
    const out = redact(text);
    return typeof out === "string" ? out : "<redaction-failed>";
  } catch {
    return "<redaction-failed>";
  }
}

function redactDeep(value: unknown, redact: (text: string) => string): unknown {
  if (typeof value === "string") return redactString(value, redact);
  if (Array.isArray(value)) return value.map((v) => redactDeep(v, redact));
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) out[k] = redactDeep(v, redact);
    return out;
  }
  return value;
}

/** Class name of a thrown value (Python's `type(error).__name__`). */
export function errorClass(error: unknown): string {
  const cls = typeName(error);
  const name = read(error, "name");
  if ((cls === "Error" || cls === "DOMException" || cls === "object") && typeof name === "string" && name) return name;
  return cls;
}

function errorMessage(error: unknown): string {
  const message = read(error, "message");
  return typeof message === "string" ? message : String(error);
}

export function isAbortError(error: unknown): boolean {
  const cls = errorClass(error);
  return cls === "AbortError" || /Abort/.test(cls) || read(error, "name") === "AbortError";
}

export interface CallInfo {
  model?: unknown;
  response_id?: unknown;
  request_id?: unknown;
  finish_reason?: unknown;
  usage?: TokenUsage | null;
  content?: unknown;
  extra?: Record<string, unknown>;
  cancelled?: boolean;
}

export interface StreamOutcome {
  chunk_count: number;
  stream_completed: boolean;
}

export class ProviderCall implements PendingCall {
  done = false;
  private firstChunkMs: number | null = null;
  /** What the record says if the call is abandoned (process exit) before it finishes. */
  fallback: CallInfo = {};
  chunks = 0;

  constructor(
    private readonly inst: Instrumentor,
    private readonly record: DecisionRecord,
    private readonly startedEpochMs: number,
    private readonly t0: number,
    private readonly model: string | null,
    private readonly params: Record<string, unknown>,
    private stream: boolean,
  ) {
    state().pending.add(this);
  }

  markStream(): void {
    this.stream = true;
  }

  firstChunk(): void {
    if (this.firstChunkMs === null) this.firstChunkMs = performance.now() - this.t0;
  }

  /** Emits the record now, as an unfinished call (stream never read to the end). */
  abandon(): void {
    this.finish(this.fallback, this.stream ? { stream: { chunk_count: this.chunks, stream_completed: false } } : {});
  }

  /** Completes and exports the record exactly once. Never throws. */
  finish(info: CallInfo = {}, outcome: { error?: unknown; stream?: StreamOutcome } = {}): void {
    if (this.done) return;
    this.done = true;
    state().pending.delete(this);
    try {
      this.build(info, outcome);
      state().pipeline?.enqueue(this.record);
    } catch (error) {
      debug(`record could not be completed: ${describeError(error)}`);
    }
  }

  private build(info: CallInfo, outcome: { error?: unknown; stream?: StreamOutcome }): void {
    const inst = this.inst;
    const record = this.record;
    const elapsed = performance.now() - this.t0;
    record.ended_at = isoTimestamp(this.startedEpochMs + elapsed);
    record.execution_time_ms = elapsed;

    if ("error" in outcome) {
      const error = outcome.error;
      record.status = isAbortError(error) ? "cancelled" : "error";
      record.error = inst.errorText(error);
      record.error_type = errorClass(error);
      const status =
        asInt(read(error, "status")) ??
        asInt(read(error, "$metadata", "httpStatusCode")) ??
        asInt(read(error, "$response", "statusCode"));
      if (status !== null) record.http_status = status;
      if (info.request_id === undefined || info.request_id === null) {
        const rid = read(error, "requestID") ?? read(error, "request_id") ?? read(error, "$metadata", "requestId");
        if (typeof rid === "string" && rid) info = { ...info, request_id: rid };
      }
    } else {
      record.status = info.cancelled ? "cancelled" : "ok";
    }

    let outputs: Record<string, unknown> = {};
    if (info.content !== undefined && inst.capturesContent) {
      outputs = asObject(inst.content(jsonable(info.content, inst.options.maxContentChars)));
    }
    if (outcome.stream) Object.assign(outputs, outcome.stream);
    record.outputs = outputs;

    const model = (typeof info.model === "string" && info.model) || this.model;
    const parameters: Record<string, unknown> = { provider: inst.provider };
    if (model) parameters["model_name"] = String(model);
    if (this.model && model && this.model !== model) parameters["requested_model"] = this.model;
    Object.assign(parameters, this.params);
    record.model_parameters = parameters;

    record.token_usage = info.usage ?? null;
    for (const key of ["finish_reason", "response_id", "request_id"] as const) {
      const value = info[key];
      if (value !== undefined && value !== null && value !== "") record[key] = String(value);
    }
    record.stream = this.stream;
    if (this.firstChunkMs !== null) record.time_to_first_chunk_ms = this.firstChunkMs;
    for (const [key, value] of Object.entries(info.extra ?? {})) {
      if (value !== undefined && value !== null && !(key in record)) record[key] = value;
    }
  }
}

/** How an observed iterator ended. */
export type EndKind = "exhausted" | "returned" | "error";

/** Per-chunk bookkeeping for a streamed call; provider modules subclass it. */
export abstract class StreamState {
  constructor(readonly call: ProviderCall) {}

  abstract observe(chunk: unknown): void;
  abstract info(): CallInfo;

  onItem(chunk: unknown): void {
    this.call.chunks += 1;
    if (this.call.chunks === 1) this.call.firstChunk();
    this.observe(chunk);
    this.call.fallback = safe(() => this.info(), "stream info") ?? this.call.fallback;
  }

  /**
   * Finishes the call. `exhausted` with `aborted` set means the request was
   * aborted and the SDK ended the iteration quietly: recorded as cancelled.
   * `returned` (the consumer stopped early) is recorded as an incomplete
   * stream with status ok, as Python records a stream closed early.
   */
  onEnd(kind: EndKind, error: unknown, aborted = false): void {
    if (this.call.done) return;
    const info = safe(() => this.info(), "stream info") ?? {};
    const completed = kind === "exhausted" && !aborted;
    if (kind === "exhausted" && aborted) info.cancelled = true;
    const stream = { chunk_count: this.call.chunks, stream_completed: completed };
    if (kind === "error") this.call.finish(info, { error, stream });
    else this.call.finish(info, { stream });
  }
}

export interface IteratorHooks {
  onItem(item: unknown): void;
  onEnd(kind: EndKind, error?: unknown): void;
}

/**
 * Wraps an async iterator so each item and the end are reported once. The
 * consumer sees the same items, errors and `return()` behaviour; hook
 * failures are swallowed.
 */
export function observeAsyncIterator<T>(inner: AsyncIterator<T>, hooks: IteratorHooks): AsyncIterableIterator<T> {
  let ended = false;
  const end = (kind: EndKind, error?: unknown): void => {
    if (ended) return;
    ended = true;
    safe(() => hooks.onEnd(kind, error), "stream end hook");
  };
  const iterator: AsyncIterableIterator<T> = {
    async next(...args: [] | [unknown]): Promise<IteratorResult<T>> {
      let result: IteratorResult<T>;
      try {
        result = await inner.next(...(args as []));
      } catch (error) {
        end("error", error);
        throw error;
      }
      if (result.done) end("exhausted");
      else if (!ended) safe(() => hooks.onItem(result.value), "stream item hook");
      return result;
    },
    async return(value?: unknown): Promise<IteratorResult<T>> {
      try {
        if (typeof inner.return === "function") return await inner.return(value as T);
        return { done: true, value: value as T };
      } finally {
        end("returned");
      }
    },
    async throw(error?: unknown): Promise<IteratorResult<T>> {
      end("error", error);
      if (typeof inner.throw === "function") return inner.throw(error);
      throw error;
    },
    [Symbol.asyncIterator]() {
      return this;
    },
  };
  return iterator;
}

/**
 * Instruments an async iterable in place, keeping the caller's object:
 * an async generator (or any object that is its own iterator) gets own
 * `next` / `return` / `throw` that report to `hooks`; any other async
 * iterable gets an own `[Symbol.asyncIterator]` returning an observed
 * iterator. Returns false when `obj` is not async-iterable.
 */
export function instrumentAsyncIterable(obj: unknown, hooks: IteratorHooks): boolean {
  if (!obj || typeof obj !== "object") return false;
  const target = obj as Record<PropertyKey, unknown>;
  const iterFn = target[Symbol.asyncIterator];
  if (typeof iterFn !== "function") return false;
  const next = target["next"];
  if (typeof next === "function") {
    const ret = target["return"];
    const thr = target["throw"];
    const inner: AsyncIterator<unknown> = {
      next: (...args: [] | [unknown]) => (next as (...a: unknown[]) => Promise<IteratorResult<unknown>>).apply(obj, args),
    };
    if (typeof ret === "function") inner.return = (value?: unknown) => (ret as (v?: unknown) => Promise<IteratorResult<unknown>>).call(obj, value);
    if (typeof thr === "function") inner.throw = (error?: unknown) => (thr as (e?: unknown) => Promise<IteratorResult<unknown>>).call(obj, error);
    const observed = observeAsyncIterator(inner, hooks);
    for (const key of ["next", "return", "throw"] as const) {
      Object.defineProperty(obj, key, { value: observed[key]!.bind(observed), writable: true, configurable: true, enumerable: false });
    }
    return true;
  }
  Object.defineProperty(obj, Symbol.asyncIterator, {
    value: function (this: unknown, ...args: unknown[]) {
      return observeAsyncIterator((iterFn as (...a: unknown[]) => AsyncIterator<unknown>).apply(this, args), hooks);
    },
    writable: true,
    configurable: true,
    enumerable: false,
  });
  return true;
}

/** Frees the record of a stream object that is garbage-collected without being read. */
const streamRegistry = new FinalizationRegistry<ProviderCall>((call) => {
  safe(() => call.abandon(), "finalizer");
});

export function finishWhenCollected(target: object, call: ProviderCall): void {
  safe(() => streamRegistry.register(target, call, call), "finalizer registration");
}

export const DEFAULT_OPTIONS: CaptureOptions = {
  captureContent: "none",
  maxContentChars: DEFAULT_MAX_CONTENT_CHARS,
  streamUsage: false,
};

// -- module copies ------------------------------------------------------------

export interface ModuleCopy {
  format: "esm" | "cjs";
  path: string;
  ns: Record<string, unknown>;
}

const localRequire = createRequire(import.meta.url);

const ESM_CONDITIONS = ["node", "import", "default"];

function pickConditional(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    for (const item of value) {
      const hit = pickConditional(item);
      if (hit) return hit;
    }
    return null;
  }
  if (value && typeof value === "object") {
    for (const [key, nested] of Object.entries(value)) {
      if (ESM_CONDITIONS.includes(key)) {
        const hit = pickConditional(nested);
        if (hit) return hit;
      }
    }
  }
  return null;
}

/**
 * Resolves `specifier` the way an `import` from Node would, by reading the
 * package's `exports` map with the conditions node / import / default.
 * Used when `import.meta.resolve` is unavailable (some test runners and
 * transpilers); `cjsPath` locates the package directory.
 */
export function resolveEsmFromExports(specifier: string, cjsPath: string): string | null {
  const parts = specifier.split("/");
  const nameParts = specifier.startsWith("@") ? 2 : 1;
  const packageName = parts.slice(0, nameParts).join("/");
  const subpath = parts.length > nameParts ? `./${parts.slice(nameParts).join("/")}` : ".";
  let dir = dirname(cjsPath);
  let pkg: { name?: string; exports?: unknown } | null = null;
  for (let i = 0; i < 12; i++) {
    const candidate = join(dir, "package.json");
    if (existsSync(candidate)) {
      try {
        const parsed = JSON.parse(readFileSync(candidate, "utf8")) as { name?: string; exports?: unknown };
        if (parsed.name === packageName) {
          pkg = parsed;
          break;
        }
      } catch {
        // keep walking
      }
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  if (!pkg) return null;
  let exportsMap = pkg.exports;
  if (exportsMap === undefined) return cjsPath; // no exports: import and require both use "main"
  if (typeof exportsMap === "string" || Array.isArray(exportsMap) || !Object.keys(exportsMap as object).some((k) => k.startsWith("."))) {
    exportsMap = { ".": exportsMap };
  }
  const map = exportsMap as Record<string, unknown>;
  let target: string | null = null;
  if (subpath in map) target = pickConditional(map[subpath]);
  else {
    for (const [key, value] of Object.entries(map)) {
      const star = key.indexOf("*");
      if (star < 0) continue;
      const prefix = key.slice(0, star);
      const suffix = key.slice(star + 1);
      if (subpath.startsWith(prefix) && subpath.endsWith(suffix) && subpath.length >= key.length - 1) {
        const match = subpath.slice(prefix.length, subpath.length - suffix.length);
        const picked = pickConditional(value);
        if (picked) {
          target = picked.split("*").join(match);
          break;
        }
      }
    }
  }
  if (!target) return null;
  const file = join(dir, target);
  return existsSync(file) ? file : null;
}

function resolveEsm(specifier: string, cjsPath: string | null): string | null {
  try {
    const meta = import.meta as ImportMeta & { resolve?: (s: string) => string };
    if (typeof meta.resolve === "function") {
      const url = meta.resolve(specifier);
      return url.startsWith("file:") ? fileURLToPath(url) : null;
    }
  } catch {
    // fall through
  }
  return cjsPath ? resolveEsmFromExports(specifier, cjsPath) : null;
}

export function isNotFound(error: unknown): boolean {
  const code = read(error, "code");
  return code === "MODULE_NOT_FOUND" || code === "ERR_MODULE_NOT_FOUND" || code === "ERR_PACKAGE_PATH_NOT_EXPORTED";
}

/**
 * Loads every copy of `specifier` an application could get: the file
 * `import` resolves to and the file `require` resolves to. For dual
 * packages these differ and are separate module instances. The ESM copy is
 * loaded with `require()` (require(esm), on by default since Node 20.19 and
 * 22.12), which returns the same instance a later `import` gets.
 * Returns null when the package is not installed.
 */
export function loadCopies(specifier: string): { copies: ModuleCopy[]; problems: string[] } | null {
  let cjsPath: string | null = null;
  try {
    cjsPath = localRequire.resolve(specifier);
  } catch (error) {
    if (!isNotFound(error)) debug(`resolve ${specifier} (require): ${describeError(error)}`);
  }
  const esmPath = resolveEsm(specifier, cjsPath);
  if (!cjsPath && !esmPath) return null;
  const copies: ModuleCopy[] = [];
  const problems: string[] = [];
  const load = (format: "esm" | "cjs", path: string): void => {
    try {
      copies.push({ format, path, ns: localRequire(path) as Record<string, unknown> });
    } catch (error) {
      problems.push(`${format} copy could not be loaded (${describeError(error)})`);
    }
  };
  if (esmPath && esmPath !== cjsPath) load("esm", esmPath);
  if (cjsPath) load("cjs", cjsPath);
  return { copies, problems };
}

/** The `version` of the installed package that contains `file`. */
export function packageVersion(file: string, packageName: string): string | null {
  let dir = dirname(file);
  for (let i = 0; i < 8; i++) {
    const candidate = join(dir, "package.json");
    if (existsSync(candidate)) {
      try {
        const pkg = JSON.parse(readFileSync(candidate, "utf8")) as { name?: string; version?: string };
        if (pkg.name === packageName) return pkg.version ?? null;
      } catch {
        // keep walking
      }
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}
