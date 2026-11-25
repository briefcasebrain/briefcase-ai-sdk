/**
 * Lazy patching: each SDK copy is patched when the application loads it,
 * not up front.
 *
 * - A synchronous, in-thread load hook (`module.registerHooks`, Node >= 26
 *   here) watches for the few SDK files that hold patch targets (each
 *   provider's `FILES`). For those files only, it appends one statement to
 *   the source Node is about to evaluate: it reports the module's own
 *   exports (`module.exports` for CommonJS, the module namespace for ESM,
 *   through a self-import) to a function this package keeps on
 *   `globalThis`. The statement runs after the module body, so every class
 *   is defined; nothing else in the file changes. Every other module goes
 *   through untouched (`nextLoad`'s result is returned as is).
 * - The same statement is what the esbuild plugin (`esbuild.ts`) appends
 *   when it bundles an SDK into an application, so bundled copies report
 *   themselves the same way.
 * - Reports that arrive before anything is listening are queued in an array
 *   on that `globalThis` key and replayed when `auto()` installs the
 *   listener.
 *
 * Copies are grouped per package root, format and build (`SdkFile.group`);
 * a provider is applied to a copy once all of its files for that copy have
 * reported (so the all-or-none rule of `Instrumentor.applyAll` still
 * holds per copy).
 */

import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";

import type { SdkFile } from "./instrument/core.js";
import { state } from "./state.js";
import { debug, describeError, safe, warn } from "./util.js";

export const LOADED_KEY = Symbol.for("briefcase-ai.auto.loaded.v1");

export type Origin = "node" | "bundle" | "test" | "client";

export interface WatchedFile extends SdkFile {
  provider: string;
  packageName: string;
}

export interface LoadedCopy {
  key: string;
  provider: string;
  packageName: string;
  format: "esm" | "cjs";
  group: string;
  origin: Origin;
  root: string;
  /** One file of the copy, for version lookup. */
  file: string;
  modules: Record<string, Record<string, unknown>>;
}

type Listener = (copy: LoadedCopy) => void;

let watched: WatchedFile[] = [];
let listener: Listener | null = null;

/** Sets the SDK files to watch (every provider's, whether or not it is enabled). */
export function setWatchedFiles(files: WatchedFile[]): void {
  watched = files;
  quick = null;
}

/** Called for each copy whenever one of its files reports. */
export function setCopyListener(fn: Listener | null): void {
  listener = fn;
}

const REPORT =
  'function(f,p,n){try{var g=globalThis,k=Symbol.for("briefcase-ai.auto.loaded.v1"),h=g[k];' +
  'if(typeof h==="function")h(f,p,n);else(g[k]=Array.isArray(h)?h:[]).push([f,p,n])}catch(e){}}';

/**
 * The statement appended to a watched SDK file. `tag` is "esm" / "cjs"
 * (loaded by Node) or "bundle-esm" / "bundle-cjs" (inlined by a bundler).
 * `self` is the specifier an ES module uses to import its own namespace.
 */
export function notifierSource(tag: string, file: string, self?: string): string {
  const exportsExpr = tag.endsWith("esm") ? "__briefcase_auto_self" : "module.exports";
  const importLine = tag.endsWith("esm") ? `import * as __briefcase_auto_self from ${JSON.stringify(self ?? file)};\n` : "";
  return `\n;${importLine}(${REPORT})(${JSON.stringify(tag)}, ${JSON.stringify(file)}, ${exportsExpr});\n`;
}

function normalize(path: string): string {
  return path.replace(/\\/g, "/");
}

const known = new Map<string, { file: WatchedFile; root: string }>();

/** A file resolved from this package's location (also matched when it is not under node_modules, e.g. a linked SDK). */
export function addKnownFile(path: string, file: WatchedFile): void {
  const p = normalize(path);
  const root = p.endsWith(`/${file.rel}`) ? p.slice(0, p.length - file.rel.length - 1) : p.slice(0, p.lastIndexOf("/"));
  known.set(p, { file, root });
}

/** The watched entry a file path is, and the package root it sits in. */
export function matchFile(path: string): { file: WatchedFile; root: string } | null {
  const p = normalize(path);
  const exact = known.get(p);
  if (exact) return exact;
  for (const file of watched) {
    const suffix = `/node_modules/${file.packageName}/${file.rel}`;
    if (p.endsWith(suffix)) return { file, root: p.slice(0, p.length - file.rel.length - 1) };
  }
  return null;
}

/** A quick filter on the last path segment before the full match. */
let quick: RegExp | null = null;
function quickFilter(): RegExp {
  if (!quick) {
    const names = [...new Set(watched.map((f) => f.rel.split("/").pop()!.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))];
    quick = new RegExp(`/(?:${names.join("|")})$`);
  }
  return quick;
}

/** Records one reported module and hands its copy to the listener. */
export function report(tag: string, path: string, ns: unknown, explicit?: { file: WatchedFile; root: string }): void {
  safe(() => {
    if (!ns || (typeof ns !== "object" && typeof ns !== "function")) return;
    const bundled = tag.startsWith("bundle-");
    const format = (bundled ? tag.slice(7) : tag) as "esm" | "cjs";
    const hit = explicit ?? matchFile(path);
    if (!hit || hit.file.format !== format) return;
    const origin: Origin = explicit ? "test" : bundled ? "bundle" : "node";
    const key = `${origin}|${format}|${hit.file.group}|${hit.root}`;
    const copies = state().copies;
    let copy = copies.get(key);
    if (!copy) {
      copy = {
        key,
        provider: hit.file.provider,
        packageName: hit.file.packageName,
        format,
        group: hit.file.group,
        origin,
        root: hit.root,
        file: path,
        modules: {},
      };
      copies.set(key, copy);
    }
    copy.modules[hit.file.specifier] = ns as Record<string, unknown>;
    debug(`loaded ${hit.file.packageName}/${hit.file.rel} (${origin} ${format})`);
    listener?.(copy);
  }, "module report");
}

function onReport(tag: unknown, path: unknown, ns: unknown): void {
  if (typeof tag === "string" && typeof path === "string") report(tag, path, ns);
}

/** Installs the `globalThis` listener and replays reports queued before it. */
function installListener(): void {
  const holder = globalThis as unknown as Record<symbol, unknown>;
  const current = holder[LOADED_KEY];
  if (current === onReport) return;
  if (typeof current === "function") {
    // Another copy of this package (bundled next to a preloaded one) already listens; it shares our state.
    return;
  }
  const queued = Array.isArray(current) ? (current as unknown[][]) : [];
  holder[LOADED_KEY] = onReport;
  for (const entry of queued) onReport(entry[0], entry[1], entry[2]);
}

function sourceText(source: unknown): string | null {
  if (typeof source === "string") return source;
  if (source instanceof Uint8Array) return Buffer.from(source.buffer, source.byteOffset, source.byteLength).toString("utf8");
  if (source instanceof ArrayBuffer) return Buffer.from(source).toString("utf8");
  return null;
}

/**
 * Installs the listener and the load hook once per thread. Returns false
 * when the hook could not be registered (the caller then falls back to
 * loading the SDK copies up front).
 */
export function installLoadHooks(): boolean {
  installListener();
  const s = state();
  if (s.loadHooks !== "none") return s.loadHooks === "installed";
  try {
    registerHooks({
      load(url, context, nextLoad) {
        const result = nextLoad(url, context);
        try {
          if (!url.startsWith("file:")) return result;
          const path = fileURLToPath(url);
          if (!quickFilter().test(normalize(path)) && !known.has(normalize(path))) return result;
          const hit = matchFile(path);
          // A plain require() of a .js file can come back without a format: it is CommonJS then.
          const declared = result.format ?? (path.endsWith(".mjs") ? "module" : "commonjs");
          const format = declared === "module" ? "esm" : declared === "commonjs" ? "cjs" : null;
          if (!hit || hit.file.format !== format) return result;
          const text = sourceText(result.source);
          if (text === null) return result;
          return { ...result, source: text + notifierSource(format, path, url) };
        } catch (error) {
          debug(`load hook skipped ${url}: ${describeError(error)}`);
          return result;
        }
      },
    });
    s.loadHooks = "installed";
    return true;
  } catch (error) {
    s.loadHooks = "failed";
    warn(`could not install the module load hook (${describeError(error)}); SDKs are loaded up front instead.`);
    return false;
  }
}

/** Reports CommonJS (and require(esm)) SDK files already in `require.cache`. */
export function scanRequireCache(cache: Record<string, { loaded?: boolean; exports?: unknown } | undefined>): void {
  for (const [path, mod] of Object.entries(cache)) {
    if (!mod || !mod.loaded || !quickFilter().test(normalize(path))) continue;
    const hit = matchFile(path);
    if (hit) report(hit.file.format, path, mod.exports);
  }
}
