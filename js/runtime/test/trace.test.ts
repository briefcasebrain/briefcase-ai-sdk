import { describe, expect, it, vi } from "vitest";

import {
  InMemoryTraceStore,
  TraceBackpressureError,
  createTraceRecorder,
  snapshotTracePayload,
  type TraceEvent,
  verifyInvocationRecord,
  verifyStepRecord,
} from "../src/trace/index.js";

describe("trace recorder", () => {
  it("serializes invocation start before finish and flushes pending writes", async () => {
    const order: string[] = [];
    const store = new InMemoryTraceStore({
      beforeWrite: async (kind) => {
        if (kind === "invocation_start") await Promise.resolve();
        order.push(kind);
      },
    });
    let id = 0;
    const recorder = createTraceRecorder({ store, idGenerator: () => `id-${++id}` });
    const invocationId = recorder.invocation.start({
      tenantId: "tenant-1",
      actorType: "agent",
      actorId: "agent-1",
      intent: "answer",
      context: { prompt: "hello" },
    });
    recorder.invocation.finish({ invocationId, outcome: "success" });
    await recorder.flush();
    expect(order).toEqual(["invocation_start", "invocation_finish"]);
    expect(store.invocations.get(invocationId)?.outcome).toBe("success");
    await recorder.close();
  });

  it("assigns step sequence synchronously and orders step finalization", async () => {
    const store = new InMemoryTraceStore();
    let id = 0;
    const recorder = createTraceRecorder({ store, idGenerator: () => `id-${++id}` });
    const invocationId = recorder.invocation.start({ tenantId: "t", actorType: "agent", actorId: "a", intent: "i", context: {} });
    const first = recorder.step.start({ invocationId, stepType: "tool", name: "one", input: {} });
    const second = recorder.step.start({ invocationId, stepType: "llm", name: "two", input: {} });
    recorder.step.finish({ stepId: first, output: { ok: true } });
    await recorder.flush();
    expect(store.steps.get(first)?.sequence).toBe(0);
    expect(store.steps.get(second)?.sequence).toBe(1);
    expect(store.steps.get(first)?.output).toEqual({ ok: true });
    await recorder.close();
  });

  it("writes an active step finish before its invocation finish", async () => {
    const order: string[] = [];
    let releaseStep!: () => void;
    const stepGate = new Promise<void>((resolve) => { releaseStep = resolve; });
    const store = new InMemoryTraceStore({
      beforeWrite: async (kind) => {
        if (kind === "step_finish") await stepGate;
        order.push(kind);
      },
    });
    let id = 0;
    const recorder = createTraceRecorder({ store, idGenerator: () => `id-${++id}` });
    const invocationId = recorder.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context: {} });
    const stepId = recorder.step.start({ invocationId, stepType: "tool", name: "x", input: {} });
    recorder.step.finish({ stepId, output: { ok: true } });
    recorder.invocation.finish({ invocationId, outcome: "success" });
    await Promise.resolve();
    expect(order).not.toContain("invocation_finish");
    releaseStep();
    await recorder.flush();
    expect(order).toEqual(["invocation_start", "step_start", "step_finish", "invocation_finish"]);
    await recorder.close();
  });

  it("snapshots caller-owned payloads before asynchronous writes", async () => {
    const store = new InMemoryTraceStore();
    const recorder = createTraceRecorder({ store, idGenerator: () => "inv-1" });
    const context = { nested: { value: "original" } };
    const invocationId = recorder.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context });
    context.nested.value = "mutated";
    await recorder.flush();
    const record = store.invocations.get(invocationId)!;
    expect(record.context).toEqual({ nested: { value: "original" } });
    expect(verifyInvocationRecord(record)).toBe(true);
    await recorder.close();
  });

  it("retries transient writes and counts exhausted drops", async () => {
    let attempts = 0;
    const transientStore = new InMemoryTraceStore({
      beforeWrite: async () => {
        if (attempts++ === 0) throw new Error("ECONNRESET");
      },
    });
    const recorder = createTraceRecorder({
      store: transientStore,
      idGenerator: () => "inv-1",
      sleep: async () => undefined,
    });
    recorder.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context: {} });
    await recorder.flush();
    expect(attempts).toBe(2);
    expect(recorder.dropCounts().invocationStart).toBe(0);
    await recorder.close();

    const failed = createTraceRecorder({
      store: new InMemoryTraceStore({ beforeWrite: async () => { throw new Error("permanent"); } }),
      idGenerator: () => "inv-2",
    });
    failed.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context: {} });
    await failed.flush();
    expect(failed.dropCounts().invocationStart).toBe(1);
    await failed.close();
  });

  it("emits orphan events without writing unknown steps", async () => {
    const events: string[] = [];
    const store = new InMemoryTraceStore();
    const recorder = createTraceRecorder({ store, onEvent: (event) => events.push(event.type) });
    recorder.step.start({ invocationId: "missing", stepType: "tool", name: "x", input: {} });
    recorder.step.finish({ stepId: "missing", output: {} });
    await recorder.flush();
    expect(events).toEqual(["orphan_step_start", "orphan_step_finish"]);
    expect(store.steps.size).toBe(0);
    await recorder.close();
  });

  it("sweeps stale open state to dropped records", async () => {
    const store = new InMemoryTraceStore();
    let current = new Date("2026-01-01T00:00:00Z");
    const recorder = createTraceRecorder({
      store,
      now: () => current,
      staleAfterMs: 1000,
      idGenerator: () => "inv-1",
    });
    recorder.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context: {} });
    await recorder.flush();
    current = new Date("2026-01-01T00:00:02Z");
    await recorder.sweep();
    expect(store.invocations.get("inv-1")?.outcome).toBe("dropped");
    await recorder.close();
  });

  it("verifies start and finalize hashes and detects mutations", async () => {
    const store = new InMemoryTraceStore();
    const recorder = createTraceRecorder({ store, idGenerator: () => "inv-1" });
    const invocationId = recorder.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context: {} });
    const stepId = recorder.step.start({ invocationId, stepType: "tool", name: "x", input: { a: 1 } });
    recorder.step.finish({ stepId, output: { ok: true } });
    recorder.invocation.finish({ invocationId, outcome: "success" });
    await recorder.flush();
    const invocation = store.invocations.get(invocationId)!;
    const step = store.steps.get(stepId)!;
    expect(verifyInvocationRecord(invocation)).toBe(true);
    expect(verifyStepRecord(step)).toBe(true);
    expect(verifyInvocationRecord({ ...invocation, outcome: "error" })).toBe(false);
    expect(verifyStepRecord({ ...step, output: { ok: false } })).toBe(false);
    expect(verifyInvocationRecord({ ...invocation, finishedAt: null, finalizeHash: null })).toBe(false);
    expect(verifyStepRecord({ ...step, finishedAt: null, finalizeHash: null })).toBe(false);
    await recorder.close();
  });

  it("covers non-JSON payload values in the integrity hash instead of throwing or hashing {}", async () => {
    const store = new InMemoryTraceStore();
    const events: TraceEvent[] = [];
    let id = 0;
    const recorder = createTraceRecorder({ store, idGenerator: () => `id-${++id}`, onEvent: (event) => events.push(event) });
    const start = (at: Date) => recorder.invocation.start({
      tenantId: "t",
      actorType: "a",
      actorId: "1",
      intent: "i",
      context: { at, tags: new Set(["x"]), meta: new Map([["k", 1n]]), skip: undefined, fn: () => 1 },
    });
    const first = start(new Date("2026-01-01T00:00:00.000Z"));
    const stepId = recorder.step.start({ invocationId: first, stepType: "tool", name: "x", input: { bytes: new Uint8Array(2) } });
    const circular: Record<string, unknown> = { n: Number.NaN };
    circular.self = circular;
    recorder.step.finish({ stepId, error: { cause: new RangeError("boom"), circular } });
    await recorder.flush();

    const record = store.invocations.get(first)!;
    expect(record.context).toEqual({
      at: "2026-01-01T00:00:00.000Z",
      tags: ["x"],
      meta: { k: "1" },
      fn: "[unserializable function]",
    });
    // The stored record is plain JSON, so it survives a store round-trip.
    expect(JSON.parse(JSON.stringify(record.context))).toEqual(record.context);
    expect(verifyInvocationRecord(record)).toBe(true);
    expect(verifyInvocationRecord({ ...record, context: { ...record.context, at: "2026-01-02T00:00:00.000Z" } })).toBe(false);

    const step = store.steps.get(stepId)!;
    expect(step.input).toEqual({ bytes: "[unserializable Uint8Array]" });
    expect(step.error).toEqual({
      cause: { name: "RangeError", message: "boom" },
      circular: { n: "NaN", self: "[unserializable circular reference]" },
    });
    expect(verifyStepRecord(step)).toBe(true);

    const sanitized = events.filter((event) => event.type === "payload_sanitized");
    expect(sanitized[0]).toEqual({
      type: "payload_sanitized",
      kind: "invocation_start",
      id: first,
      paths: ["at", "tags", "meta", "meta.k", "fn"],
    });
    expect(sanitized.map((event) => event.kind)).toEqual(["invocation_start", "step_start", "step_finish"]);

    // A record still holding a non-JSON value fails verification rather than throwing.
    expect(verifyInvocationRecord({ ...record, context: { at: new Map() } })).toBe(false);
    await recorder.close();
  });

  it("snapshots plain JSON without reporting anything", () => {
    const input = { a: [1, "two", null, true, { b: {} }], "__proto__x": 1 };
    const { value, sanitizedPaths } = snapshotTracePayload(input);
    expect(value).toEqual(input);
    expect(value).not.toBe(input);
    expect(sanitizedPaths).toEqual([]);
    const shared = { v: 1 };
    expect(snapshotTracePayload({ a: shared, b: shared }).sanitizedPaths).toEqual([]);
  });

  it("drops new starts once the pending-write bound is reached and still finishes admitted records", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const store = new InMemoryTraceStore({ beforeWrite: () => gate });
    const events: TraceEvent[] = [];
    let id = 0;
    const recorder = createTraceRecorder({
      store,
      maxPendingWrites: 2,
      idGenerator: () => `id-${++id}`,
      onEvent: (event) => events.push(event),
    });
    const context = {};
    const first = recorder.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context });
    const step = recorder.step.start({ invocationId: first, stepType: "tool", name: "x", input: {} });
    // Two writes pending: further starts are refused without throwing.
    const refusedInvocation = recorder.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context });
    const refusedStep = recorder.step.start({ invocationId: first, stepType: "tool", name: "y", input: {} });
    expect(recorder.dropCounts()).toMatchObject({ invocationStart: 1, stepStart: 1 });
    const dropped = events.filter((event) => event.type === "write_dropped");
    expect(dropped.map((event) => event.id)).toEqual([refusedInvocation, refusedStep]);
    expect(dropped[0]!.error).toBeInstanceOf(TraceBackpressureError);
    expect((dropped[0]!.error as TraceBackpressureError).maxPendingWrites).toBe(2);
    // Finishes of admitted records are always queued.
    recorder.step.finish({ stepId: step, output: { ok: true } });
    recorder.invocation.finish({ invocationId: first, outcome: "success" });
    release();
    await recorder.flush();
    expect([...store.invocations.keys()]).toEqual([first]);
    expect(store.invocations.get(first)?.outcome).toBe("success");
    expect(store.steps.get(step)?.output).toEqual({ ok: true });
    // Capacity is released once the queue drains.
    const later = recorder.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context });
    await recorder.flush();
    expect(store.invocations.has(later)).toBe(true);
    await recorder.close();
    expect(() => createTraceRecorder({ store, maxPendingWrites: 0 })).toThrow(RangeError);
  });

  it("rejects new records after close", async () => {
    const recorder = createTraceRecorder({ store: new InMemoryTraceStore() });
    await recorder.close();
    expect(() => recorder.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context: {} })).toThrow(/closed/);
  });
});
