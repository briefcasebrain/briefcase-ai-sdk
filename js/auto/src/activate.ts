/**
 * Zero-code activation, used by `register.ts` (`node --import
 * @briefcase-ai/auto/register app.js`). The rule:
 *
 * - `BRIEFCASE_AUTO=0` (or `false`): never activates.
 * - `BRIEFCASE_AUTO=1`: activates.
 * - Otherwise it activates only when `--import @briefcase-ai/auto/register`
 *   (or `--require`) is on this process's own command line
 *   (`process.execArgv`). A register module that arrives only through an
 *   inherited `NODE_OPTIONS` does nothing without `BRIEFCASE_AUTO=1`:
 *   `NODE_OPTIONS` reaches every Node process started below it (npm, tsc,
 *   test runners, editors), and those should not start exporting records.
 *   `briefcase exec` sets `BRIEFCASE_AUTO=1` itself.
 *
 * It runs on the main thread and in worker threads (a worker inherits the
 * parent's `execArgv` and environment, so the same rule applies there; a
 * worker's records go to the main thread when the main thread is
 * observing, see observe.ts). Activation calls `observe()`
 * (destination from the environment) and `auto()` with
 * `BRIEFCASE_CAPTURE_CONTENT` (none | hash | full, default none) and
 * `BRIEFCASE_AUTO_SKIP` (comma-separated provider names) and
 * `BRIEFCASE_AUTO_EAGER=1` (patch every installed SDK copy up front instead
 * of when the application loads it). Nothing here
 * throws into the program: a failure is reported in one stderr line and the
 * program runs uninstrumented.
 */

import { resolve } from "node:path";

import { auto, markStartup, PROVIDER_NAMES, type AutoStatus } from "./auto.js";
import { observe } from "./observe.js";

const PRELOAD_FLAGS = new Set(["--import", "--require", "-r", "--loader", "--experimental-loader"]);

/** True when one of `registerPaths` is preloaded by this process's own command line. */
export function preloadedOnCommandLine(execArgv: readonly string[], registerPaths: readonly string[]): boolean {
  const matches = (value: string): boolean => {
    if (/^@briefcase-ai\/auto\/register(\.js)?$/.test(value)) return true;
    let candidate = value;
    if (candidate.startsWith("file:")) {
      try {
        candidate = new URL(candidate).pathname;
      } catch {
        return false;
      }
    }
    return registerPaths.some((p) => resolve(candidate) === p);
  };
  for (let i = 0; i < execArgv.length; i++) {
    const arg = execArgv[i]!;
    const eq = arg.indexOf("=");
    if (eq > 0 && PRELOAD_FLAGS.has(arg.slice(0, eq))) {
      if (matches(arg.slice(eq + 1))) return true;
    } else if (PRELOAD_FLAGS.has(arg) && i + 1 < execArgv.length) {
      if (matches(execArgv[i + 1]!)) return true;
      i++;
    }
  }
  return false;
}

export interface Activation {
  active: boolean;
  reason: string;
  status?: AutoStatus;
}

function report(message: string): void {
  try {
    process.stderr.write(`briefcase: ${message}\n`);
  } catch {
    // never break the program
  }
}

export function activate(env: Record<string, string | undefined>, explicit: boolean): Activation {
  const flag = (env["BRIEFCASE_AUTO"] ?? "").trim().toLowerCase();
  if (flag === "0" || flag === "false") return { active: false, reason: "BRIEFCASE_AUTO=0" };
  if (flag !== "1" && flag !== "true" && !explicit) {
    return { active: false, reason: "loaded through NODE_OPTIONS without BRIEFCASE_AUTO=1" };
  }
  try {
    let content = (env["BRIEFCASE_CAPTURE_CONTENT"] ?? "none").trim().toLowerCase();
    if (!["none", "hash", "full"].includes(content)) {
      report(`ignoring BRIEFCASE_CAPTURE_CONTENT=${JSON.stringify(content)} (expected none, hash or full); content capture stays off`);
      content = "none";
    }
    const skip = (env["BRIEFCASE_AUTO_SKIP"] ?? "")
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s);
    const unknown = skip.filter((s) => !PROVIDER_NAMES.includes(s));
    if (unknown.length) report(`ignoring unknown names in BRIEFCASE_AUTO_SKIP: ${unknown.join(", ")}`);
    observe();
    const eager = ["1", "true"].includes((env["BRIEFCASE_AUTO_EAGER"] ?? "").trim().toLowerCase());
    // The preload runs before any application module: from here the load hook sees every SDK load.
    if (!eager) markStartup();
    const status = auto({
      captureContent: content as "none" | "hash" | "full",
      skip: skip.filter((s) => PROVIDER_NAMES.includes(s)),
      eager,
    });
    return { active: true, reason: flag === "1" || flag === "true" ? "BRIEFCASE_AUTO=1" : "--import on the command line", status };
  } catch (error) {
    const name = error instanceof Error ? error.name : "Error";
    const message = error instanceof Error ? error.message : String(error);
    report(`auto-instrumentation is disabled for this process (${name}: ${message})`);
    return { active: false, reason: `failed: ${message}` };
  }
}
