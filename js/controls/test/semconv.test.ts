import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import * as semconv from "../src/semconv.js";

const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/semconv_controls.json", import.meta.url), "utf8"),
) as { constants: Record<string, string> };

describe("semconv parity with tests/fixtures/semconv_controls.json", () => {
  const tsConstants = Object.fromEntries(
    Object.entries(semconv).filter(([, v]) => typeof v === "string"),
  ) as Record<string, string>;

  it("loads a non-empty constant table from the fixture", () => {
    expect(Object.keys(fixture.constants).length).toBeGreaterThan(0);
  });

  it("exports exactly the fixture constant names and values", () => {
    // toEqual on the full records checks both directions: no missing
    // constants, no extras, and identical values.
    expect(tsConstants).toEqual(fixture.constants);
  });
});
