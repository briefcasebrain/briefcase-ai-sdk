import { createPrivateKey, createPublicKey } from "node:crypto";
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { BundleFormatError, ed25519KeyId, readZip, verifyBundle } from "../src/integrity/index.js";

// Written by the Python SDK (tests/fixtures/bundles/generate.py); the Python
// verifier checks the same files.
const fixture = (name: string) =>
  new Uint8Array(readFileSync(new URL(`../../../tests/fixtures/bundles/${name}`, import.meta.url)));
// The fixtures are signed with the test seed bytes 0..31 (it protects nothing);
// derive its key id the same way an auditor would from a published public key.
const seed = Buffer.from(Array.from({ length: 32 }, (_, i) => i));
const pkcs8 = Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), seed]);
const publicJwk = createPublicKey(createPrivateKey({ key: pkcs8, format: "der", type: "pkcs8" })).export({ format: "jwk" });
const KEY_ID = ed25519KeyId(String(publicJwk.x));

function rezip(files: Map<string, Uint8Array>): Map<string, Uint8Array> {
  return new Map(files);
}

describe("evidence bundle verifier (format 2)", () => {
  it("verifies a Python-made bundle of chained records with Merkle proofs", () => {
    const result = verifyBundle(fixture("records-v2.zip"), { expectedKeyIds: [KEY_ID] });
    expect(result.problems).toEqual([]);
    expect(result.intact).toBe(true);
    expect(result.recordsChecked).toBe(6);
    expect(result.entriesChecked).toBe(6);
    expect(result.signatures.filter((s) => s.status === "valid")).toHaveLength(3);
  });

  it("verifies a migrated examiner bundle and leaves KMS signatures unchecked", () => {
    const examiner = verifyBundle(fixture("examiner-v2.zip"), { expectedKeyIds: [KEY_ID] });
    expect(examiner.intact).toBe(true);
    expect(examiner.subject).toBe("examiner");
    expect(examiner.notes.join(" ")).toMatch(/legacy_signatures/);
    const kms = verifyBundle(fixture("kms-v2.zip"));
    expect(kms.intact).toBe(true);
    expect(kms.signatures[0].status).toBe("unchecked");
  });

  it("detects an edited record, a wrong pinned key and a stripped seal", () => {
    const files = rezip(readZip(fixture("records-v2.zip")));
    const records = Buffer.from(files.get("records.jsonl") as Uint8Array).toString("utf8");
    files.set("records.jsonl", Buffer.from(records.replace('"wf-1"', '"wf-2"'), "utf8"));
    const edited = verifyBundle(files);
    expect(edited.intact).toBe(false);
    expect(edited.problems.map((p) => p.kind)).toEqual(expect.arrayContaining(["file_modified", "modified_entry"]));

    const pinned = verifyBundle(fixture("records-v2.zip"), { expectedKeyIds: ["0".repeat(64)] });
    expect(pinned.problems.map((p) => p.kind)).toContain("unexpected_key");

    const stripped = rezip(readZip(fixture("records-v2.zip")));
    const manifest = JSON.parse(Buffer.from(stripped.get("manifest.json") as Uint8Array).toString("utf8"));
    manifest.signatures = [];
    stripped.set("manifest.json", Buffer.from(JSON.stringify(manifest), "utf8"));
    expect(verifyBundle(stripped).problems.map((p) => p.kind)).toContain("bundle_signature_invalid");
  });

  it("refuses what it cannot read", () => {
    expect(() => verifyBundle(new Uint8Array([1, 2, 3]))).toThrow(BundleFormatError);
    const files = rezip(readZip(fixture("kms-v2.zip")));
    const manifest = JSON.parse(Buffer.from(files.get("manifest.json") as Uint8Array).toString("utf8"));
    files.set("manifest.json", Buffer.from(JSON.stringify({ ...manifest, format_version: 3 }), "utf8"));
    expect(() => verifyBundle(files)).toThrow(/format version 3/);
  });
});
