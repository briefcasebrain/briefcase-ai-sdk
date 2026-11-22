import { context, trace, TraceFlags } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import OpenAI from "openai";
import { afterAll, describe, expect, it } from "vitest";

import { undo } from "../src/auto.js";
import { currentWorkflow, resetOtelForTests, withWorkflow } from "../src/context.js";
import { shutdown } from "../src/observe.js";
import { SECRET_KEY, fakeFetch, fresh, json, records } from "./helpers.js";

const completion = {
  id: "chatcmpl-1",
  object: "chat.completion",
  created: 1,
  model: "gpt-4o-mini",
  choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
};

function client() {
  return new OpenAI({
    apiKey: SECRET_KEY,
    maxRetries: 0,
    fetch: fakeFetch(async () => {
      await new Promise((r) => setTimeout(r, 2)); // cross an async boundary
      return json(completion);
    }),
  });
}

afterAll(async () => {
  undo();
  await shutdown();
});

describe("correlation", () => {
  it("withWorkflow() carries one workflow_id across awaits and concurrent calls", async () => {
    const mem = await fresh({ only: ["openai"] });
    const ids = await Promise.all(
      ["triage", "decide"].map((name) =>
        withWorkflow(name, async () => {
          await client().chat.completions.create({ model: "gpt-4o-mini", messages: [] });
          await client().chat.completions.create({ model: "gpt-4o-mini", messages: [] });
          return currentWorkflow()!.workflowId;
        }),
      ),
    );
    await client().chat.completions.create({ model: "gpt-4o-mini", messages: [] });
    const all = await records(mem, 5);
    expect(ids[0]).toMatch(/^wf_[0-9a-f]{12}$/);
    expect(all.filter((r) => r.workflow_id === ids[0])).toHaveLength(2);
    expect(all.filter((r) => r.workflow_id === ids[1])).toHaveLength(2);
    expect(all.filter((r) => r.workflow_id === undefined)).toHaveLength(1);
  });

  it("picks up the active OpenTelemetry span as trace_id / parent_span_id / trace_flags", async () => {
    const manager = new AsyncLocalStorageContextManager().enable();
    context.setGlobalContextManager(manager);
    resetOtelForTests();
    try {
      const mem = await fresh({ only: ["openai"] });
      const parent = trace.wrapSpanContext({
        traceId: "4bf92f3577b34da6a3ce929d0e0e4736",
        spanId: "00f067aa0ba902b7",
        traceFlags: TraceFlags.SAMPLED,
      });
      await context.with(trace.setSpan(context.active(), parent), () =>
        client().chat.completions.create({ model: "gpt-4o-mini", messages: [] }),
      );
      await client().chat.completions.create({ model: "gpt-4o-mini", messages: [] });
      const [inside, outside] = await records(mem, 2);
      expect(inside).toMatchObject({ trace_id: "4bf92f3577b34da6a3ce929d0e0e4736", parent_span_id: "00f067aa0ba902b7", trace_flags: 1 });
      expect(outside).not.toHaveProperty("trace_id");
    } finally {
      context.disable();
      resetOtelForTests();
    }
  });
});
