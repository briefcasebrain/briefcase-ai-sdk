export interface LakefsClientOptions {
  endpoint: string;
  accessKeyId: string;
  secretAccessKey: string;
  timeoutMs?: number;
  fetch?: (input: string, init?: RequestInit) => Promise<Response>;
  sleep?: (milliseconds: number) => Promise<void>;
  random?: () => number;
  now?: () => number;
}

export interface LakefsStagingLocation {
  physical_address: string;
  presigned_url: string;
  presigned_url_expiry?: number;
  presigned_url_method?: string;
}

export interface LakefsClient {
  health(): Promise<void>;
  listRepositories(): Promise<unknown>;
  send(path: string, init?: RequestInit): Promise<unknown>;
  uploadObject(args: { repo: string; branch: string; path: string; body: string | Buffer; contentType?: string }): Promise<void>;
  getPresignedUploadUrl(args: { repo: string; branch: string; path: string }): Promise<LakefsStagingLocation>;
  linkPhysicalAddress(args: { repo: string; branch: string; path: string; contentType: string; sizeBytes: number; checksum: string; staging: LakefsStagingLocation }): Promise<void>;
  stageObjectAtAddress(args: { repo: string; branch: string; path: string; physicalAddress: string; sizeBytes: number; checksum: string; contentType: string }): Promise<void>;
  getObjectText(args: { repo: string; ref: string; path: string }): Promise<string>;
  getObjectStream(args: { repo: string; ref: string; path: string }): Promise<{ body: ReadableStream<Uint8Array>; contentType: string | undefined; contentLength: number | undefined } | null>;
  commit(args: { repo: string; branch: string; message: string; metadata?: Record<string, string> }): Promise<{ id: string }>;
  listObjectCommits(args: { repo: string; ref: string; path: string; limit?: number }): Promise<Array<{ id: string; message: string; timestamp: Date; author: string }>>;
  tag(args: { repo: string; id: string; ref: string }): Promise<void>;
  import(args: { repo: string; branch: string; sources: Array<{ s3Prefix: string; destPath: string; kind?: "prefix" | "object" }>; commitMessage: string; metadata?: Record<string, string> }): Promise<{ commit_id: string }>;
  snapshot(args: { repo: string; sourceBranch: string; tagName: string; commitMessage: string; metadata?: Record<string, string> }): Promise<{ commit_id: string; tag: string }>;
  branch(args: { repo: string; sourceRef: string; name: string; metadata?: Record<string, string> }): Promise<{ branch: string }>;
  readAt(args: { repo: string; ref: string; path: string }): Promise<string | Buffer>;
  deleteObject(args: { repo: string; branch: string; path: string }): Promise<boolean>;
  listObjects(args: { repo: string; ref: string; prefix: string; after?: string; limit?: number }): Promise<{ objects: Array<{ path: string; checksum: string; sizeBytes: number; mtime: Date }>; nextAfter: string | null; hasMore: boolean }>;
}

const RETRYABLE_READ_STATUSES = new Set([429, 502, 503, 504]);
const defaultSleep = (milliseconds: number) => new Promise<void>((resolve) => setTimeout(resolve, milliseconds));

export function createLakefsClient(options: LakefsClientOptions): LakefsClient {
  if (!options.endpoint || !options.accessKeyId || !options.secretAccessKey) {
    throw new Error("lakefs endpoint and credentials are required");
  }
  const base = `${options.endpoint.replace(/\/+$/, "")}${/\/api\/v1\/?$/.test(options.endpoint) ? "" : "/api/v1"}`;
  const auth = `Basic ${Buffer.from(`${options.accessKeyId}:${options.secretAccessKey}`).toString("base64")}`;
  const fetchFn = options.fetch ?? ((input, init) => fetch(input, init));
  const sleep = options.sleep ?? defaultSleep;
  const random = options.random ?? Math.random;
  const now = options.now ?? Date.now;
  const timeoutMs = Math.min(Math.max(options.timeoutMs ?? 8_000, 1), 60_000);

  const request = async (input: string, init: RequestInit = {}): Promise<Response> => {
    const method = (init.method ?? "GET").toUpperCase();
    const attempts = method === "GET" || method === "HEAD" ? 2 : 1;
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      const deadline = AbortSignal.timeout(timeoutMs);
      const signal = init.signal ? AbortSignal.any([init.signal, deadline]) : deadline;
      try {
        const response = await fetchFn(input, { ...init, signal });
        if (attempt < attempts && RETRYABLE_READ_STATUSES.has(response.status)) {
          await response.arrayBuffer().catch(() => undefined);
          await sleep(25 + Math.floor(random() * 25));
          continue;
        }
        return response;
      } catch (error) {
        if (init.signal?.aborted) throw error;
        if (attempt < attempts) {
          await sleep(25 + Math.floor(random() * 25));
          continue;
        }
        if (deadline.aborted) throw new Error(`lakefs request timed out after ${timeoutMs}ms`, { cause: error });
        throw error;
      }
    }
    throw new Error("lakefs request exhausted its bounded attempts");
  };

  const authorized = (init: RequestInit = {}): RequestInit => {
    const headers = new Headers(init.headers);
    headers.set("Authorization", auth);
    if (init.body !== undefined && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return { ...init, headers };
  };

  const errorFor = async (response: Response, operation: string): Promise<Error> => {
    const detail = await response.text().catch(() => "");
    return new Error(`lakefs ${response.status} ${response.statusText} on ${operation}: ${detail.slice(0, 200)}`);
  };

  const send = async (path: string, init: RequestInit = {}): Promise<unknown> => {
    const response = await request(`${base}${path}`, authorized(init));
    if (!response.ok) throw await errorFor(response, path);
    if (response.status === 204) return null;
    const contentType = response.headers.get("content-type") ?? "";
    return contentType.includes("application/json") ? response.json() : response.text();
  };

  const objectUrl = (repo: string, ref: string, path: string) =>
    `${base}/repositories/${encodeURIComponent(repo)}/refs/${encodeURIComponent(ref)}/objects?path=${encodeURIComponent(path)}`;
  const stagingUrl = (repo: string, branch: string, path: string, presign = false) =>
    `${base}/repositories/${encodeURIComponent(repo)}/branches/${encodeURIComponent(branch)}/staging/backing?path=${encodeURIComponent(path)}${presign ? "&presign=true" : ""}`;

  const client: LakefsClient = {
    async health() {
      const response = await request(`${base}/healthcheck`, authorized());
      if (!response.ok) throw await errorFor(response, "healthcheck");
    },
    listRepositories: () => send("/repositories"),
    send,
    async getPresignedUploadUrl({ repo, branch, path }) {
      const response = await request(stagingUrl(repo, branch, path, true), authorized());
      if (!response.ok) throw await errorFor(response, "presign");
      return response.json() as Promise<LakefsStagingLocation>;
    },
    async linkPhysicalAddress({ repo, branch, path, contentType, sizeBytes, checksum, staging }) {
      const response = await request(stagingUrl(repo, branch, path), authorized({
        method: "PUT",
        body: JSON.stringify({ staging, checksum, size_bytes: sizeBytes, content_type: contentType }),
      }));
      if (!response.ok) throw await errorFor(response, "linkPhysicalAddress");
    },
    async stageObjectAtAddress({ repo, branch, path, physicalAddress, sizeBytes, checksum, contentType }) {
      await client.linkPhysicalAddress({
        repo,
        branch,
        path,
        contentType,
        sizeBytes,
        checksum,
        staging: { physical_address: physicalAddress, presigned_url: "" },
      });
    },
    async uploadObject({ repo, branch, path, body, contentType = "application/octet-stream" }) {
      const staging = await client.getPresignedUploadUrl({ repo, branch, path });
      const bytes = typeof body === "string" ? Buffer.from(body, "utf8") : body;
      const uploaded = await request(staging.presigned_url, {
        method: "PUT",
        headers: { "Content-Type": contentType },
        body: new Uint8Array(bytes),
      });
      if (!uploaded.ok) throw await errorFor(uploaded, "presigned PUT");
      await client.linkPhysicalAddress({
        repo,
        branch,
        path,
        contentType,
        sizeBytes: bytes.byteLength,
        checksum: uploaded.headers.get("etag")?.replaceAll('"', "") ?? "",
        staging,
      });
    },
    async getObjectText({ repo, ref, path }) {
      const response = await request(objectUrl(repo, ref, path), authorized());
      if (!response.ok) throw await errorFor(response, "getObjectText");
      return response.text();
    },
    async getObjectStream({ repo, ref, path }) {
      const response = await request(objectUrl(repo, ref, path), authorized());
      if (response.status === 404) return null;
      if (!response.ok) throw await errorFor(response, "getObjectStream");
      if (!response.body) return null;
      const length = response.headers.get("content-length");
      return {
        body: response.body,
        contentType: response.headers.get("content-type") ?? undefined,
        contentLength: length && /^\d+$/.test(length) ? Number(length) : undefined,
      };
    },
    async commit({ repo, branch, message, metadata }) {
      const result = (await send(`/repositories/${encodeURIComponent(repo)}/branches/${encodeURIComponent(branch)}/commits`, {
        method: "POST",
        body: JSON.stringify({ message, metadata }),
      })) as { id?: string };
      if (!result.id) throw new Error("lakefs commit returned no id");
      return { id: result.id };
    },
    async listObjectCommits({ repo, ref, path, limit = 50 }) {
      const query = new URLSearchParams({ objects: path, amount: String(limit) });
      const result = (await send(`/repositories/${encodeURIComponent(repo)}/refs/${encodeURIComponent(ref)}/commits?${query}`)) as {
        results?: Array<{ id: string; message?: string; creation_date?: number; committer?: string }>;
      };
      return (result.results ?? []).map((entry) => ({
        id: entry.id,
        message: entry.message ?? "",
        timestamp: new Date((entry.creation_date ?? 0) * 1000),
        author: entry.committer ?? "",
      }));
    },
    async tag({ repo, id, ref }) {
      try {
        await send(`/repositories/${encodeURIComponent(repo)}/tags`, { method: "POST", body: JSON.stringify({ id, ref }) });
      } catch (error) {
        if (!(error instanceof Error && /409|already exists/i.test(error.message))) throw error;
      }
    },
    async branch({ repo, sourceRef, name, metadata }) {
      try {
        await send(`/repositories/${encodeURIComponent(repo)}/branches`, {
          method: "POST",
          body: JSON.stringify({ name, source: sourceRef, metadata }),
        });
      } catch (error) {
        if (!(error instanceof Error && /409|already exists/i.test(error.message))) throw error;
        const resolveRef = async (ref: string): Promise<string | null> => {
          try {
            const result = (await send(`/repositories/${encodeURIComponent(repo)}/refs/${encodeURIComponent(ref)}/commits?amount=1`)) as { results?: Array<{ id?: string }> };
            return result.results?.[0]?.id ?? null;
          } catch {
            return null;
          }
        };
        const [existing, requested] = await Promise.all([resolveRef(name), resolveRef(sourceRef)]);
        if (existing && requested && existing !== requested) {
          throw new Error(`lakefs branch ${name} already exists at ${existing}, not ${requested}`);
        }
      }
      return { branch: name };
    },
    async import({ repo, branch, sources, commitMessage, metadata }) {
      const started = (await send(`/repositories/${encodeURIComponent(repo)}/branches/${encodeURIComponent(branch)}/import`, {
        method: "POST",
        body: JSON.stringify({
          paths: sources.map((source) => ({
            type: (source.kind ?? (source.s3Prefix.endsWith("/") ? "prefix" : "object")) === "prefix" ? "common_prefix" : "object",
            path: source.s3Prefix,
            destination: source.destPath,
          })),
          commit: { message: commitMessage, metadata },
        }),
      })) as { id?: string };
      if (!started.id) throw new Error("lakefs import returned no id");
      const deadline = now() + 10 * 60_000;
      let delay = 500;
      while (now() < deadline) {
        const status = (await send(`/repositories/${encodeURIComponent(repo)}/branches/${encodeURIComponent(branch)}/import?id=${encodeURIComponent(started.id)}`)) as {
          completed?: boolean;
          status?: string;
          error?: { message?: string };
          commit?: { id?: string };
        };
        if (status.error?.message) throw new Error(`lakefs import error: ${status.error.message}`);
        if (status.completed || status.status === "completed") {
          if (status.commit?.id) return { commit_id: status.commit.id };
          const committed = await client.commit({ repo, branch, message: commitMessage, ...(metadata ? { metadata } : {}) });
          return { commit_id: committed.id };
        }
        await sleep(delay);
        delay = Math.min(delay * 2, 5_000);
      }
      throw new Error(`lakefs import ${started.id} did not complete within 10m`);
    },
    async snapshot({ repo, sourceBranch, tagName, commitMessage, metadata }) {
      const committed = await client.commit({ repo, branch: sourceBranch, message: commitMessage, ...(metadata ? { metadata } : {}) });
      await client.tag({ repo, id: tagName, ref: committed.id });
      return { commit_id: committed.id, tag: tagName };
    },
    async readAt({ repo, ref, path }) {
      const response = await request(objectUrl(repo, ref, path), authorized());
      if (!response.ok) throw await errorFor(response, "readAt");
      const contentType = response.headers.get("content-type") ?? "";
      if (contentType.startsWith("text/") || /json|ndjson|xml/.test(contentType)) return response.text();
      return Buffer.from(await response.arrayBuffer());
    },
    async deleteObject({ repo, branch, path }) {
      const url = `${base}/repositories/${encodeURIComponent(repo)}/branches/${encodeURIComponent(branch)}/objects?path=${encodeURIComponent(path)}`;
      const response = await request(url, authorized({ method: "DELETE" }));
      if (response.status === 404) return false;
      if (!response.ok) throw await errorFor(response, "deleteObject");
      return true;
    },
    async listObjects({ repo, ref, prefix, after, limit = 1000 }) {
      const query = new URLSearchParams({ prefix, amount: String(limit) });
      if (after) query.set("after", after);
      const response = await request(`${base}/repositories/${encodeURIComponent(repo)}/refs/${encodeURIComponent(ref)}/objects/ls?${query}`, authorized());
      if (!response.ok) throw await errorFor(response, "listObjects");
      const result = (await response.json()) as {
        results?: Array<{ path: string; path_type?: string; checksum?: string; size_bytes?: number; mtime?: number }>;
        pagination?: { has_more?: boolean; next_offset?: string };
      };
      const objects = (result.results ?? [])
        .filter((entry) => (entry.path_type ?? "object") === "object")
        .map((entry) => ({
          path: entry.path,
          checksum: entry.checksum ?? "",
          sizeBytes: entry.size_bytes ?? 0,
          mtime: new Date((entry.mtime ?? 0) * 1000),
        }));
      const hasMore = result.pagination?.has_more ?? false;
      return { objects, nextAfter: hasMore ? result.pagination?.next_offset ?? null : null, hasMore };
    },
  };
  return client;
}
