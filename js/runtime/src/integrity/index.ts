import { createHash } from "node:crypto";

export type JsonPrimitive = null | boolean | number | string;
export type JsonValue = JsonPrimitive | JsonValue[] | { readonly [key: string]: JsonValue };
export type MaybePromise<T> = T | Promise<T>;

export const HASH_SPEC_VERSION = 1 as const;
export const GENESIS_PRIOR_HASH = "00".repeat(32);

function encodeJson(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("canonical JSON numbers must be finite");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(encodeJson).join(",")}]`;
  if (typeof value === "object") {
    const object = value as Record<string, unknown>;
    const entries = Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${encodeJson(object[key])}`);
    return `{${entries.join(",")}}`;
  }
  throw new TypeError(`unsupported canonical JSON value: ${typeof value}`);
}

export function canonicalJson(value: unknown): Uint8Array {
  return Buffer.from(encodeJson(value), "utf8");
}

export function sha256Hex(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

export function computePayloadHash(payload: unknown): string {
  return sha256Hex(canonicalJson(payload));
}

function rfc3339Nanos(value: Date): string {
  if (Number.isNaN(value.getTime())) throw new TypeError("hash-chain timestamps must be valid dates");
  return value.toISOString().replace(/\.(\d{3})Z$/, ".$1000000Z");
}

export interface EntryHashInput {
  rowId: string;
  table: string;
  entityId: string | null;
  observedAt: Date;
  recordedAt: Date;
  payloadHash: string;
  supersedes: string | null;
  priorHash: string;
}

function entryInput(input: EntryHashInput): Record<string, unknown> {
  return {
    v: HASH_SPEC_VERSION,
    id: input.rowId,
    table: input.table,
    entity: input.entityId,
    observed_at: rfc3339Nanos(input.observedAt),
    recorded_at: rfc3339Nanos(input.recordedAt),
    payload_hash: input.payloadHash,
    supersedes: input.supersedes,
    prior_hash: input.priorHash,
  };
}

export function computeEntryHash(input: EntryHashInput): string {
  return sha256Hex(canonicalJson(entryInput(input)));
}

export interface HashChainEntry extends EntryHashInput {
  hash: string;
  signature: string | null;
}

export class ChainConflictError extends Error {
  constructor(message = "hash-chain tail changed") {
    super(message);
    this.name = "ChainConflictError";
  }
}

export interface HashChainStore {
  lastEntryHash(table: string, entityId?: string | null): MaybePromise<string>;
  append(entry: HashChainEntry): MaybePromise<void>;
}

export interface AppendRowInput {
  table: string;
  rowId: string;
  entityId?: string | null;
  observedAt: Date;
  recordedAt: Date;
  payload: unknown;
  supersedes?: string | null;
  maxAttempts?: number;
}

export interface HashChainAppenderOptions {
  sleep?: (milliseconds: number) => Promise<void>;
  random?: () => number;
}

const defaultSleep = (milliseconds: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, milliseconds));

export class HashChainAppender {
  private readonly sleep: (milliseconds: number) => Promise<void>;
  private readonly random: () => number;

  constructor(
    private readonly store: HashChainStore,
    options: HashChainAppenderOptions = {},
  ) {
    this.sleep = options.sleep ?? defaultSleep;
    this.random = options.random ?? Math.random;
  }

  async appendRow(input: AppendRowInput): Promise<HashChainEntry> {
    const maxAttempts = input.maxAttempts ?? 32;
    if (!Number.isInteger(maxAttempts) || maxAttempts < 1) {
      throw new RangeError("maxAttempts must be a positive integer");
    }
    const payloadHash = computePayloadHash(input.payload);
    let lastConflict: ChainConflictError | undefined;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      const priorHash = await this.store.lastEntryHash(input.table, input.entityId ?? null);
      const entry: HashChainEntry = {
        rowId: input.rowId,
        table: input.table,
        entityId: input.entityId ?? null,
        observedAt: input.observedAt,
        recordedAt: input.recordedAt,
        payloadHash,
        supersedes: input.supersedes ?? null,
        priorHash,
        hash: "",
        signature: null,
      };
      entry.hash = computeEntryHash(entry);
      try {
        await this.store.append(entry);
        return entry;
      } catch (error) {
        if (!(error instanceof ChainConflictError)) throw error;
        lastConflict = error;
        if (attempt + 1 < maxAttempts) {
          await this.sleep(this.random() * Math.min((attempt + 1) * 2, 20));
        }
      }
    }
    throw lastConflict ?? new ChainConflictError();
  }
}

export type ChainVerification =
  | { ok: true }
  | { ok: false; rowId: string; reason: "prior_hash" | "hash" };

export function verifyChainSegment(
  entries: readonly HashChainEntry[],
  options: { expectedPrior?: string } = {},
): ChainVerification {
  let expectedPrior = options.expectedPrior ?? GENESIS_PRIOR_HASH;
  for (const entry of entries) {
    if (entry.priorHash !== expectedPrior) {
      return { ok: false, rowId: entry.rowId, reason: "prior_hash" };
    }
    if (computeEntryHash(entry) !== entry.hash) {
      return { ok: false, rowId: entry.rowId, reason: "hash" };
    }
    expectedPrior = entry.hash;
  }
  return { ok: true };
}
