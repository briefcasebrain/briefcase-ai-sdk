import { describe, expect, it, vi } from "vitest";

import {
  InMemoryTraceStore,
  createTraceRecorder,
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

  it("rejects new records after close", async () => {
    const recorder = createTraceRecorder({ store: new InMemoryTraceStore() });
    await recorder.close();
    expect(() => recorder.invocation.start({ tenantId: "t", actorType: "a", actorId: "1", intent: "i", context: {} })).toThrow(/closed/);
  });
});
