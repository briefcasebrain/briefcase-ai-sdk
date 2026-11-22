/**
 * `auto()`: instrument every installed provider SDK; `instrument()`,
 * `undo()` and `status()`.
 *
 * Same provider names and decision types as Python's `briefcase.auto()`.
 * A package that is not installed is skipped silently; one that is installed
 * but does not have the verified layout is skipped with one warning and is
 * never half-patched. Calling `auto()` again does not wrap anything twice.
 */

import { createRequire } from "node:module";

import * as anthropic from "./instrument/anthropic.js";
import * as bedrock from "./instrument/bedrock.js";
import {
  type CaptureOptions,
  type PatchTarget,
  type ProviderReport,
  type SdkFile,
  DEFAULT_OPTIONS,
  Instrumentor,
  WRAPPED,
  loadCopies,
  normalizeContentMode,
  packageVersion,
  requireFile,
  resolveCopies,
} from "./instrument/core.js";
import * as genai from "./instrument/google-genai.js";
import * as openai from "./instrument/openai.js";
import { installDetection } from "./detect.js";
import {
  type LoadedCopy,
  type WatchedFile,
  addKnownFile,
  installLoadHooks,
  report as reportModule,
  scanRequireCache,
  setCopyListener,
  setWatchedFiles,
} from "./loader.js";
import type { PipelineStats } from "./pipeline.js";
import { state } from "./state.js";
import { debug, describeError, moduleUrl, read, safe, warn } from "./util.js";
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
  /**
   * Load and patch every installed SDK copy (ESM and CommonJS) now, instead
   * of patching each copy when the application loads it. Costs startup
   * time; useful only to make `status()` final immediately.
   */
  eager?: boolean;
}

type TargetsFn = (
  inst: Instrumentor,
  modules: Record<string, Record<string, unknown> | undefined>,
  format: string,
) => PatchTarget[] | { targets: PatchTarget[]; verify: () => string | null };

interface ProviderSpec {
  name: string;
  packageName: string;
  verifiedVersion: string;
  specifiers: string[];
  files: SdkFile[];
  targets: TargetsFn;
}

export const PROVIDERS: ProviderSpec[] = [
  { name: openai.PROVIDER, packageName: openai.PACKAGE, verifiedVersion: openai.VERIFIED_VERSION, specifiers: openai.SPECIFIERS, files: openai.FILES, targets: openai.targets },
  { name: anthropic.PROVIDER, packageName: anthropic.PACKAGE, verifiedVersion: anthropic.VERIFIED_VERSION, specifiers: anthropic.SPECIFIERS, files: anthropic.FILES, targets: anthropic.targets },
  { name: genai.PROVIDER, packageName: genai.PACKAGE, verifiedVersion: genai.VERIFIED_VERSION, specifiers: genai.SPECIFIERS, files: genai.FILES, targets: genai.targets },
  { name: bedrock.PROVIDER, packageName: bedrock.PACKAGE, verifiedVersion: bedrock.VERIFIED_VERSION, specifiers: bedrock.SPECIFIERS, files: bedrock.FILES, targets: bedrock.targets },
];

export const PROVIDER_NAMES = PROVIDERS.map((p) => p.name);

export const WATCHED: WatchedFile[] = PROVIDERS.flatMap((spec) =>
  spec.files.map((file) => ({ ...file, provider: spec.name, packageName: spec.packageName })),
);

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

/** Test seam: replaces how SDK copies are found (and makes auto() load them up front). */
let copyLoader: typeof loadCopies | null = null;
export function setCopyLoaderForTests(loader: typeof loadCopies | null): void {
  copyLoader = loader;
}

const localRequire = createRequire(moduleUrl(import.meta.url));

function copyLabel(copy: LoadedCopy, inst: Instrumentor): string {
  let label: string = copy.format;
  if (copy.origin === "bundle") label = `bundle-${copy.format}`;
  else if (copy.origin === "client") label = `client-${copy.format}`;
  else if (copy.group !== "main" && copy.group !== "node") label = `${copy.format}:${copy.group}`;
  // A second install of the same SDK (another node_modules) gets its root in the label.
  const taken = [...inst.handledCopies].some((key) => key !== copy.key && key.startsWith(`${copy.origin}|${copy.format}|${copy.group}|`));
  return taken ? `${label}@${copy.root}` : label;
}

/** Instruments one module copy once all of its files have loaded. Never throws. */
function applyCopy(copy: LoadedCopy): void {
  safe(() => {
    const report = state().providers.get(copy.provider);
    const inst = report?.instrumentor;
    if (!report || !inst || !inst.active || inst.handledCopies.has(copy.key)) return;
    const spec = PROVIDERS.find((p) => p.name === copy.provider)!;
    const needed =
      copy.origin === "test" || copy.origin === "client"
        ? spec.specifiers
        : [...new Set(spec.files.filter((f) => f.format === copy.format && f.group === copy.group).map((f) => f.specifier))];
    if (!needed.every((specifier) => copy.modules[specifier])) return; // the rest of this copy has not loaded yet
    const label = copyLabel(copy, inst);
    inst.handledCopies.add(copy.key);
    if (copy.origin !== "client") report.sdkVersion ??= packageVersion(copy.file, spec.packageName);
    const built = spec.targets(inst, copy.modules, copy.format);
    const { targets, verify } = Array.isArray(built) ? { targets: built, verify: undefined } : built;
    inst.applyAll(label, targets, verify);
    report.state = report.patched.length > 0 ? "patched" : "skipped";
    debug(`${spec.name}: ${label} ${report.state} (${report.patched.length} target(s), sdk ${report.sdkVersion ?? "unknown"})`);
  }, "apply copy");
}

function ensureMachinery(): boolean {
  setWatchedFiles(WATCHED);
  setCopyListener(applyCopy);
  installDetection();
  return installLoadHooks();
}

/** Starts (or keeps) the instrumentor of one provider; returns null when one is already active. */
function enable(spec: ProviderSpec, capture: CaptureOptions): ProviderReport | null {
  const providers = state().providers;
  const existing = providers.get(spec.name);
  if (existing?.instrumentor?.active && existing.state !== "skipped") return null;
  const inst = new Instrumentor(spec.name, capture, spec.packageName, spec.verifiedVersion);
  const report = inst.report;
  report.state = "not-installed";
  for (const specifier of copyLoader ? [] : spec.specifiers) {
    const paths = safe(() => resolveCopies(specifier), "resolve") ?? null;
    if (!paths) continue;
    report.state = "waiting";
    for (const path of [paths.esm, paths.cjs]) {
      if (!path) continue;
      report.sdkVersion ??= packageVersion(path, spec.packageName);
      const file = spec.files.find((f) => f.specifier === specifier && f.format === (path === paths.esm ? "esm" : "cjs"));
      if (file) addKnownFile(path, { ...file, provider: spec.name, packageName: spec.packageName });
    }
  }
  providers.set(spec.name, report);
  return report;
}

/** Loads what is already loaded (or, eager, everything installed) so it reports itself. */
function catchUp(spec: ProviderSpec, eager: boolean, hooksAtStartup: boolean): void {
  if (copyLoader) {
    const report = state().providers.get(spec.name)!;
    for (const specifier of spec.specifiers) {
      const found = copyLoader(specifier);
      if (!found) continue;
      if (report.state === "not-installed") report.state = "waiting";
      for (const problem of found.problems) report.skipped.push({ target: specifier, reason: problem });
      for (const copy of found.copies) {
        const file = { specifier, rel: "", format: copy.format, group: "main", provider: spec.name, packageName: spec.packageName };
        reportModule(copy.format, copy.path, copy.ns, { file, root: `test:${copy.path.slice(0, copy.path.lastIndexOf("/"))}` });
      }
    }
    return;
  }
  for (const specifier of spec.specifiers) {
    const paths = safe(() => resolveCopies(specifier), "resolve") ?? null;
    if (!paths) continue;
    // Node has no API that says whether an ES module is already loaded. Unless the load hook
    // has watched every load since startup (the preload), ask for the ESM copy: require(esm)
    // returns the loaded instance when there is one, and loads it otherwise.
    const wanted: ["esm" | "cjs", string | null][] = eager
      ? [["esm", paths.esm], ["cjs", paths.cjs]]
      : hooksAtStartup
        ? []
        : [["esm", paths.esm]];
    for (const [format, path] of wanted) {
      if (!path) continue;
      try {
        reportModule(format, path, requireFile(path));
      } catch (error) {
        const report = state().providers.get(spec.name)!;
        report.skipped.push({ target: specifier, reason: `${format} copy could not be loaded (${describeError(error)})` });
      }
    }
  }
}

/**
 * Instruments every installed provider SDK (or those named in `only`).
 * Each SDK copy is patched when the application loads it (or now, if it
 * is already loaded); `eager: true` loads and patches every installed copy
 * immediately. Safe to call more than once: a provider that is already
 * instrumented is left as it is. Records are only produced once
 * `observe()` has set a destination.
 */
export function auto(input: AutoOptions = {}): AutoStatus {
  const capture = options(input);
  const skip = new Set(input.skip ?? []);
  const only = input.only ? new Set(input.only) : null;
  for (const name of [...skip, ...(only ?? [])]) {
    if (!PROVIDER_NAMES.includes(name)) throw new Error(`Unknown provider: ${JSON.stringify(name)}. Supported: ${PROVIDER_NAMES.join(", ")}`);
  }
  lastCapture = capture;
  const hooked = ensureMachinery();
  const s = state();
  const enabled: ProviderSpec[] = [];
  for (const spec of PROVIDERS) {
    if (skip.has(spec.name) || (only && !only.has(spec.name))) continue;
    if (enable(spec, capture)) enabled.push(spec);
  }
  if (enabled.length > 0) {
    if (!copyLoader) safe(() => scanRequireCache(localRequire.cache as Record<string, { loaded?: boolean; exports?: unknown }>), "require.cache scan");
    for (const spec of enabled) catchUp(spec, !!input.eager || !hooked, s.hooksAtStartup && hooked);
    for (const copy of [...s.copies.values()]) if (!copyLoader || copy.origin === "test") applyCopy(copy);
  }
  return status();
}

/** Marks the load hook as installed before any application module (called by the preload). */
export function markStartup(): void {
  const s = state();
  if (s.loadHooks === "none") {
    s.hooksAtStartup = ensureMachinery();
  }
}

let lastCapture: CaptureOptions | null = null;
let warnedTarget = false;

/** Protos and classes found on a client / class, as module namespaces for a provider's `targets()`. */
function describeTarget(target: unknown): { provider: string; modules: Record<string, Record<string, unknown>>; probe: [object, string] } | null {
  const proto = (o: unknown): object | null => (o && typeof o === "object" ? Object.getPrototypeOf(o) : null);
  const ns = (className: string, prototype: unknown) => ({ [className]: { prototype } });
  const isFn = (v: unknown): boolean => typeof v === "function";
  const t = target as Record<string, unknown>;
  if (!t || (typeof t !== "object" && typeof t !== "function")) return null;

  // openai: an OpenAI client, or the OpenAI class (its static resource classes).
  const oc = typeof t === "function"
    ? [read(t, "Chat", "Completions"), read(t, "Responses"), read(t, "Completions"), read(t, "Embeddings")].map((c) => read(c, "prototype"))
    : [read(t, "chat", "completions"), read(t, "responses"), read(t, "completions"), read(t, "embeddings")].map(proto);
  if (oc.every((p) => p && isFn(read(p, "create")))) {
    const modules: Record<string, Record<string, unknown>> = {};
    openai.SURFACES.forEach((surface, i) => (modules[surface.specifier] = ns(surface.className, oc[i])));
    return { provider: openai.PROVIDER, modules, probe: [oc[0]!, "create"] };
  }
  // @anthropic-ai/sdk: an Anthropic client, or the Anthropic class.
  const ac = typeof t === "function"
    ? [read(t, "Messages", "prototype"), read(t, "Beta", "Messages", "prototype")]
    : [proto(read(t, "messages")), proto(read(t, "beta", "messages"))];
  if (ac.every((p) => p && isFn(read(p, "create")))) {
    return {
      provider: anthropic.PROVIDER,
      modules: { [anthropic.SPECIFIERS[0]!]: ns("Messages", ac[0]), [anthropic.SPECIFIERS[1]!]: ns("Messages", ac[1]) },
      probe: [ac[0]!, "create"],
    };
  }
  // @google/genai: a GoogleGenAI client, a Models instance, or the Models class.
  const models = typeof t === "function" ? read(t, "prototype") : isFn(read(t, "generateContentInternal")) ? proto(t) : proto(read(t, "models"));
  if (models && isFn(read(models, "generateContentInternal")) && isFn(read(models, "constructor"))) {
    return { provider: genai.PROVIDER, modules: { [genai.SPECIFIERS[0]!]: { Models: read(models, "constructor") } }, probe: [models, "generateContentInternal"] };
  }
  // Bedrock Runtime: a BedrockRuntimeClient / BedrockRuntime client, or one of those classes.
  const instance = typeof t === "function" ? null : t;
  const service = read(instance, "config", "serviceId");
  let p: object | null = typeof t === "function" ? (read(t, "prototype") as object | null) : proto(t);
  const isBedrock = typeof t === "function" ? /^BedrockRuntime(Client)?$/.test(String(read(t, "name"))) : service === "Bedrock Runtime";
  if (isBedrock && p) {
    // The prototype just below the one that owns `send` (smithy's Client): BedrockRuntimeClient.prototype.
    while (p && !Object.prototype.hasOwnProperty.call(Object.getPrototypeOf(p) ?? {}, "send")) p = Object.getPrototypeOf(p);
    if (p && isFn(read(p, "send"))) {
      return { provider: bedrock.PROVIDER, modules: { [bedrock.SPECIFIERS[0]!]: ns("BedrockRuntimeClient", p) }, probe: [p, "send"] };
    }
  }
  return null;
}

const clientCopyIds = new WeakMap<object, string>();
let nextClientCopy = 0;

/**
 * Instruments the SDK copy a client (or client class) belongs to, by its
 * prototypes: for an SDK bundled into the application, or any copy
 * `auto()` cannot reach. Every client of that copy, created before or
 * after, is covered. Returns `target` unchanged. Never throws: an object
 * that is not a supported client is reported once on stderr.
 *
 * Supported: `new OpenAI()` / `OpenAI`, `new Anthropic()` / `Anthropic`,
 * `new GoogleGenAI()` (or its `.models`, or `Models`), and
 * `new BedrockRuntimeClient()` / `new BedrockRuntime()` / either class.
 */
export function instrument<T>(target: T, input: AutoOptions = {}): T {
  safe(() => {
    const found = describeTarget(target);
    if (!found) {
      if (!warnedTarget) {
        warnedTarget = true;
        warn("instrument(): not a client or client class of a supported SDK (openai, @anthropic-ai/sdk, @google/genai, @aws-sdk/client-bedrock-runtime); nothing was patched.");
      }
      return;
    }
    const [probeOwner, probeKey] = found.probe;
    const current = Object.getOwnPropertyDescriptor(probeOwner, probeKey)?.value as Record<symbol, unknown> | undefined;
    const report = state().providers.get(found.provider);
    if (current?.[WRAPPED] && report?.instrumentor?.active) return; // this copy is already instrumented
    ensureMachinery();
    const spec = PROVIDERS.find((p) => p.name === found.provider)!;
    const hasOptions = Object.keys(input).length > 0;
    enable(spec, hasOptions || !lastCapture ? options(input) : lastCapture);
    let id = clientCopyIds.get(probeOwner);
    if (!id) {
      id = `client|esm|main|#${++nextClientCopy}`;
      clientCopyIds.set(probeOwner, id);
    }
    applyCopy({
      key: id,
      provider: found.provider,
      packageName: spec.packageName,
      format: "esm",
      group: "main",
      origin: "client",
      root: id,
      file: "",
      modules: found.modules,
    });
  }, "instrument");
  return target;
}

export interface AutoStatus {
  observing: boolean;
  destination: string | null;
  providers: Omit<ProviderReport, "instrumentor">[];
  stats: PipelineStats;
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
        unrecordedRequests: 0,
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
