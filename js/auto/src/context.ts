/**
 * Correlation context carried into every record:
 *
 * - `withWorkflow(name, fn)` runs `fn` inside an AsyncLocalStorage scope, so
 *   every provider call made from it (across awaits, timers and streams)
 *   carries the same `workflow_id`, the field Python's
 *   `briefcase.correlation` workflow writes.
 * - When `@opentelemetry/api` is installed and a span is active, the record
 *   carries its W3C trace context as `trace_id`, `parent_span_id` and
 *   `trace_flags` (the fields Python's `@capture` writes), so the OTLP
 *   exporter can parent the decision span under it.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes } from "node:crypto";
import { createRequire } from "node:module";
import { join } from "node:path";

import { debug, moduleUrl } from "./util.js";

export interface WorkflowContext {
  readonly workflowId: string;
  readonly name: string;
}

const storage = new AsyncLocalStorage<WorkflowContext>();

/** Runs `fn` with a workflow context; returns whatever `fn` returns (a promise stays a promise). */
export function withWorkflow<T>(name: string, fn: () => T, options: { workflowId?: string } = {}): T {
  const context: WorkflowContext = {
    workflowId: options.workflowId ?? `wf_${randomBytes(6).toString("hex")}`,
    name,
  };
  return storage.run(context, fn);
}

/** The workflow active in the current async context, if any. */
export function currentWorkflow(): WorkflowContext | undefined {
  return storage.getStore();
}

interface OtelApi {
  context: { active(): unknown };
  trace: {
    getSpan(ctx: unknown): { spanContext(): { traceId: string; spanId: string; traceFlags: number } } | undefined;
    isSpanContextValid(sc: unknown): boolean;
  };
}

let otel: OtelApi | null | undefined;

/**
 * Loads `@opentelemetry/api` once, at runtime, if it is installed (resolved
 * from the application's directory first, then from this package). The API
 * keeps its global context manager on `globalThis`, so the CommonJS build
 * loaded here sees spans started through the ESM build and vice versa.
 */
export function loadOtel(): OtelApi | null {
  if (otel !== undefined) return otel;
  otel = null;
  for (const base of [join(process.cwd(), "noop.js"), moduleUrl(import.meta.url)]) {
    try {
      const mod = createRequire(base)("@opentelemetry/api") as Partial<OtelApi>;
      if (mod && mod.trace && mod.context) {
        otel = mod as OtelApi;
        break;
      }
    } catch {
      // not installed there
    }
  }
  return otel;
}

/** Correlation fields for a record started now. Never throws. */
export function correlationFields(): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  try {
    const workflow = storage.getStore();
    if (workflow) out["workflow_id"] = workflow.workflowId;
    const api = loadOtel();
    if (api) {
      const span = api.trace.getSpan(api.context.active());
      const sc = span?.spanContext();
      if (sc && api.trace.isSpanContextValid(sc)) {
        out["trace_id"] = sc.traceId;
        out["parent_span_id"] = sc.spanId;
        out["trace_flags"] = sc.traceFlags;
      }
    }
  } catch (error) {
    debug(`correlation lookup failed: ${String(error)}`);
  }
  return out;
}

/** Test hook: forget the cached OpenTelemetry lookup. */
export function resetOtelForTests(api?: OtelApi | null): void {
  otel = api === undefined ? undefined : api;
}
