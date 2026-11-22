/**
 * @briefcase-ai/auto: zero-code decision capture for Node.
 *
 *     import { observe, auto } from "@briefcase-ai/auto";
 *     observe();   // destination from BRIEFCASE_EXPORTER / OTEL_EXPORTER_OTLP_*, else stderr
 *     auto();      // openai, @anthropic-ai/sdk, @google/genai, @aws-sdk/client-bedrock-runtime
 *
 * or, without editing the program: `node --import @briefcase-ai/auto/register app.js`.
 */

export { auto, undo, status, PROVIDER_NAMES, type AutoOptions, type AutoStatus } from "./auto.js";
export {
  observe,
  flush,
  shutdown,
  exportStats,
  exporterFromEnv,
  resolveExporter,
  type ObserveOptions,
} from "./observe.js";
export {
  ConsoleExporter,
  HttpExporter,
  JsonlFileExporter,
  MemoryExporter,
  OtlpHttpExporter,
  type Exporter,
  type ExportResult,
  type HttpExporterOptions,
  type OtlpExporterOptions,
} from "./exporters.js";
export { withWorkflow, currentWorkflow, type WorkflowContext } from "./context.js";
export { SCHEMA_VERSION, type DecisionRecord, type TokenUsage } from "./record.js";
export type { PipelineStats } from "./pipeline.js";
export type { ProviderReport } from "./instrument/core.js";
