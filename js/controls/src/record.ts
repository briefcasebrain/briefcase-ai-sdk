/**
 * Decision-record building with content-capture modes. Mirrors the semantics
 * of briefcase.decorators: "full" records serialized content (optionally
 * redacted, then truncated), "hash" records SHA-256 digests plus character
 * counts and type names, "none" records shape only. Wire fields are
 * snake_case and shared with the Python SDK via tests/fixtures/decision_record.json.
 * Field names and captureContent modes are shared with the Python SDK, but
 * digest values and char counts are serializer-specific (repr vs JSON) and
 * must never be compared across languages.
 */

import { createHash, randomUUID } from "node:crypto";

export const CAPTURE_CONTENT_MODES = ["full", "hash", "none"] as const;
export type CaptureContentMode = (typeof CAPTURE_CONTENT_MODES)[number];

export interface DecisionRecord {
  decision_id: string;
  decision_type: string;
  function_name: string;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  started_at: string;
  ended_at?: string;
  execution_time_ms?: number;
  error?: string;
  context_version?: string;
}

export function sha256Hex(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

/** Serializes a value for capture; the TS analog of Python's repr. */
function serialize(value: unknown): string {
  const json = JSON.stringify(value);
  return json === undefined ? String(value) : json;
}

/** Constructor name of a value, with primitives reported via their wrappers. */
function typeName(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  const ctor = (value as { constructor?: { name?: string } }).constructor;
  return ctor?.name ?? typeof value;
}

/** Class name of a thrown value; the error field never carries a message in non-full modes. */
function errorClassName(error: unknown): string {
  if (typeof error === "object" && error !== null) return typeName(error);
  return typeof error;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

type Redact = (text: string) => string;

function fullText(text: string, redact: Redact | undefined, limit: number): string {
  return (redact ? redact(text) : text).slice(0, limit);
}

function assertMode(mode: CaptureContentMode): void {
  if (!CAPTURE_CONTENT_MODES.includes(mode)) {
    throw new Error(
      `captureContent must be one of ${JSON.stringify(CAPTURE_CONTENT_MODES)}, got ${JSON.stringify(mode)}`,
    );
  }
}

export interface BuildDecisionRecordOptions {
  decisionType: string;
  functionName: string;
  args?: unknown[];
  kwargs?: Record<string, unknown>;
  contextVersion?: string;
  captureContent?: CaptureContentMode;
  maxInputChars?: number;
  redact?: Redact;
  clock?: () => Date;
}

/** Builds the initial decision record with captured inputs and started_at. */
export function buildDecisionRecord(options: BuildDecisionRecordOptions): DecisionRecord {
  const mode = options.captureContent ?? "full";
  assertMode(mode);
  const maxInputChars = options.maxInputChars ?? 1000;
  const clock = options.clock ?? (() => new Date());
  const { args, kwargs, redact } = options;

  const inputs: Record<string, unknown> = {};
  if (mode === "full") {
    if (args && args.length > 0) inputs["args"] = fullText(serialize(args), redact, maxInputChars);
    if (kwargs && Object.keys(kwargs).length > 0) {
      inputs["kwargs"] = fullText(serialize(kwargs), redact, maxInputChars);
    }
  } else if (mode === "hash") {
    if (args && args.length > 0) {
      const text = serialize(args);
      inputs["args_sha256"] = sha256Hex(text);
      inputs["args_chars"] = text.length;
    }
    if (kwargs && Object.keys(kwargs).length > 0) {
      const text = serialize(kwargs);
      inputs["kwargs_sha256"] = sha256Hex(text);
      inputs["kwargs_chars"] = text.length;
    }
  } else {
    if (args && args.length > 0) inputs["args_count"] = args.length;
    if (kwargs && Object.keys(kwargs).length > 0) {
      inputs["kwargs_count"] = Object.keys(kwargs).length;
    }
  }

  const record: DecisionRecord = {
    decision_id: randomUUID(),
    decision_type: options.decisionType,
    function_name: options.functionName,
    inputs,
    outputs: {},
    started_at: clock().toISOString(),
  };
  if (options.contextVersion !== undefined) record.context_version = options.contextVersion;
  return record;
}

export interface FinalizeDecisionRecordOptions {
  result?: unknown;
  error?: unknown;
  maxOutputChars?: number;
  captureContent?: CaptureContentMode;
  redact?: Redact;
  clock?: () => Date;
}

/**
 * Mutates the record in place with timing and output or error. The presence
 * of the `error` key on options selects the error path.
 */
export function finalizeDecisionRecord(
  record: DecisionRecord,
  startedAt: Date,
  options: FinalizeDecisionRecordOptions = {},
): void {
  const mode = options.captureContent ?? "full";
  assertMode(mode);
  const maxOutputChars = options.maxOutputChars ?? 1000;
  const clock = options.clock ?? (() => new Date());
  const { redact } = options;

  const endedAt = clock();
  record.ended_at = endedAt.toISOString();
  record.execution_time_ms = endedAt.getTime() - startedAt.getTime();

  if ("error" in options) {
    const error = options.error;
    if (mode === "full") {
      record.error = fullText(errorMessage(error), redact, maxOutputChars);
    } else if (mode === "hash") {
      record.error = `${errorClassName(error)}:sha256:${sha256Hex(errorMessage(error))}`;
    } else {
      record.error = errorClassName(error);
    }
    return;
  }

  const result = options.result;
  if (mode === "full") {
    record.outputs = { result: fullText(serialize(result), redact, maxOutputChars) };
  } else if (mode === "hash") {
    const text = serialize(result);
    record.outputs = {
      result_sha256: sha256Hex(text),
      result_chars: text.length,
      result_type: typeName(result),
    };
  } else {
    record.outputs = { result_type: typeName(result) };
  }
}
