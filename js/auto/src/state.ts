/**
 * Process-wide state, kept on `globalThis` under a registered symbol so two
 * copies of this package (an ESM import and a bundled copy, say) share one
 * queue and never patch the same SDK twice.
 */

import type { PendingCall, Pipeline } from "./pipeline.js";
import type { ProviderReport } from "./instrument/core.js";

export interface GlobalState {
  pipeline: Pipeline | null;
  pending: Set<PendingCall>;
  providers: Map<string, ProviderReport>;
  hooksInstalled: boolean;
}

const KEY = Symbol.for("briefcase-ai.auto.state.v1");

export function state(): GlobalState {
  const holder = globalThis as unknown as Record<symbol, GlobalState | undefined>;
  let current = holder[KEY];
  if (!current) {
    current = { pipeline: null, pending: new Set(), providers: new Map(), hooksInstalled: false };
    holder[KEY] = current;
  }
  return current;
}
