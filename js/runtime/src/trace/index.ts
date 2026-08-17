import { randomUUID } from "node:crypto";

import { computePayloadHash } from "../integrity/index.js";

export type TraceOutcome = "success" | "error" | "cancelled" | "dropped";
export type TraceWriteKind = "invocation_start" | "invocation_finish" | "step_start" | "step_finish";

export interface InvocationRecord {
  id: string;
  tenantId: string;
  actorType: string;
  actorId: string;
  intent: string;
  context: Record<string, unknown>;
  startedAt: Date;
  integrityHash: string;
  finishedAt: Date | null;
  outcome: TraceOutcome | null;
  finalizeHash: string | null;
}

export interface StepRecord {
  id: string;
  invocationId: string;
  tenantId: string;
  sequence: number;
  stepType: string;
  name: string;
  input: Record<string, unknown>;
  startedAt: Date;
  integrityHash: string;
  output: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  finishedAt: Date | null;
  finalizeHash: string | null;
}

export interface TraceStore {
  writeInvocationStart(record: InvocationRecord): Promise<void>;
  writeInvocationFinish(args: { id: string; outcome: TraceOutcome; finishedAt: Date; finalizeHash: string }): Promise<void>;
  writeStepStart(record: StepRecord): Promise<void>;
  writeStepFinish(args: { id: string; output: Record<string, unknown> | null; error: Record<string, unknown> | null; finishedAt: Date; finalizeHash: string }): Promise<void>;
}

export type TraceEvent =
  | { type: "orphan_invocation_finish"; invocationId: string }
  | { type: "orphan_step_start"; invocationId: string; stepId: string }
  | { type: "orphan_step_finish"; stepId: string }
  | { type: "write_dropped"; kind: TraceWriteKind; id: string; error: unknown }
  | { type: "state_swept"; kind: "invocation" | "step"; id: string };

export interface TraceRecorderOptions {
  store: TraceStore;
  idGenerator?: () => string;
  now?: () => Date;
  sleep?: (milliseconds: number) => Promise<void>;
  isTransient?: (error: unknown) => boolean;
  maxAttempts?: number;
  staleAfterMs?: number;
  sweepIntervalMs?: number;
  onEvent?: (event: TraceEvent) => void;
}

export interface TraceRecorder {
  invocation: {
    start(args: { tenantId: string; actorType: string; actorId: string; intent: string; context: Record<string, unknown> }): string;
    finish(args: { invocationId: string; outcome: Exclude<TraceOutcome, "dropped"> }): void;
  };
  step: {
    start(args: { invocationId: string; stepType: string; name: string; input: Record<string, unknown> }): string;
    finish(args: { stepId: string; output?: Record<string, unknown>; error?: Record<string, unknown> }): void;
  };
  dropCounts(): Readonly<Record<"invocationStart" | "invocationFinish" | "stepStart" | "stepFinish", number>>;
  flush(): Promise<void>;
  sweep(): Promise<void>;
  close(): Promise<void>;
}

function invocationStartInput(record: InvocationRecord): Record<string, unknown> {
  return {
    id: record.id,
    tenant_id: record.tenantId,
    actor_type: record.actorType,
    actor_id: record.actorId,
    intent: record.intent,
    context: record.context,
    started_at: record.startedAt.toISOString(),
  };
}

function invocationFinalizeHash(record: Pick<InvocationRecord, "integrityHash" | "outcome" | "finishedAt">): string | null {
  if (record.outcome === null || record.finishedAt === null) return null;
  return computePayloadHash({
    integrity_hash: record.integrityHash,
    outcome: record.outcome,
    finished_at: record.finishedAt.toISOString(),
  });
}

function stepStartInput(record: StepRecord): Record<string, unknown> {
  return {
    id: record.id,
    invocation_id: record.invocationId,
    tenant_id: record.tenantId,
    sequence: record.sequence,
    step_type: record.stepType,
    name: record.name,
    input: record.input,
    started_at: record.startedAt.toISOString(),
  };
}

function stepFinalizeHash(record: Pick<StepRecord, "integrityHash" | "output" | "error" | "finishedAt">): string | null {
  if (record.finishedAt === null) return null;
  return computePayloadHash({
    integrity_hash: record.integrityHash,
    output: record.output,
    error: record.error,
    finished_at: record.finishedAt.toISOString(),
  });
}

export function verifyInvocationRecord(record: InvocationRecord): boolean {
  if (computePayloadHash(invocationStartInput(record)) !== record.integrityHash) return false;
  if (record.outcome === null && record.finishedAt === null && record.finalizeHash === null) return true;
  if (record.outcome === null || record.finishedAt === null || record.finalizeHash === null) return false;
  return invocationFinalizeHash(record) === record.finalizeHash;
}

export function verifyStepRecord(record: StepRecord): boolean {
  if (computePayloadHash(stepStartInput(record)) !== record.integrityHash) return false;
  if (
    record.output === null &&
    record.error === null &&
    record.finishedAt === null &&
    record.finalizeHash === null
  ) return true;
  if (record.finishedAt === null || record.finalizeHash === null) return false;
  return stepFinalizeHash(record) === record.finalizeHash;
}

const defaultTransient = (error: unknown): boolean =>
  /deadlock detected|could not serialize|connection terminated|ECONNRESET|ETIMEDOUT/i.test(
    error instanceof Error ? error.message : String(error),
  );
const defaultSleep = (milliseconds: number) => new Promise<void>((resolve) => setTimeout(resolve, milliseconds));

interface InvocationState {
  tenantId: string;
  createdAtMs: number;
  nextSequence: number;
  record: InvocationRecord;
  tail: Promise<unknown>;
}

interface StepState {
  createdAtMs: number;
  record: StepRecord;
  tail: Promise<unknown>;
}

export function createTraceRecorder(options: TraceRecorderOptions): TraceRecorder {
  const now = options.now ?? (() => new Date());
  const idGenerator = options.idGenerator ?? randomUUID;
  const sleep = options.sleep ?? defaultSleep;
  const isTransient = options.isTransient ?? defaultTransient;
  const maxAttempts = options.maxAttempts ?? 3;
  const staleAfterMs = options.staleAfterMs ?? 60 * 60_000;
  const onEvent = options.onEvent ?? (() => undefined);
  const invocations = new Map<string, InvocationState>();
  const steps = new Map<string, StepState>();
  const drops = { invocationStart: 0, invocationFinish: 0, stepStart: 0, stepFinish: 0 };
  let closed = false;

  const dropKey: Record<TraceWriteKind, keyof typeof drops> = {
    invocation_start: "invocationStart",
    invocation_finish: "invocationFinish",
    step_start: "stepStart",
    step_finish: "stepFinish",
  };

  const write = async (kind: TraceWriteKind, id: string, operation: () => Promise<void>): Promise<boolean> => {
    let lastError: unknown;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      try {
        await operation();
        return true;
      } catch (error) {
        lastError = error;
        if (attempt === maxAttempts || !isTransient(error)) break;
        await sleep(25 * 2 ** (attempt - 1));
      }
    }
    drops[dropKey[kind]] += 1;
    onEvent({ type: "write_dropped", kind, id, error: lastError });
    return false;
  };

  const assertOpen = () => {
    if (closed) throw new Error("trace recorder is closed");
  };

  const startInvocation: TraceRecorder["invocation"]["start"] = (args) => {
    assertOpen();
    const id = idGenerator();
    const startedAt = now();
    const context = structuredClone(args.context);
    const record: InvocationRecord = {
      id,
      tenantId: args.tenantId,
      actorType: args.actorType,
      actorId: args.actorId,
      intent: args.intent,
      context,
      startedAt,
      integrityHash: "",
      finishedAt: null,
      outcome: null,
      finalizeHash: null,
    };
    record.integrityHash = computePayloadHash(invocationStartInput(record));
    const tail = write("invocation_start", id, () => options.store.writeInvocationStart(record));
    invocations.set(id, {
      tenantId: args.tenantId,
      createdAtMs: startedAt.getTime(),
      nextSequence: 0,
      record,
      tail,
    });
    return id;
  };

  const finishInvocation: TraceRecorder["invocation"]["finish"] = ({ invocationId, outcome }) => {
    assertOpen();
    const state = invocations.get(invocationId);
    if (!state) {
      onEvent({ type: "orphan_invocation_finish", invocationId });
      return;
    }
    const finishedAt = now();
    state.record = { ...state.record, outcome, finishedAt };
    const finalizeHash = invocationFinalizeHash(state.record)!;
    state.record.finalizeHash = finalizeHash;
    state.tail = state.tail
      .then(() => write("invocation_finish", invocationId, () =>
        options.store.writeInvocationFinish({ id: invocationId, outcome, finishedAt, finalizeHash })))
      .finally(() => invocations.delete(invocationId));
  };

  const startStep: TraceRecorder["step"]["start"] = (args) => {
    assertOpen();
    const id = idGenerator();
    const invocation = invocations.get(args.invocationId);
    if (!invocation) {
      onEvent({ type: "orphan_step_start", invocationId: args.invocationId, stepId: id });
      return id;
    }
    const startedAt = now();
    const input = structuredClone(args.input);
    const record: StepRecord = {
      id,
      invocationId: args.invocationId,
      tenantId: invocation.tenantId,
      sequence: invocation.nextSequence++,
      stepType: args.stepType,
      name: args.name,
      input,
      startedAt,
      integrityHash: "",
      output: null,
      error: null,
      finishedAt: null,
      finalizeHash: null,
    };
    record.integrityHash = computePayloadHash(stepStartInput(record));
    const tail = invocation.tail.then(() =>
      write("step_start", id, () => options.store.writeStepStart(record)),
    );
    invocation.tail = tail;
    steps.set(id, { createdAtMs: startedAt.getTime(), record, tail });
    return id;
  };

  const finishStep: TraceRecorder["step"]["finish"] = ({ stepId, output, error }) => {
    assertOpen();
    const state = steps.get(stepId);
    if (!state) {
      onEvent({ type: "orphan_step_finish", stepId });
      return;
    }
    const finishedAt = now();
    const finalizedOutput = output === undefined ? null : structuredClone(output);
    const finalizedError = error === undefined ? null : structuredClone(error);
    state.record = {
      ...state.record,
      output: finalizedOutput,
      error: finalizedError,
      finishedAt,
    };
    const finalizeHash = stepFinalizeHash(state.record)!;
    state.record.finalizeHash = finalizeHash;
    const invocation = invocations.get(state.record.invocationId);
    const prerequisites = invocation
      ? Promise.all([state.tail, invocation.tail])
      : state.tail;
    state.tail = prerequisites
      .then(() => write("step_finish", stepId, () => options.store.writeStepFinish({
        id: stepId,
        output: finalizedOutput,
        error: finalizedError,
        finishedAt,
        finalizeHash,
      })))
      .finally(() => steps.delete(stepId));
    if (invocation) invocation.tail = state.tail;
  };

  const flush = async (): Promise<void> => {
    while (invocations.size > 0 || steps.size > 0) {
      const pending = [
        ...Array.from(invocations.values(), (state) => state.tail),
        ...Array.from(steps.values(), (state) => state.tail),
      ];
      if (pending.length === 0) return;
      await Promise.allSettled(pending);
      const openOnly = [...invocations.values(), ...steps.values()].every((state) => {
        const record = state.record;
        return "outcome" in record ? record.outcome === null : record.finishedAt === null;
      });
      if (openOnly) return;
    }
  };

  const sweep = async (): Promise<void> => {
    const at = now();
    const cutoff = at.getTime() - staleAfterMs;
    for (const [id, state] of invocations) {
      if (state.createdAtMs > cutoff) continue;
      await state.tail;
      const outcome: TraceOutcome = "dropped";
      const finalizeHash = invocationFinalizeHash({ ...state.record, outcome, finishedAt: at })!;
      await write("invocation_finish", id, () =>
        options.store.writeInvocationFinish({ id, outcome, finishedAt: at, finalizeHash }));
      invocations.delete(id);
      onEvent({ type: "state_swept", kind: "invocation", id });
    }
    for (const [id, state] of steps) {
      if (state.createdAtMs > cutoff) continue;
      await state.tail;
      const error = { reason: "dropped" };
      const finalizeHash = stepFinalizeHash({ ...state.record, error, finishedAt: at })!;
      await write("step_finish", id, () => options.store.writeStepFinish({
        id,
        output: null,
        error,
        finishedAt: at,
        finalizeHash,
      }));
      steps.delete(id);
      onEvent({ type: "state_swept", kind: "step", id });
    }
  };

  const interval = options.sweepIntervalMs && options.sweepIntervalMs > 0
    ? setInterval(() => void sweep(), options.sweepIntervalMs)
    : null;
  interval?.unref?.();

  return {
    invocation: { start: startInvocation, finish: finishInvocation },
    step: { start: startStep, finish: finishStep },
    dropCounts: () => ({ ...drops }),
    flush,
    sweep,
    async close() {
      if (closed) return;
      if (interval) clearInterval(interval);
      await flush();
      closed = true;
    },
  };
}

export class InMemoryTraceStore implements TraceStore {
  readonly invocations = new Map<string, InvocationRecord>();
  readonly steps = new Map<string, StepRecord>();

  constructor(
    private readonly options: { beforeWrite?: (kind: TraceWriteKind) => Promise<void> } = {},
  ) {}

  private async before(kind: TraceWriteKind): Promise<void> {
    await this.options.beforeWrite?.(kind);
  }

  async writeInvocationStart(record: InvocationRecord): Promise<void> {
    await this.before("invocation_start");
    this.invocations.set(record.id, { ...record, context: { ...record.context } });
  }

  async writeInvocationFinish(args: { id: string; outcome: TraceOutcome; finishedAt: Date; finalizeHash: string }): Promise<void> {
    await this.before("invocation_finish");
    const record = this.invocations.get(args.id);
    if (!record) throw new Error(`invocation ${args.id} not found`);
    this.invocations.set(args.id, { ...record, outcome: args.outcome, finishedAt: args.finishedAt, finalizeHash: args.finalizeHash });
  }

  async writeStepStart(record: StepRecord): Promise<void> {
    await this.before("step_start");
    this.steps.set(record.id, { ...record, input: { ...record.input } });
  }

  async writeStepFinish(args: { id: string; output: Record<string, unknown> | null; error: Record<string, unknown> | null; finishedAt: Date; finalizeHash: string }): Promise<void> {
    await this.before("step_finish");
    const record = this.steps.get(args.id);
    if (!record) throw new Error(`step ${args.id} not found`);
    this.steps.set(args.id, { ...record, ...args });
  }
}
