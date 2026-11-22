/**
 * esbuild plugin for applications that bundle a provider SDK.
 *
 *     import { build } from "esbuild";
 *     import { briefcaseAuto } from "@briefcase-ai/auto/esbuild";
 *     await build({ entryPoints: ["app.ts"], bundle: true, platform: "node", plugins: [briefcaseAuto()] });
 *
 * When an SDK is bundled, its classes live inside the bundle, where
 * `auto()` cannot find them. This plugin appends one statement to each SDK
 * file that holds patch targets (the same files and the same statement the
 * Node load hook uses, see `loader.ts`): at run time the bundled module
 * reports its exports to `@briefcase-ai/auto`, which patches them as it
 * patches an installed copy. The bundle still needs `auto()` to run, from
 * the `--import @briefcase-ai/auto/register` preload or from the
 * application (bundled or not). Reports made before `auto()` runs are kept
 * and replayed. Nothing else in the bundle changes.
 *
 * Alternative without the plugin: keep the SDKs out of the bundle
 * (`external: SDK_PACKAGES`) and ship them in node_modules.
 */

import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { basename, dirname } from "node:path";

import { WATCHED } from "./auto.js";
import { matchFile, notifierSource, setWatchedFiles } from "./loader.js";

/** The npm packages this package instruments (for `external: SDK_PACKAGES`). */
export const SDK_PACKAGES = [...new Set(WATCHED.map((f) => f.packageName))];

/** The subset of esbuild's plugin API this plugin uses (no type dependency on esbuild). */
export interface EsbuildPluginBuild {
  onLoad(
    options: { filter: RegExp; namespace?: string },
    callback: (args: { path: string }) => Promise<{ contents: string; loader: "js"; resolveDir: string } | undefined>,
  ): void;
}

export interface EsbuildPlugin {
  name: string;
  setup(build: EsbuildPluginBuild): void;
}

function escape(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\//g, "[\\\\/]");
}

export function briefcaseAuto(): EsbuildPlugin {
  return {
    name: "briefcase-auto",
    setup(build) {
      setWatchedFiles(WATCHED);
      // One id per build, so two bundles loaded into one process stay separate copies.
      const buildId = randomBytes(4).toString("hex");
      const filter = new RegExp(`[\\\\/]node_modules[\\\\/](?:${WATCHED.map((f) => `${escape(f.packageName)}[\\\\/]${escape(f.rel)}`).join("|")})$`);
      build.onLoad({ filter, namespace: "file" }, async (args) => {
        const hit = matchFile(args.path);
        if (!hit) return undefined;
        const text = await readFile(args.path, "utf8");
        // No build-machine path in the bundle: a per-build id and the path inside the package.
        const reported = `bundle:${buildId}/node_modules/${hit.file.packageName}/${hit.file.rel}`;
        const self = `./${basename(args.path)}`;
        return {
          contents: text + notifierSource(`bundle-${hit.file.format}`, reported, self),
          loader: "js",
          resolveDir: dirname(args.path),
        };
      });
    },
  };
}
