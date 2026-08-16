import { describe, expect, it } from "vitest";

import { computeBackoff, withRetry } from "../src/retry.js";

const transient = () => ({ message: "aws", $metadata: { httpStatusCode: 503 } });
const throttle = () => ({ message: "aws", name: "ThrottlingException" });

const noSleep = async (_ms: number) => {};

describe("computeBackoff", () => {
  it("grows exponentially and caps", () => {
    const opts = { baseMs: 500, capMs: 30000, jitterMs: 0 };
    expect(computeBackoff(0, opts)).toBe(500);
    expect(computeBackoff(1, opts)).toBe(1000);
    expect(computeBackoff(2, opts)).toBe(2000);
    expect(computeBackoff(10, opts)).toBe(30000);
  });

  it("bounds jitter", () => {
    for (let i = 0; i < 50; i++) {
      const d = computeBackoff(0, { baseMs: 500, capMs: 30000, jitterMs: 250 });
      expect(d).toBeGreaterThanOrEqual(500);
      expect(d).toBeLessThanOrEqual(750);
    }
  });
});

describe("withRetry", () => {
  it("returns on first success", async () => {
    let calls = 0;
    const result = await withRetry(
      () => {
        calls += 1;
        return "ok";
      },
      { maxAttempts: 3, sleep: noSleep },
    );
    expect(result).toBe("ok");
    expect(calls).toBe(1);
  });

  it("retries transient errors then succeeds", async () => {
    let attempts = 0;
    const slept: number[] = [];
    const result = await withRetry(
      () => {
        attempts += 1;
        if (attempts < 3) throw transient();
        return "ok";
      },
      {
        maxAttempts: 3,
        sleep: async (ms) => {
          slept.push(ms);
        },
      },
    );
    expect(result).toBe("ok");
    expect(attempts).toBe(3);
    expect(slept).toHaveLength(2);
  });

  it("rethrows non-transient errors immediately", async () => {
    let attempts = 0;
    await expect(
      withRetry(
        () => {
          attempts += 1;
          throw new Error("boom");
        },
        { maxAttempts: 5, sleep: noSleep },
      ),
    ).rejects.toThrow("boom");
    expect(attempts).toBe(1);
  });

  it("rethrows the last error when attempts run out", async () => {
    await expect(
      withRetry(
        () => {
          throw transient();
        },
        { maxAttempts: 2, sleep: noSleep },
      ),
    ).rejects.toMatchObject({ $metadata: { httpStatusCode: 503 } });
  });

  it("does not retry throttles when retryThrottled is false", async () => {
    let attempts = 0;
    await expect(
      withRetry(
        () => {
          attempts += 1;
          throw throttle();
        },
        { maxAttempts: 5, retryThrottled: false, sleep: noSleep },
      ),
    ).rejects.toMatchObject({ name: "ThrottlingException" });
    expect(attempts).toBe(1);
  });

  it("stops before a sleep would pass the deadline", async () => {
    let now = 0;
    let attempts = 0;
    await expect(
      withRetry(
        () => {
          attempts += 1;
          throw transient();
        },
        {
          maxAttempts: 100,
          baseMs: 1000,
          jitterMs: 0,
          deadlineMs: 2500,
          sleep: async (ms) => {
            now += ms;
          },
          clock: () => now,
        },
      ),
    ).rejects.toMatchObject({ $metadata: { httpStatusCode: 503 } });
    // Backoffs 1000ms + 2000ms pass the 2500ms deadline on the second sleep.
    expect(attempts).toBeLessThanOrEqual(3);
  });

  it("stops retrying when the signal aborts between attempts", async () => {
    const controller = new AbortController();
    controller.abort();
    let attempts = 0;
    await expect(
      withRetry(
        () => {
          attempts += 1;
          throw transient();
        },
        { maxAttempts: 5, signal: controller.signal, sleep: noSleep },
      ),
    ).rejects.toMatchObject({ $metadata: { httpStatusCode: 503 } });
    expect(attempts).toBe(1);
  });

  it("supports async callables", async () => {
    let attempts = 0;
    const result = await withRetry(
      async () => {
        attempts += 1;
        if (attempts < 2) throw transient();
        return "ok";
      },
      { maxAttempts: 3, sleep: noSleep },
    );
    expect(result).toBe("ok");
    expect(attempts).toBe(2);
  });
});
