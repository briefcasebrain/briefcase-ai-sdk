/**
 * Per-thread state, kept on `globalThis` under a registered symbol so two
 * copies of this package (an ESM import and a bundled copy, say) share one
 * queue and never patch the same SDK twice. Each worker thread has its own
 * `globalThis`, so its own state.
 */

import type { PendingCall, Pipeline } from "./pipeline.js";
import type { ProviderReport } from "./instrument/core.js";
import type { LoadedCopy } from "./loader.js";

export interface GlobalState {
  pipeline: Pipeline | null;
  pending: Set<PendingCall>;
  providers: Map<string, ProviderReport>;
  hooksInstalled: boolean;
  /** SDK module copies that have reported themselves (see loader.ts). */
  copies: Map<string, LoadedCopy>;
  loadHooks: "none" | "installed" | "failed";
  /** True when the load hook was installed by the preload, before any application module loaded. */
  hooksAtStartup: boolean;
  /** Request detection (detect.ts) is subscribed. */
  detecting: boolean;
  /** Main thread: the channel worker threads forward records on (observe.ts). */
  workerChannel?: object;
}

const KEY = Symbol.for("briefcase-ai.auto.state.v1");

export function state(): GlobalState {
  const holder = globalThis as unknown as Record<symbol, GlobalState | undefined>;
  let current = holder[KEY];
  if (current && !current.copies) {
    // Left by an older copy of this package in the same thread: add what it lacks.
    Object.assign(current, { copies: new Map(), loadHooks: "none", hooksAtStartup: false, detecting: false });
  }
  if (!current) {
    current = {
      pipeline: null,
      pending: new Set(),
      providers: new Map(),
      hooksInstalled: false,
      copies: new Map(),
      loadHooks: "none",
      hooksAtStartup: false,
      detecting: false,
    };
    holder[KEY] = current;
  }
  return current;
}
