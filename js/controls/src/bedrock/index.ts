/**
 * Bedrock-specific helpers, published on the "./bedrock" subpath only:
 * model-id resolution and capability probes, model-id normalization,
 * list-price lookup, and a memoized credential provider over the optional
 * @aws-sdk/credential-providers peer dependency.
 */

export {
  BEDROCK_DEFAULT_RATE,
  BEDROCK_RATES,
  BEDROCK_RATE_PER_TOKENS,
  priceForUsage,
  type BedrockRate,
} from "./pricing.js";

export interface ResolveModelIdArgs {
  /** Highest-precedence override, typically a per-feature environment knob. */
  featureOverride?: string;
  /** Canonical operator override, e.g. process.env.BEDROCK_MODEL_ID. */
  envOverride?: string;
  /** Stage default inference profile id, e.g. process.env.AWS_INFERENCE_PROFILE_PREFIX. */
  profilePrefix?: string;
  /** Bare in-account last resort. */
  fallback: string;
}

/**
 * Resolves the Bedrock model id / inference-profile id to invoke. Candidates
 * win in order: featureOverride, envOverride, profilePrefix, fallback. Uses
 * `||` so an empty-string value falls through to the next candidate.
 */
export function resolveModelId(args: ResolveModelIdArgs): string {
  return args.featureOverride || args.envOverride || args.profilePrefix || args.fallback;
}

/**
 * Whether a model id or inference-profile ARN supports Bedrock Converse tool
 * use in streaming mode. Tool-streaming-capable families: Anthropic Claude,
 * Amazon Nova, Cohere Command R/R+, Mistral Large. Application Inference
 * Profile ARNs are trusted as operator configuration.
 */
export function supportsConverseTools(modelIdOrArn: string): boolean {
  const isAppInferenceProfileArn = modelIdOrArn.includes(":application-inference-profile/");
  const isToolCapableModelId =
    /^(us\.|global\.)?(anthropic\.|cohere\.command-r|mistral\.mistral-large|amazon\.nova-)/.test(
      modelIdOrArn,
    );
  return isAppInferenceProfileArn || isToolCapableModelId;
}

/**
 * Whether a model id is documented to support Bedrock's per-request Priority
 * service tier. Conservative: Bedrock rejects unsupported tiers instead of
 * falling back to Standard, and opaque Application Inference Profile ARNs
 * cannot reveal their backing model.
 */
export function supportsPriorityServiceTier(modelId: string): boolean {
  return /(?:^|\/)(?:us\.|global\.)?amazon\.nova-(?:pro|premier)(?:[-.:]|$)/.test(modelId);
}

/**
 * Strips region prefixes (us./eu./apac./global.), vendor prefixes
 * (anthropic./amazon./cohere./mistral./meta.), a version-revision suffix
 * (-v1:0 or :0), and a trailing 8-digit date stamp, leaving the bare model
 * family name for grouping and pricing.
 */
export function normalizeBedrockModelId(id: string): string {
  return id
    .replace(/^(us|eu|apac|global)\./, "")
    .replace(/^(anthropic|amazon|cohere|mistral|meta)\./, "")
    .replace(/-v\d+:\d+$/, "")
    .replace(/:\d+$/, "")
    .replace(/-\d{8}$/, "");
}

interface CredentialProvidersModule {
  fromNodeProviderChain(): unknown;
}

// The provider promise is memoized module-wide: the chain is built once
// and caches resolved credentials internally.
let providerPromise: Promise<unknown> | undefined;

const defaultImporter = (): Promise<CredentialProvidersModule> =>
  import("@aws-sdk/credential-providers");

/**
 * Returns, synchronously, a credential provider in the AWS SDK's
 * AwsCredentialIdentityProvider shape: a function that yields credentials
 * when invoked. The lazy import and chain construction happen inside the
 * returned function on first fetch, sharing the module-wide memo, so this
 * wires directly into clients whose config takes a provider function.
 */
export function createCredentialProvider(options?: {
  importer?: () => Promise<CredentialProvidersModule>;
}): () => Promise<unknown> {
  const getProvider = createMemoizedCredentialProvider(options);
  return async () => {
    const provider = (await getProvider()) as () => Promise<unknown>;
    return provider();
  };
}

/**
 * Returns a getter for a memoized AWS credential provider chain. The memo is
 * module-wide: every getter from every call shares one cached promise.
 * @aws-sdk/credential-providers is imported lazily on first use; it is an
 * optional peer dependency, and the getter rejects with an install hint when
 * it is absent. Any failure clears the memo, so a later call retries.
 * `importer` is a test seam over the dynamic import.
 */
export function createMemoizedCredentialProvider(options?: {
  importer?: () => Promise<CredentialProvidersModule>;
}): () => Promise<unknown> {
  const importer = options?.importer ?? defaultImporter;
  return async () => {
    if (providerPromise === undefined) {
      const attempt: Promise<unknown> = (async () => {
        let module: CredentialProvidersModule;
        try {
          module = await importer();
        } catch (err) {
          throw new Error(
            "createMemoizedCredentialProvider requires the optional peer dependency @aws-sdk/credential-providers",
            { cause: err },
          );
        }
        return module.fromNodeProviderChain();
      })();
      providerPromise = attempt;
      attempt.catch(() => {
        // Any failure drops the memo so the next call retries; the guard
        // keeps a reset or newer attempt from being clobbered.
        if (providerPromise === attempt) providerPromise = undefined;
      });
    }
    return providerPromise;
  };
}

/** Test hook: drops the memoized credential provider. */
export function __resetForTests(): void {
  providerPromise = undefined;
}
