import { describe, expect, it, vi } from "vitest";

import { createLakefsClient } from "../src/lakefs/index.js";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("lakeFS client", () => {
  it("authenticates API calls and normalizes the endpoint", async () => {
    const fetch = vi.fn(async () => json([{ id: "repo" }]));
    const client = createLakefsClient({
      endpoint: "https://lakefs.example/api/v1/",
      accessKeyId: "key",
      secretAccessKey: "secret",
      fetch,
    });
    await expect(client.listRepositories()).resolves.toEqual([{ id: "repo" }]);
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe("https://lakefs.example/api/v1/repositories");
    expect(new Headers(init?.headers).get("authorization")).toBe(
      `Basic ${Buffer.from("key:secret").toString("base64")}`,
    );
  });

  it("retries one transient read but never retries a write", async () => {
    const readFetch = vi
      .fn<(_: string, __?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(new Response("busy", { status: 503 }))
      .mockResolvedValueOnce(json({ ok: true }));
    const readClient = createLakefsClient({
      endpoint: "https://lakefs.example",
      accessKeyId: "key",
      secretAccessKey: "secret",
      fetch: readFetch,
      sleep: async () => undefined,
      random: () => 0,
    });
    await expect(readClient.send("/health")).resolves.toEqual({ ok: true });
    expect(readFetch).toHaveBeenCalledTimes(2);

    const writeFetch = vi.fn(async () => new Response("busy", { status: 503 }));
    const writeClient = createLakefsClient({
      endpoint: "https://lakefs.example",
      accessKeyId: "key",
      secretAccessKey: "secret",
      fetch: writeFetch,
    });
    await expect(writeClient.commit({ repo: "r", branch: "main", message: "m" })).rejects.toThrow(
      /503/,
    );
    expect(writeFetch).toHaveBeenCalledTimes(1);
  });

  it("preserves caller headers supplied as tuples", async () => {
    const fetch = vi.fn(async (_: string, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      expect(headers.get("x-request-id")).toBe("request-1");
      expect(headers.get("content-type")).toBe("application/custom+json");
      return json({ ok: true });
    });
    const client = createLakefsClient({ endpoint: "https://lakefs.example", accessKeyId: "k", secretAccessKey: "s", fetch });
    await client.send("/custom", {
      method: "POST",
      headers: [["X-Request-Id", "request-1"], ["Content-Type", "application/custom+json"]],
      body: "{}",
    });
  });

  it("performs the presign, upload, and link sequence", async () => {
    const fetch = vi
      .fn<(_: string, __?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(
        json({ physical_address: "s3://bucket/key", presigned_url: "https://s3.example/upload" }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 200, headers: { etag: '"abc"' } }))
      .mockResolvedValueOnce(new Response(null, { status: 201 }));
    const client = createLakefsClient({
      endpoint: "https://lakefs.example",
      accessKeyId: "key",
      secretAccessKey: "secret",
      fetch,
    });
    await client.uploadObject({ repo: "r", branch: "main", path: "a.txt", body: "hello" });
    expect(fetch.mock.calls.map(([url, init]) => [url, init?.method ?? "GET"])).toEqual([
      [expect.stringContaining("staging/backing?path=a.txt&presign=true"), "GET"],
      ["https://s3.example/upload", "PUT"],
      [expect.stringContaining("staging/backing?path=a.txt"), "PUT"],
    ]);
    expect(String(fetch.mock.calls[2]?.[1]?.body)).toContain('"checksum":"abc"');
  });

  it("treats idempotent tag, branch, and delete statuses as success", async () => {
    const fetch = vi
      .fn<(_: string, __?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(new Response("exists", { status: 409 }))
      .mockResolvedValueOnce(new Response("exists", { status: 409 }))
      .mockResolvedValueOnce(json({ results: [{ id: "same" }] }))
      .mockResolvedValueOnce(json({ results: [{ id: "same" }] }))
      .mockResolvedValueOnce(new Response("missing", { status: 404 }));
    const client = createLakefsClient({
      endpoint: "https://lakefs.example",
      accessKeyId: "key",
      secretAccessKey: "secret",
      fetch,
    });
    await expect(client.tag({ repo: "r", id: "t", ref: "main" })).resolves.toBeUndefined();
    await expect(client.branch({ repo: "r", sourceRef: "main", name: "b" })).resolves.toEqual({ branch: "b" });
    await expect(client.deleteObject({ repo: "r", branch: "main", path: "gone" })).resolves.toBe(false);
  });

  it("maps typed object, commit, stream, and pagination responses", async () => {
    const fetch = vi
      .fn<(_: string, __?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(new Response("body", { headers: { "content-type": "text/plain" } }))
      .mockResolvedValueOnce(json({ results: [{ id: "c1", message: "m", creation_date: 1, committer: "a" }] }))
      .mockResolvedValueOnce(new Response("stream", { headers: { "content-length": "6" } }))
      .mockResolvedValueOnce(json({ results: [{ path: "a", path_type: "object", checksum: "x", size_bytes: 2, mtime: 1 }], pagination: { has_more: true, next_offset: "a" } }));
    const client = createLakefsClient({ endpoint: "https://lakefs.example", accessKeyId: "k", secretAccessKey: "s", fetch });
    await expect(client.getObjectText({ repo: "r", ref: "main", path: "a" })).resolves.toBe("body");
    await expect(client.listObjectCommits({ repo: "r", ref: "main", path: "a" })).resolves.toEqual([
      { id: "c1", message: "m", timestamp: new Date(1000), author: "a" },
    ]);
    const stream = await client.getObjectStream({ repo: "r", ref: "main", path: "a" });
    expect(stream?.contentLength).toBe(6);
    await expect(client.listObjects({ repo: "r", ref: "main", prefix: "" })).resolves.toEqual({
      objects: [{ path: "a", checksum: "x", sizeBytes: 2, mtime: new Date(1000) }],
      nextAfter: "a",
      hasMore: true,
    });
  });

  it("polls imports and composes commit plus tag snapshots", async () => {
    const fetch = vi
      .fn<(_: string, __?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(json({ id: "import-1" }, 202))
      .mockResolvedValueOnce(json({ status: "completed" }))
      .mockResolvedValueOnce(json({ id: "commit-1" }, 201))
      .mockResolvedValueOnce(json({ id: "commit-2" }, 201))
      .mockResolvedValueOnce(new Response(null, { status: 201 }));
    const client = createLakefsClient({
      endpoint: "https://lakefs.example",
      accessKeyId: "k",
      secretAccessKey: "s",
      fetch,
      sleep: async () => undefined,
    });
    await expect(
      client.import({
        repo: "r",
        branch: "main",
        sources: [{ s3Prefix: "s3://bucket/prefix/", destPath: "dest" }],
        commitMessage: "import",
      }),
    ).resolves.toEqual({ commit_id: "commit-1" });
    await expect(
      client.snapshot({ repo: "r", sourceBranch: "main", tagName: "snap", commitMessage: "snapshot" }),
    ).resolves.toEqual({ commit_id: "commit-2", tag: "snap" });
  });

  it("covers health, direct staging, explicit linking, and text and binary reads", async () => {
    const fetch = vi
      .fn<(_: string, __?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 201 }))
      .mockResolvedValueOnce(new Response(null, { status: 201 }))
      .mockResolvedValueOnce(new Response("text", { headers: { "content-type": "text/plain" } }))
      .mockResolvedValueOnce(new Response(Uint8Array.from([0, 1, 2]), { headers: { "content-type": "application/octet-stream" } }));
    const client = createLakefsClient({ endpoint: "https://lakefs.example", accessKeyId: "k", secretAccessKey: "s", fetch });

    await expect(client.health()).resolves.toBeUndefined();
    await client.stageObjectAtAddress({
      repo: "r",
      branch: "main",
      path: "direct.bin",
      physicalAddress: "s3://bucket/direct.bin",
      sizeBytes: 3,
      checksum: "abc",
      contentType: "application/octet-stream",
    });
    await client.linkPhysicalAddress({
      repo: "r",
      branch: "main",
      path: "linked.bin",
      contentType: "application/octet-stream",
      sizeBytes: 3,
      checksum: "def",
      staging: { physical_address: "s3://bucket/linked.bin", presigned_url: "https://upload" },
    });
    await expect(client.readAt({ repo: "r", ref: "main", path: "text" })).resolves.toBe("text");
    const binary = await client.readAt({ repo: "r", ref: "main", path: "binary" });
    expect(Buffer.isBuffer(binary)).toBe(true);
    expect(binary).toEqual(Buffer.from([0, 1, 2]));
  });

  it("enforces request deadlines on retryable reads", async () => {
    const fetch = vi.fn((_: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
    }));
    const client = createLakefsClient({
      endpoint: "https://lakefs.example",
      accessKeyId: "k",
      secretAccessKey: "s",
      fetch,
      timeoutMs: 1,
      sleep: async () => undefined,
    });
    await expect(client.health()).rejects.toThrow(/timed out after 1ms/);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
