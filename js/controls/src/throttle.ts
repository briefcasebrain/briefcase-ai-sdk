/**
 * Provider-throttle classification for AWS SDK v3 style errors. Recognizes
 * throttle names, Smithy `__type` markers, `$metadata` and attached-response
 * status codes, and traverses `cause` chains. 503s and
 * ServiceQuotaExceededException classify as transient only: retry with
 * backoff, never a quota cooldown. The message-text regex is opt-in because
 * it can misfire on unrelated errors that merely mention rate limits.
 */

export interface ThrottleClassification {
  /** The provider rejected for rate; cooldown-worthy. */
  throttled: boolean;
  /** Worth retrying with backoff. Every throttle is transient. */
  transient: boolean;
}

const THROTTLED_NAMES = new Set(["ThrottlingException", "TooManyRequestsException"]);
const TRANSIENT_ONLY_NAMES = new Set(["ServiceQuotaExceededException"]);
const MESSAGE_REGEX = /throttl|rate.?limit/i;
const MAX_CAUSE_DEPTH = 8;

interface ErrorShape {
  name?: unknown;
  code?: unknown;
  message?: unknown;
  __type?: unknown;
  $metadata?: { httpStatusCode?: unknown };
  response?: { status?: unknown; status_code?: unknown };
  cause?: unknown;
  constructor?: { name?: string };
}

function names(err: ErrorShape): string[] {
  const out: string[] = [];
  if (typeof err.name === "string") out.push(err.name);
  const ctor = err.constructor?.name;
  if (typeof ctor === "string") out.push(ctor);
  return out;
}

function statusCode(err: ErrorShape): number | null {
  const meta = err.$metadata?.httpStatusCode;
  if (typeof meta === "number") return meta;
  const response = err.response;
  if (typeof response === "object" && response !== null) {
    if (typeof response.status === "number") return response.status;
    if (typeof response.status_code === "number") return response.status_code;
  }
  return null;
}

function classifySingle(err: unknown, messageRegex: boolean): ThrottleClassification {
  if (typeof err !== "object" || err === null) {
    const throttled = messageRegex && MESSAGE_REGEX.test(String(err));
    return { throttled, transient: throttled };
  }
  const e = err as ErrorShape;
  const errNames = names(e);
  const status = statusCode(e);

  let throttled =
    errNames.some((n) => THROTTLED_NAMES.has(n)) ||
    (typeof e.__type === "string" && e.__type.includes("ThrottlingException")) ||
    status === 429;

  const transientOnly =
    errNames.some((n) => TRANSIENT_ONLY_NAMES.has(n)) ||
    (typeof e.code === "string" && TRANSIENT_ONLY_NAMES.has(e.code)) ||
    status === 503;

  if (!throttled && messageRegex && typeof e.message === "string") {
    throttled = MESSAGE_REGEX.test(e.message);
  }

  return { throttled, transient: throttled || transientOnly };
}

/**
 * Classifies a provider error, traversing `cause` chains with a depth cap and
 * cycle guard. `messageRegex` additionally matches throttle wording in the
 * message text; off by default because a match opens quota cooldowns in the
 * gateway, so enable it only where wrapped errors hide their type.
 */
export function classifyProviderError(
  err: unknown,
  options: { messageRegex?: boolean } = {},
): ThrottleClassification {
  const messageRegex = options.messageRegex ?? false;
  const seen = new Set<unknown>();
  let current: unknown = err;
  let depth = 0;
  const combined: ThrottleClassification = { throttled: false, transient: false };
  while (current !== null && current !== undefined && depth < MAX_CAUSE_DEPTH && !seen.has(current)) {
    seen.add(current);
    const single = classifySingle(current, messageRegex);
    combined.throttled = combined.throttled || single.throttled;
    combined.transient = combined.transient || single.transient;
    if (combined.throttled) break;
    current = typeof current === "object" ? (current as ErrorShape).cause : undefined;
    depth += 1;
  }
  return combined;
}
