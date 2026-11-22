/**
 * `auto()`: instrument every installed provider SDK; `undo()` and `status()`.
 *
 * Same provider names and decision types as Python's `briefcase.auto()`.
 * A package that is not installed is skipped silently; one that is installed
 * but does not have the verified layout is skipped with one warning and is
 * never half-patched. Calling `auto()` again does not wrap anything twice.
 */

import * as anthropic from "./instrument/anthropic.js";
import * as bedrock from "./instrument/bedrock.js";
import {
  type CaptureOptions,
  type ModuleCopy,
  type PatchTarget,
  type ProviderReport,
  DEFAULT_OPTIONS,
  Instrumentor,
  loadCopies,
  normalizeContentMode,
  packageVersion,
} from "./instrument/core.js";
import * as genai from "./instrument/google-genai.js";
import * as openai from "./instrument/openai.js";
import type { PipelineStats } from "./pipeline.js";
import { state } from "./state.js";
import { debug } from "./util.js";
import { exportStats } from "./observe.js";

export interface AutoOptions {
  /**
   * Record prompt / completion content. `false` / "none" (default): nothing.
   * `true` / "full": content, each string cut at `maxContentChars`.
   * "hash": a SHA-256 digest and a character count instead.
   */
  captureContent?: boolean | "none" | "hash" | "full";
  /** Bound on each captured string (default 8000). */
  maxContentChars?: number;
  /** Applied to every captured string in "full" mode; a throwing hook yields "<redaction-failed>". */
  redact?: (text: string) => string;
  /** Added to every record as `context_version`. */
  contextVersion?: string;
  /** openai: request `stream_options.include_usage` on chat streams (adds a usage-only final chunk). */
  streamUsage?: boolean;
  /** Provider names to leave alone: "openai", "anthropic", "google-genai", "bedrock". */
  skip?: string[];
  /** Only these providers. */
  only?: string[];
}

interface ProviderSpec {
  name: string;
  packageName: string;
  verifiedVersion: string;
  specifiers: string[];
  targets(inst: Instrumentor, modules: Record<string, Record<string, unknown> | undefined>, copy: string): PatchTarget[] | { targets: PatchTarget[]; verify: () => string | null };
}

export const PROVIDERS: ProviderSpec[] = [
  { name: openai.PROVIDER, packageName: openai.PACKAGE, verifiedVersion: openai.VERIFIED_VERSION, specifiers: openai.SPECIFIERS, targets: openai.targets },
  { name: anthropic.PROVIDER, packageName: anthropic.PACKAGE, verifiedVersion: anthropic.VERIFIED_VERSION, specifiers: anthropic.SPECIFIERS, targets: anthropic.targets },
  { name: genai.PROVIDER, packageName: genai.PACKAGE, verifiedVersion: genai.VERIFIED_VERSION, specifiers: genai.SPECIFIERS, targets: genai.targets },
  { name: bedrock.PROVIDER, packageName: bedrock.PACKAGE, verifiedVersion: bedrock.VERIFIED_VERSION, specifiers: bedrock.SPECIFIERS, targets: bedrock.targets },
];

export const PROVIDER_NAMES = PROVIDERS.map((p) => p.name);

function options(input: AutoOptions): CaptureOptions {
  const out: CaptureOptions = {
    ...DEFAULT_OPTIONS,
    captureContent: normalizeContentMode(input.captureContent),
    maxContentChars: input.maxContentChars ?? DEFAULT_OPTIONS.maxContentChars,
    streamUsage: input.streamUsage ?? false,
  };
  if (input.redact) out.redact = input.redact;
  if (input.contextVersion !== undefined) out.contextVersion = input.contextVersion;
  return out;
}

/** Test seam: replaces how SDK copies are found. */
let copyLoader: (specifier: string) => ReturnType<typeof loadCopies> = loadCopies;
export function setCopyLoaderForTests(loader: typeof copyLoader | null): void {
  copyLoader = loader ?? loadCopies;
}

function instrumentProvider(spec: ProviderSpec, capture: CaptureOptions): ProviderReport {
  const inst = new Instrumentor(spec.name, capture, spec.packageName, spec.verifiedVersion);
  const report = inst.report;
  const byFormat = new Map<string, Record<string, Record<string, unknown> | undefined>>();
  let installed = false;
  for (const specifier of spec.specifiers) {
    const found = copyLoader(specifier);
    if (!found) continue;
    installed = true;
    for (const problem of found.problems) report.skipped.push({ target: specifier, reason: problem });
    for (const copy of found.copies as ModuleCopy[]) {
      report.sdkVersion ??= packageVersion(copy.path, spec.packageName);
      const modules = byFormat.get(copy.format) ?? {};
      modules[specifier] = copy.ns;
      byFormat.set(copy.format, modules);
    }
  }
  if (!installed) {
    report.state = "not-installed";
    delete report.instrumentor;
    return report;
  }
  for (const [format, modules] of byFormat) {
    const built = spec.targets(inst, modules, format);
    const { targets, verify } = Array.isArray(built) ? { targets: built, verify: undefined } : built;
    inst.applyAll(format, targets, verify);
  }
  report.state = report.patched.length > 0 ? "patched" : "skipped";
  if (report.state === "skipped") inst.active = false;
  debug(`${spec.name}: ${report.state} (${report.patched.length} target(s), sdk ${report.sdkVersion ?? "unknown"})`);
  return report;
}

export interface AutoStatus {
  observing: boolean;
  destination: string | null;
  providers: Omit<ProviderReport, "instrumentor">[];
  stats: PipelineStats;
}

/**
 * Instruments every installed provider SDK (or those named in `only`). Safe
 * to call more than once: a provider that is already patched is left as it
 * is. Records are only produced once `observe()` has set a destination.
 */
export function auto(input: AutoOptions = {}): AutoStatus {
  const capture = options(input);
  const skip = new Set(input.skip ?? []);
  const only = input.only ? new Set(input.only) : null;
  for (const name of [...skip, ...(only ?? [])]) {
    if (!PROVIDER_NAMES.includes(name)) throw new Error(`Unknown provider: ${JSON.stringify(name)}. Supported: ${PROVIDER_NAMES.join(", ")}`);
  }
  const providers = state().providers;
  for (const spec of PROVIDERS) {
    if (skip.has(spec.name) || (only && !only.has(spec.name))) continue;
    const existing = providers.get(spec.name);
    if (existing && existing.state === "patched") continue;
    providers.set(spec.name, instrumentProvider(spec, capture));
  }
  return status();
}

/**
 * Restores what `auto()` patched (one provider, or all). A target that
 * someone else re-patched after `auto()` is left in place and listed under
 * `refusedUndo`; our wrapper inside it stops recording.
 */
export function undo(name?: string): AutoStatus {
  if (name !== undefined && !PROVIDER_NAMES.includes(name)) {
    throw new Error(`Unknown provider: ${JSON.stringify(name)}. Supported: ${PROVIDER_NAMES.join(", ")}`);
  }
  for (const [key, report] of state().providers) {
    if (name !== undefined && key !== name) continue;
    report.instrumentor?.undo();
  }
  return status();
}

/** What is patched, what was skipped and why, and the export counters. */
export function status(): AutoStatus {
  const pipeline = state().pipeline;
  const providers: Omit<ProviderReport, "instrumentor">[] = [];
  for (const spec of PROVIDERS) {
    const report = state().providers.get(spec.name);
    if (!report) {
      providers.push({
        name: spec.name,
        state: "not-checked",
        packageName: spec.packageName,
        sdkVersion: null,
        verifiedVersion: spec.verifiedVersion,
        patched: [],
        skipped: [],
        refusedUndo: [],
      });
      continue;
    }
    const { instrumentor: _ignored, ...plain } = report;
    providers.push({ ...plain, patched: [...plain.patched], skipped: [...plain.skipped], refusedUndo: [...plain.refusedUndo] });
  }
  return {
    observing: !!pipeline && !pipeline.closed,
    destination: pipeline && !pipeline.closed ? pipeline.exporter.describe() : null,
    providers,
    stats: exportStats(),
  };
}
