/**
 * The decision record layout. Field names are the ones Python's `@capture`
 * and provider instrumentation write (see `tests/fixtures/decision_record.json`
 * and `briefcase/integrations/providers/_base.py`), so records from both
 * languages look the same to every consumer.
 */

import { randomUUID } from "node:crypto";

import { correlationFields } from "./context.js";
import { isoTimestamp } from "./util.js";

export const SCHEMA_VERSION = "1.1";

export interface TokenUsage {
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  [counter: string]: number | null;
}

export interface DecisionRecord {
  schema_version: string;
  decision_id: string;
  decision_type: string;
  function_name: string;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  started_at: string;
  ended_at?: string;
  execution_time_ms?: number;
  status?: "ok" | "error" | "cancelled";
  error?: string;
  error_type?: string;
  http_status?: number;
  model_parameters?: Record<string, unknown>;
  token_usage?: TokenUsage | null;
  finish_reason?: string;
  response_id?: string;
  request_id?: string;
  stream?: boolean;
  time_to_first_chunk_ms?: number;
  context_version?: string;
  workflow_id?: string;
  trace_id?: string;
  parent_span_id?: string;
  trace_flags?: number;
  [field: string]: unknown;
}

/** Starts a record: ids, timestamps and correlation from the caller's context. */
export function startRecord(
  decisionType: string,
  functionName: string,
  contextVersion: string | undefined,
): { record: DecisionRecord; startedEpochMs: number; t0: number } {
  const t0 = performance.now();
  const startedEpochMs = performance.timeOrigin + t0;
  const record: DecisionRecord = {
    schema_version: SCHEMA_VERSION,
    decision_id: randomUUID(),
    decision_type: decisionType,
    function_name: functionName,
    inputs: {},
    outputs: {},
    started_at: isoTimestamp(startedEpochMs),
  };
  if (contextVersion !== undefined) record.context_version = contextVersion;
  Object.assign(record, correlationFields());
  return { record, startedEpochMs, t0 };
}

/**
 * Normalizes provider token counters onto prompt/completion/total, keeping
 * provider-specific counters when they are numbers. Returns `null` when the
 * provider reported nothing; an individual counter that was not reported is
 * `null`, never 0.
 */
export function tokenUsage(
  prompt: unknown,
  completion: unknown,
  total?: unknown,
  extras: Record<string, unknown> = {},
): TokenUsage | null {
  const p = toInt(prompt);
  const c = toInt(completion);
  const t = toInt(total);
  if (p === null && c === null && t === null) return null;
  const usage: TokenUsage = {
    prompt_tokens: p,
    completion_tokens: c,
    total_tokens: t ?? (p !== null && c !== null ? p + c : null),
  };
  for (const [key, value] of Object.entries(extras)) {
    const n = toInt(value);
    if (n !== null) usage[key] = n;
  }
  return usage;
}

function toInt(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : null;
}
