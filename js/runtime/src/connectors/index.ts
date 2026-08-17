import {
  createCipheriv,
  createDecipheriv,
  createHash,
  randomBytes as nodeRandomBytes,
  timingSafeEqual,
} from "node:crypto";

export type MaybePromise<T> = T | Promise<T>;

export interface ConnectorConnection {
  id: string;
  tenantId: string;
  provider: string;
  accessToken: string;
  refreshToken: string | null;
  tokenExpiresAt: Date | null;
  refreshVersion: string | number | null;
  refreshedAt: Date | null;
}

export interface ConnectorResource {
  externalId: string;
  occurredAt: Date;
}

export interface RefreshedTokens {
  accessToken: string;
  refreshToken?: string | null;
  expiresInSeconds?: number | null;
}

export interface ConnectorStrategy<R extends ConnectorResource, C extends ConnectorConnection = ConnectorConnection> {
  provider: string;
  refreshAccessToken?(context: { tenantId: string; connection: C }): Promise<RefreshedTokens | null>;
  fetchResources(context: { tenantId: string; connection: C; accessToken: string; cursor: string | null }): Promise<R[]>;
  revoke?(context: { tenantId: string; connection: C }): Promise<void>;
}

export interface ConnectorStore<C extends ConnectorConnection = ConnectorConnection> {
  beginRun(args: { tenantId: string; connectionId: string; provider: string; startedAt: Date }): Promise<string>;
  loadConnection(args: { tenantId: string; connectionId: string; provider: string }): Promise<C | null>;
  latestCursor(args: { tenantId: string; connectionId: string; provider: string }): Promise<string | null>;
  compareAndSwapTokens(args: {
    tenantId: string;
    connectionId: string;
    provider: string;
    expectedRefreshVersion: string | number | null;
    tokens: { accessToken: string; refreshToken?: string | null; expiresAt?: Date | null };
    refreshedAt: Date;
  }): Promise<boolean>;
  completeRun(args: { runId: string; finishedAt: Date; fetched: number; written: number; cursor: string }): Promise<void>;
  failRun(args: { runId: string; finishedAt: Date; error: string }): Promise<void>;
  markNeedsReconnect(args: { tenantId: string; connectionId: string; provider: string; reason: string; at: Date }): Promise<void>;
}

export interface ResourceSink<R extends ConnectorResource> {
  write(resources: readonly R[], context: { tenantId: string; connectionId: string; provider: string; runId: string }): Promise<{ written: number }>;
}

export type RefreshErrorKind = "terminal" | "transient";

export class ConnectorRefreshError extends Error {
  constructor(message: string, readonly kind: RefreshErrorKind, options?: ErrorOptions) {
    super(message, options);
    this.name = "ConnectorRefreshError";
  }
}

export interface RunConnectorSyncOptions<R extends ConnectorResource, C extends ConnectorConnection = ConnectorConnection> {
  tenantId: string;
  connectionId: string;
  provider: string;
  store: ConnectorStore<C>;
  strategy: ConnectorStrategy<R, C>;
  sink: ResourceSink<R>;
  now?: () => Date;
  tokenExpirySkewMs?: number;
  refreshFastPathMs?: number;
}

function nextCursor(
  resources: readonly ConnectorResource[],
  runStartMs: number,
  priorCursor: string | null,
): string {
  const parsedPrior = priorCursor === null ? Number.NaN : Date.parse(priorCursor);
  let max = Number.isFinite(parsedPrior) && parsedPrior <= runStartMs ? parsedPrior : 0;
  for (const resource of resources) {
    const value = resource.occurredAt.getTime();
    if (Number.isFinite(value) && value <= runStartMs && value > max) max = value;
  }
  if (max > 0) return new Date(max).toISOString();
  return priorCursor ?? new Date(runStartMs).toISOString();
}

export async function runConnectorSync<R extends ConnectorResource, C extends ConnectorConnection = ConnectorConnection>(
  options: RunConnectorSyncOptions<R, C>,
): Promise<{ runId: string; fetched: number; written: number }> {
  if (options.strategy.provider !== options.provider) {
    throw new Error(`connector strategy ${options.strategy.provider} cannot run provider ${options.provider}`);
  }
  const now = options.now ?? (() => new Date());
  const startedAt = now();
  const runId = await options.store.beginRun({
    tenantId: options.tenantId,
    connectionId: options.connectionId,
    provider: options.provider,
    startedAt,
  });
  try {
    const loadArgs = {
      tenantId: options.tenantId,
      connectionId: options.connectionId,
      provider: options.provider,
    };
    let connection = await options.store.loadConnection(loadArgs);
    if (!connection) throw new Error(`${options.provider} connection not found`);
    const cursor = await options.store.latestCursor(loadArgs);
    let accessToken = connection.accessToken;

    if (options.strategy.refreshAccessToken && connection.refreshToken) {
      const at = now();
      const skew = options.tokenExpirySkewMs ?? 5 * 60_000;
      const fastPath = options.refreshFastPathMs ?? 60_000;
      const tokenFresh = connection.tokenExpiresAt !== null && connection.tokenExpiresAt.getTime() - at.getTime() > skew;
      const recentlyRefreshed = connection.refreshedAt !== null && at.getTime() - connection.refreshedAt.getTime() < fastPath;
      if (!tokenFresh && !recentlyRefreshed) {
        const expectedRefreshVersion = connection.refreshVersion;
        try {
          const refreshed = await options.strategy.refreshAccessToken({ tenantId: options.tenantId, connection });
          if (refreshed) {
            const refreshedAt = now();
            const expiresAt = refreshed.expiresInSeconds == null
              ? null
              : new Date(refreshedAt.getTime() + refreshed.expiresInSeconds * 1000);
            const won = await options.store.compareAndSwapTokens({
              ...loadArgs,
              expectedRefreshVersion,
              tokens: {
                accessToken: refreshed.accessToken,
                ...(refreshed.refreshToken !== undefined ? { refreshToken: refreshed.refreshToken } : {}),
                expiresAt,
              },
              refreshedAt,
            });
            if (won) {
              accessToken = refreshed.accessToken;
              connection = {
                ...connection,
                accessToken,
                refreshToken: refreshed.refreshToken ?? connection.refreshToken,
                tokenExpiresAt: expiresAt,
                refreshedAt,
              };
            } else {
              const winner = await options.store.loadConnection(loadArgs);
              if (!winner) throw new Error(`${options.provider} connection disappeared during refresh`);
              connection = winner;
              accessToken = winner.accessToken;
            }
          }
        } catch (error) {
          if (error instanceof ConnectorRefreshError && error.kind === "terminal") {
            const winner = await options.store.loadConnection(loadArgs);
            if (winner && winner.refreshVersion !== expectedRefreshVersion) {
              connection = winner;
              accessToken = winner.accessToken;
            } else {
              await options.store.markNeedsReconnect({ ...loadArgs, reason: error.message, at: now() });
              throw error;
            }
          } else {
            throw error;
          }
        }
      }
    }

    const resources = await options.strategy.fetchResources({
      tenantId: options.tenantId,
      connection,
      accessToken,
      cursor,
    });
    const sinkResult = await options.sink.write(resources, {
      tenantId: options.tenantId,
      connectionId: options.connectionId,
      provider: options.provider,
      runId,
    });
    const finishedAt = now();
    await options.store.completeRun({
      runId,
      finishedAt,
      fetched: resources.length,
      written: sinkResult.written,
      cursor: nextCursor(resources, startedAt.getTime(), cursor),
    });
    return { runId, fetched: resources.length, written: sinkResult.written };
  } catch (error) {
    await options.store.failRun({
      runId,
      finishedAt: (options.now ?? (() => new Date()))(),
      error: error instanceof Error ? error.message : String(error),
    });
    throw error;
  }
}

export type OAuthVerifyResult =
  | { ok: true; tenantId: string; codeVerifier: string; context?: string }
  | { ok: false; reason: "missing_cookie" | "bad_nonce" | "expired" | "malformed" };

export interface CreateOAuthStateOptions {
  tenantId: string;
  context?: string;
  now?: number;
  randomBytes?: (size: number) => Uint8Array;
}

export function createOAuthState(options: CreateOAuthStateOptions): {
  state: string;
  cookieValue: string;
  codeVerifier: string;
  codeChallenge: string;
} {
  const random = options.randomBytes ?? nodeRandomBytes;
  const nonce = Buffer.from(random(32)).toString("base64url");
  const codeVerifier = Buffer.from(random(32)).toString("base64url");
  const payload = { v: 1, tenant: options.tenantId, nonce, issued_at_ms: options.now ?? Date.now() };
  const state = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  const context = options.context === undefined ? "" : `.${Buffer.from(options.context, "utf8").toString("base64url")}`;
  return {
    state,
    cookieValue: `${nonce}.${codeVerifier}${context}`,
    codeVerifier,
    codeChallenge: createHash("sha256").update(codeVerifier).digest("base64url"),
  };
}

export function verifyOAuthState(options: {
  state: string;
  cookieValue?: string;
  now?: number;
  ttlMs?: number;
}): OAuthVerifyResult {
  if (!options.cookieValue) return { ok: false, reason: "missing_cookie" };
  if (!options.state) return { ok: false, reason: "malformed" };
  const [cookieNonce = "", codeVerifier = "", encodedContext] = options.cookieValue.split(".");
  if (!cookieNonce || !codeVerifier) return { ok: false, reason: "malformed" };
  let payload: { v: number; tenant: string; nonce: string; issued_at_ms: number };
  try {
    payload = JSON.parse(Buffer.from(options.state, "base64url").toString("utf8")) as typeof payload;
  } catch {
    return { ok: false, reason: "malformed" };
  }
  if (payload.v !== 1 || typeof payload.tenant !== "string" || typeof payload.nonce !== "string" || typeof payload.issued_at_ms !== "number") {
    return { ok: false, reason: "malformed" };
  }
  if ((options.now ?? Date.now()) - payload.issued_at_ms > (options.ttlMs ?? 10 * 60_000)) {
    return { ok: false, reason: "expired" };
  }
  const actual = Buffer.from(payload.nonce);
  const expected = Buffer.from(cookieNonce);
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
    return { ok: false, reason: "bad_nonce" };
  }
  const context = encodedContext === undefined ? undefined : Buffer.from(encodedContext, "base64url").toString("utf8");
  return {
    ok: true,
    tenantId: payload.tenant,
    codeVerifier,
    ...(context !== undefined ? { context } : {}),
  };
}

export interface AesGcmEnvelope {
  seal(plaintext: string, aad: string | Uint8Array): Promise<string>;
  open(envelope: string, aad: string | Uint8Array): Promise<string>;
}

export function createAesGcmEnvelope(options: {
  keyProvider: () => MaybePromise<Uint8Array>;
  version?: string;
}): AesGcmEnvelope {
  const version = options.version ?? "v2";
  const key = async (): Promise<Buffer> => {
    const resolved = Buffer.from(await options.keyProvider());
    if (resolved.length !== 32) throw new Error(`AES-256-GCM key must be 32 bytes, got ${resolved.length}`);
    return resolved;
  };
  const additionalData = (aad: string | Uint8Array) =>
    typeof aad === "string" ? Buffer.from(aad, "utf8") : Buffer.from(aad);
  return {
    async seal(plaintext, aad) {
      const iv = nodeRandomBytes(12);
      const cipher = createCipheriv("aes-256-gcm", await key(), iv);
      cipher.setAAD(additionalData(aad));
      const ciphertext = Buffer.concat([cipher.update(plaintext, "utf8"), cipher.final()]);
      return [version, iv.toString("base64url"), cipher.getAuthTag().toString("base64url"), ciphertext.toString("base64url")].join(".");
    },
    async open(envelope, aad) {
      const [actualVersion, iv, tag, ciphertext, extra] = envelope.split(".");
      if (actualVersion !== version) throw new Error(`unsupported envelope version ${actualVersion ?? ""}`);
      if (!iv || !tag || !ciphertext || extra !== undefined) throw new Error("malformed ciphertext envelope");
      const decipher = createDecipheriv("aes-256-gcm", await key(), Buffer.from(iv, "base64url"));
      decipher.setAAD(additionalData(aad));
      decipher.setAuthTag(Buffer.from(tag, "base64url"));
      return Buffer.concat([decipher.update(Buffer.from(ciphertext, "base64url")), decipher.final()]).toString("utf8");
    },
  };
}
