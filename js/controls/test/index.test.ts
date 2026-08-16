import { describe, expect, it } from "vitest";

import * as controls from "../src/index.js";

describe("package barrel", () => {
  it("exports the controls surface", () => {
    expect(typeof controls.classifyProviderError).toBe("function");
    expect(typeof controls.computeBackoff).toBe("function");
    expect(typeof controls.withRetry).toBe("function");
    expect(typeof controls.createGateway).toBe("function");
    expect(typeof controls.collapseLegacyReason).toBe("function");
    expect(typeof controls.runSuggestionPipeline).toBe("function");
    expect(typeof controls.sha256Fingerprint).toBe("function");
    expect(typeof controls.buildDecisionRecord).toBe("function");
    expect(typeof controls.finalizeDecisionRecord).toBe("function");
    expect(typeof controls.MemoryQuotaStore).toBe("function");
    expect(typeof controls.ConsoleExporter).toBe("function");
    expect(typeof controls.JsonlFileExporter).toBe("function");
    expect(typeof controls.MemoryExporter).toBe("function");
    expect(controls.CONTROLS_GATEWAY_BUCKET).toBe("briefcase.controls.gateway.bucket");
  });

  it("keeps bedrock on the subpath only", () => {
    expect("resolveModelId" in controls).toBe(false);
    expect("priceForUsage" in controls).toBe(false);
  });
});
