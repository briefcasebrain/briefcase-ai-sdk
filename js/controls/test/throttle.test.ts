import { describe, expect, it } from "vitest";

import { classifyProviderError } from "../src/throttle.js";

class ThrottlingException extends Error {}
class ServiceQuotaExceededException extends Error {}

function awsError(shape: Record<string, unknown>): unknown {
  return { message: "aws error", ...shape };
}

describe("classifyProviderError strict defaults", () => {
  it("marks name ThrottlingException as throttled and transient", () => {
    const c = classifyProviderError(awsError({ name: "ThrottlingException" }));
    expect(c).toEqual({ throttled: true, transient: true });
  });

  it("marks name TooManyRequestsException as throttled", () => {
    const c = classifyProviderError(awsError({ name: "TooManyRequestsException" }));
    expect(c).toEqual({ throttled: true, transient: true });
  });

  it("matches a class named ThrottlingException by constructor name", () => {
    const c = classifyProviderError(new ThrottlingException("slow down"));
    expect(c).toEqual({ throttled: true, transient: true });
  });

  it("matches __type containing ThrottlingException", () => {
    const c = classifyProviderError(
      awsError({ __type: "com.amazonaws.bedrock#ThrottlingException" }),
    );
    expect(c).toEqual({ throttled: true, transient: true });
  });

  it("marks $metadata.httpStatusCode 429 as throttled", () => {
    const c = classifyProviderError(awsError({ $metadata: { httpStatusCode: 429 } }));
    expect(c).toEqual({ throttled: true, transient: true });
  });

  it("marks $metadata.httpStatusCode 503 as transient only", () => {
    const c = classifyProviderError(awsError({ $metadata: { httpStatusCode: 503 } }));
    expect(c).toEqual({ throttled: false, transient: true });
  });

  it("marks response.status 429 as throttled", () => {
    const c = classifyProviderError(awsError({ response: { status: 429 } }));
    expect(c).toEqual({ throttled: true, transient: true });
  });

  it("marks response.status_code 503 as transient only", () => {
    const c = classifyProviderError(awsError({ response: { status_code: 503 } }));
    expect(c).toEqual({ throttled: false, transient: true });
  });

  it("marks ServiceQuotaExceededException name as transient only", () => {
    const c = classifyProviderError(new ServiceQuotaExceededException("quota"));
    expect(c).toEqual({ throttled: false, transient: true });
  });

  it("marks ServiceQuotaExceededException code as transient only", () => {
    const c = classifyProviderError(awsError({ code: "ServiceQuotaExceededException" }));
    expect(c).toEqual({ throttled: false, transient: true });
  });

  it("classifies a plain error as neither", () => {
    const c = classifyProviderError(new Error("boom"));
    expect(c).toEqual({ throttled: false, transient: false });
  });
});

describe("classifyProviderError message regex", () => {
  it("is off by default", () => {
    const c = classifyProviderError(new Error("provider rate limit reached"));
    expect(c.throttled).toBe(false);
  });

  it("matches throttle wording when opted in", () => {
    const c = classifyProviderError(new Error("provider rate limit reached"), {
      messageRegex: true,
    });
    expect(c).toEqual({ throttled: true, transient: true });
  });

  it("matches non-object errors by their string form when opted in", () => {
    const c = classifyProviderError("throttled at the gate", { messageRegex: true });
    expect(c).toEqual({ throttled: true, transient: true });
  });
});

describe("classifyProviderError cause chains", () => {
  it("traverses err.cause to find a throttle", () => {
    const err = new Error("wrapped", { cause: awsError({ name: "ThrottlingException" }) });
    expect(classifyProviderError(err).throttled).toBe(true);
  });

  it("combines transient-only signals across the chain", () => {
    const err = new Error("wrapped", {
      cause: awsError({ $metadata: { httpStatusCode: 503 } }),
    });
    expect(classifyProviderError(err)).toEqual({ throttled: false, transient: true });
  });

  it("terminates on cause cycles", () => {
    const a = new Error("a");
    const b = new Error("b");
    (a as { cause?: unknown }).cause = b;
    (b as { cause?: unknown }).cause = a;
    expect(classifyProviderError(a)).toEqual({ throttled: false, transient: false });
  });

  it("stops after the depth cap", () => {
    let leaf: unknown = awsError({ name: "ThrottlingException" });
    for (let i = 0; i < 9; i++) {
      leaf = new Error(`layer ${i}`, { cause: leaf });
    }
    expect(classifyProviderError(leaf).throttled).toBe(false);
  });
});
