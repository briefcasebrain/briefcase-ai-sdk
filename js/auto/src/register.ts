/**
 * `node --import @briefcase-ai/auto/register app.js`
 *
 * Zero-code activation: `observe()` (destination from the environment) and
 * `auto()` before the application's own modules load. See `activate.ts`
 * for when it activates. No top-level await, so it also loads with
 * `--require` in CommonJS programs (require(esm), Node >= 20.19).
 */

import { fileURLToPath } from "node:url";

import { activate, preloadedOnCommandLine } from "./activate.js";

const here = fileURLToPath(import.meta.url);
export const activation = activate(process.env, preloadedOnCommandLine(process.execArgv, [here]));
