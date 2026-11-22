import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Child-process tests run the compiled package (dist/), built once here.
    globalSetup: ["./test/global-setup.ts"],
    testTimeout: 20_000,
  },
});
