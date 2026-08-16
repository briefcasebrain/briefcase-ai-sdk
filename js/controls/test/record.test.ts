import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  CAPTURE_CONTENT_MODES,
  buildDecisionRecord,
  finalizeDecisionRecord,
  sha256Hex,
  type CaptureContentMode,
  type DecisionRecord,
} from "../src/record.js";

const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/decision_record.json", import.meta.url), "utf8"),
) as { required_fields: string[]; optional_fields: string[] };

function sha(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

function makeRecord(
  mode: CaptureContentMode,
  opts: { contextVersion?: string; error?: unknown; result?: unknown } = {},
): DecisionRecord {
  const record = buildDecisionRecord({
    decisionType: "classify_risk",
    functionName: "classify_risk",
    args: ["claim-123"],
    captureContent: mode,
    ...(opts.contextVersion !== undefined ? { contextVersion: opts.contextVersion } : {}),
  });
  const startedAt = new Date();
  if ("error" in opts) {
    finalizeDecisionRecord(record, startedAt, { captureContent: mode, error: opts.error });
  } else {
    finalizeDecisionRecord(record, startedAt, {
      captureContent: mode,
      result: opts.result ?? "approved",
    });
  }
  return record;
}

describe("decision record wire schema (fixture parity)", () => {
  const modes = ["full", "hash", "none"] as const;

  it("exposes the fixture's modes", () => {
    expect([...CAPTURE_CONTENT_MODES].sort()).toEqual(["full", "hash", "none"]);
  });

  for (const mode of modes) {
    it(`success record in ${mode} mode carries exactly the required fields`, () => {
      const record = makeRecord(mode);
      expect(Object.keys(record).sort()).toEqual([...fixture.required_fields].sort());
    });

    it(`error record in ${mode} mode adds only the error field`, () => {
      const record = makeRecord(mode, { error: new TypeError("boom") });
      expect(Object.keys(record).sort()).toEqual(
        [...fixture.required_fields, "error"].sort(),
      );
    });
  }

  it("context_version appears only when provided", () => {
    const record = makeRecord("full", { contextVersion: "v3" });
    expect(Object.keys(record).sort()).toEqual(
      [...fixture.required_fields, "context_version"].sort(),
    );
    expect(record.context_version).toBe("v3");
  });

  it("optional fields in the fixture stay optional", () => {
    expect([...fixture.optional_fields].sort()).toEqual(["context_version", "error"]);
  });
});

describe("captureContent mode semantics", () => {
  it("full mode records serialized inputs and outputs", () => {
    const record = makeRecord("full");
    expect(String(record.inputs["args"])).toContain("claim-123");
    expect(String(record.outputs["result"])).toContain("approved");
  });

  it("full mode applies redact then truncates", () => {
    const record = buildDecisionRecord({
      decisionType: "t",
      functionName: "t",
      args: ["secret-content"],
      captureContent: "full",
      maxInputChars: 6,
      redact: (text) => text.replaceAll("secret", "REDACT"),
    });
    expect(record.inputs["args"]).toBe("[\"REDA");
  });

  it("full mode error carries the redacted message text", () => {
    const record = makeRecord("full", { error: new TypeError("boom") });
    expect(record.error).toBe("boom");
  });

  it("hash mode replaces content with sha256 digests and char counts", () => {
    const record = makeRecord("hash");
    const text = JSON.stringify(["claim-123"]);
    expect(record.inputs).toEqual({ args_sha256: sha(text), args_chars: text.length });
    const out = JSON.stringify("approved");
    expect(record.outputs).toEqual({
      result_sha256: sha(out),
      result_chars: out.length,
      result_type: "String",
    });
  });

  it("hash mode error carries class name and digest, never the message", () => {
    const record = makeRecord("hash", { error: new TypeError("boom") });
    expect(record.error).toBe(`TypeError:sha256:${sha("boom")}`);
  });

  it("none mode records shape only", () => {
    const record = makeRecord("none");
    expect(record.inputs).toEqual({ args_count: 1 });
    expect(record.outputs).toEqual({ result_type: "String" });
  });

  it("none mode error carries the class name only", () => {
    const record = makeRecord("none", { error: new TypeError("boom") });
    expect(record.error).toBe("TypeError");
  });

  it("kwargs are captured per mode", () => {
    const record = buildDecisionRecord({
      decisionType: "t",
      functionName: "t",
      kwargs: { claim: "claim-123" },
      captureContent: "none",
    });
    expect(record.inputs).toEqual({ kwargs_count: 1 });
  });

  it("rejects unknown modes", () => {
    expect(() =>
      buildDecisionRecord({
        decisionType: "t",
        functionName: "t",
        captureContent: "loud" as CaptureContentMode,
      }),
    ).toThrow(/captureContent/);
  });

  it("sha256Hex matches node:crypto", () => {
    expect(sha256Hex("boom")).toBe(sha("boom"));
  });

  it("execution_time_ms measures the clock delta", () => {
    let tick = 0;
    const clock = () => new Date(1_700_000_000_000 + 250 * tick++);
    const record = buildDecisionRecord({
      decisionType: "t",
      functionName: "t",
      clock,
    });
    finalizeDecisionRecord(record, new Date(1_700_000_000_000), { result: 1, clock });
    expect(record.execution_time_ms).toBe(250);
  });
});
