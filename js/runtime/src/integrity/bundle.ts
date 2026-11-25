/**
 * Verifier for briefcase evidence bundles, format 2 (`briefcase-bundle`).
 *
 * The same checks as Python's `briefcase.compliance.verify_bundle` for
 * format 2: every file against the manifest's SHA-256 list, the manifest
 * seal (`content_hash` over the RFC 8785 form of the manifest without
 * `content_hash` and `signatures`), the sealed signer list, Ed25519
 * signatures (via `node:crypto`), checkpoint hashes, signatures and
 * chaining, and for each chained record its record hash, entry hash and
 * Merkle inclusion proof. The format is specified in
 * `docs/features/audit-bundle.md` of the SDK repository.
 *
 * Not covered here: AWS KMS signatures (reported `unchecked`; they need a
 * call to KMS) and format 1 (the examiner JSON, whose content hash uses
 * Python's legacy canonical profile); use the Python SDK for those.
 */
import { createHash, createPublicKey, verify as cryptoVerify } from "node:crypto";
import { inflateRawSync } from "node:zlib";

import { canonicalJsonV2, sha256Hex } from "./index.js";

export const BUNDLE_FORMAT = "briefcase-bundle";
export const BUNDLE_FORMAT_VERSION = 2;

export interface BundleProblem {
  kind: string;
  seq?: number;
  detail: string;
}

export interface BundleSignatureResult {
  type: string;
  keyId: string | null;
  covers: string;
  /** `untrusted`: cryptographically valid, but by a key that was not pinned. */
  status: "valid" | "invalid" | "unchecked" | "untrusted";
  detail: string;
}

export interface BundleVerification {
  intact: boolean;
  formatVersion: number;
  subject: string;
  recordsChecked: number;
  entriesChecked: number;
  checkpointsChecked: number;
  signatures: BundleSignatureResult[];
  problems: BundleProblem[];
  notes: string[];
}

export interface VerifyBundleOptions {
  /** Accepted Ed25519 key ids (RFC 7638 thumbprints). Pinning is strongly advised. */
  expectedKeyIds?: readonly string[];
}

export class BundleFormatError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BundleFormatError";
  }
}

/** Read the members of a ZIP archive (stored or deflated entries). */
export function readZip(data: Uint8Array): Map<string, Uint8Array> {
  const buf = Buffer.from(data.buffer, data.byteOffset, data.byteLength);
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 22 - 0xffff); i -= 1) {
    if (buf.readUInt32LE(i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new BundleFormatError("not a ZIP file (no end of central directory)");
  const count = buf.readUInt16LE(eocd + 10);
  let offset = buf.readUInt32LE(eocd + 16);
  const files = new Map<string, Uint8Array>();
  for (let n = 0; n < count; n += 1) {
    if (buf.readUInt32LE(offset) !== 0x02014b50) throw new BundleFormatError("corrupt ZIP central directory");
    const method = buf.readUInt16LE(offset + 10);
    const compressedSize = buf.readUInt32LE(offset + 20);
    const nameLength = buf.readUInt16LE(offset + 28);
    const extraLength = buf.readUInt16LE(offset + 30);
    const commentLength = buf.readUInt16LE(offset + 32);
    const localOffset = buf.readUInt32LE(offset + 42);
    const name = buf.subarray(offset + 46, offset + 46 + nameLength).toString("utf8");
    if (buf.readUInt32LE(localOffset) !== 0x04034b50) throw new BundleFormatError("corrupt ZIP local header");
    const start = localOffset + 30 + buf.readUInt16LE(localOffset + 26) + buf.readUInt16LE(localOffset + 28);
    const raw = buf.subarray(start, start + compressedSize);
    if (method === 0) files.set(name, new Uint8Array(raw));
    else if (method === 8) files.set(name, new Uint8Array(inflateRawSync(raw)));
    else throw new BundleFormatError(`unsupported ZIP compression method ${method} for ${name}`);
    offset += 46 + nameLength + extraLength + commentLength;
  }
  return files;
}

const b64url = (text: string): Buffer => Buffer.from(text, "base64url");

/** RFC 7638 thumbprint (hex) of an Ed25519 public key given by its JWK `x`. */
export function ed25519KeyId(x: string): string {
  return createHash("sha256").update(`{"crv":"Ed25519","kty":"OKP","x":"${x}"}`).digest("hex");
}

/** Ed25519 signature (base64url) over the raw bytes of a hex digest. */
export function verifyEd25519Digest(digestHex: string, signature: string, x: string): boolean {
  try {
    const key = createPublicKey({ key: { kty: "OKP", crv: "Ed25519", x }, format: "jwk" });
    return cryptoVerify(null, Buffer.from(digestHex, "hex"), key, b64url(signature));
  } catch {
    return false;
  }
}

function leafHash(leaf: Buffer): Buffer {
  return createHash("sha256").update(Buffer.concat([Buffer.from([0]), leaf])).digest();
}

function nodeHash(left: Buffer, right: Buffer): Buffer {
  return createHash("sha256").update(Buffer.concat([Buffer.from([1]), left, right])).digest();
}

function split(n: number): number {
  let k = 1;
  while (k * 2 < n) k *= 2;
  return k;
}

/** Fold an RFC 6962 audit path; `null` when it does not fit `index`/`size`. */
export function merkleRootFromProof(leaf: Buffer, index: number, size: number, path: readonly Buffer[]): Buffer | null {
  if (index < 0 || index >= size) return null;
  const walk = (i: number, n: number, p: readonly Buffer[]): Buffer | null => {
    if (n === 1) return p.length === 0 ? leafHash(leaf) : null;
    if (p.length === 0) return null;
    const last = p[p.length - 1] as Buffer;
    const rest = p.slice(0, -1);
    const k = split(n);
    if (i < k) {
      const sub = walk(i, k, rest);
      return sub && nodeHash(sub, last);
    }
    const sub = walk(i - k, n - k, rest);
    return sub && nodeHash(last, sub);
  };
  return walk(index, size, path);
}

const jcsHash = (value: unknown): string => sha256Hex(canonicalJsonV2(value));

/** `entry_hash` of a chained-store entry. */
export function chainEntryHash(entry: Record<string, unknown>): string {
  return jcsHash({
    v: 1,
    hash_scheme: entry.hash_scheme,
    seq: entry.seq,
    record_id: entry.record_id,
    kind: entry.kind,
    prev_hash: entry.prev_hash,
    record_hash: entry.record_hash,
    recorded_at: entry.recorded_at,
  });
}

/** `checkpoint_hash` of a checkpoint. */
export function checkpointHash(cp: Record<string, unknown>): string {
  const body: Record<string, unknown> = { v: 1 };
  for (const name of [
    "hash_scheme",
    "checkpoint_no",
    "from_seq",
    "seq",
    "head_hash",
    "merkle_root",
    "prev_checkpoint_hash",
    "created_at",
    "key_id",
  ]) {
    body[name] = cp[name] ?? null;
  }
  return jcsHash(body);
}

type Json = Record<string, unknown>;

/**
 * Verify a format-2 bundle, given as the ZIP bytes or as its member files.
 * Throws `BundleFormatError` for something that is not a format-2 bundle.
 */
export function verifyBundle(
  input: Uint8Array | ReadonlyMap<string, Uint8Array>,
  options: VerifyBundleOptions = {},
): BundleVerification {
  const files = input instanceof Uint8Array ? readZip(input) : new Map(input);
  const manifestBytes = files.get("manifest.json");
  if (!manifestBytes) throw new BundleFormatError("no manifest.json: not a briefcase-ai bundle");
  const manifest = JSON.parse(Buffer.from(manifestBytes).toString("utf8")) as Json;
  if (manifest.format !== BUNDLE_FORMAT) throw new BundleFormatError(`unknown bundle format ${String(manifest.format)}`);
  if (manifest.format_version !== BUNDLE_FORMAT_VERSION) {
    throw new BundleFormatError(
      `bundle format version ${String(manifest.format_version)} is not supported here (this verifier reads ${BUNDLE_FORMAT_VERSION}; format 1 needs the Python SDK)`,
    );
  }
  if (manifest.hash_scheme !== "jcs-1") throw new BundleFormatError(`unknown hash scheme ${String(manifest.hash_scheme)}`);

  const pins = new Set(options.expectedKeyIds ?? []);
  const problems: BundleProblem[] = [];
  const notes: string[] = [];
  const signatures: BundleSignatureResult[] = [];
  const problem = (kind: string, detail: string, seq?: number) =>
    problems.push(seq === undefined ? { kind, detail } : { kind, detail, seq });

  const listed = (manifest.files ?? {}) as Record<string, string>;
  const names = new Set([...files.keys()].filter((name) => name !== "manifest.json"));
  for (const name of [...new Set([...Object.keys(listed), ...names])].sort()) {
    if (!(name in listed)) problem("unexpected_file", `${name} is in the bundle but not in the manifest`);
    else if (!files.has(name)) problem("missing_file", `${name} is listed in the manifest but missing`);
    else if (sha256Hex(files.get(name) as Uint8Array) !== listed[name]) {
      problem("file_modified", `${name} was changed after the bundle was made`);
    }
  }

  const body: Json = {};
  for (const [key, value] of Object.entries(manifest)) if (key !== "content_hash" && key !== "signatures") body[key] = value;
  const digest = jcsHash(body);
  if (manifest.content_hash !== `sha256:${digest}`) problem("manifest_modified", "manifest.json was changed after the bundle was made");

  const sigs = (manifest.signatures ?? []) as Json[];
  const declared = ((manifest.signers ?? []) as Json[]).map((s) => `${String(s.type)}|${String(s.key_id)}`);
  const actual = sigs.map((s) => `${String(s.type)}|${String(s.key_id)}`);
  if (declared.join("\n") !== actual.join("\n")) {
    problem("bundle_signature_invalid", "the signatures do not match the signers sealed in the manifest");
  }
  let trusted = 0;
  const checkEd25519 = (keyId: unknown, x: unknown, value: unknown, digestHex: string, covers: string) => {
    const kid = typeof keyId === "string" ? keyId : null;
    let status: BundleSignatureResult["status"] = "valid";
    let detail = "";
    if (!kid || typeof x !== "string" || typeof value !== "string" || ed25519KeyId(x) !== kid) {
      status = "invalid";
      detail = "the signature names a different key than it carries";
    } else if (!verifyEd25519Digest(digestHex, value, x)) {
      status = "invalid";
      detail = "Ed25519 signature does not verify";
    } else if (pins.size > 0 && !pins.has(kid)) {
      status = "untrusted";
      detail = `valid, but by key ${kid.slice(0, 12)}, which is not one you pinned`;
      // As in the Python verifier: every checkpoint must be by a pinned key;
      // an extra seal on the bundle by another key is only noted.
      if (covers.startsWith("checkpoint")) problem("unexpected_key", `${covers} is signed by key ${kid.slice(0, 12)}, not an expected key`);
      else notes.push(`${covers} signature: ${detail}`);
    }
    if (status === "invalid") problem("bundle_signature_invalid", `${covers}: ${detail}`);
    if (status === "valid") trusted += 1;
    signatures.push({ type: "ed25519", keyId: kid, covers, status, detail });
  };
  for (const sig of sigs) {
    if (sig.type === "ed25519") checkEd25519(sig.key_id, sig.public_key, sig.value, digest, "bundle");
    else {
      signatures.push({
        type: String(sig.type),
        keyId: typeof sig.key_id === "string" ? sig.key_id : null,
        covers: "bundle",
        status: "unchecked",
        detail: `${String(sig.type)} signatures are not checked by this verifier`,
      });
      notes.push(`${String(sig.type)} signature by ${String(sig.key_id)} not checked here (use the Python SDK with --kms)`);
    }
  }

  const text = (name: string, fallback: string) =>
    files.has(name) ? Buffer.from(files.get(name) as Uint8Array).toString("utf8") : fallback;
  const checkpoints = JSON.parse(text("checkpoints.json", "[]")) as Json[];
  const keys = JSON.parse(text("keys.json", "{}")) as Record<string, Json>;
  const byNo = new Map<number, Json>();
  for (const cp of checkpoints) {
    byNo.set(Number(cp.checkpoint_no), cp);
    let hashOk = false;
    try {
      hashOk = checkpointHash(cp) === cp.checkpoint_hash;
    } catch {
      hashOk = false;
    }
    if (!hashOk) problem("checkpoint_modified", `checkpoint ${String(cp.checkpoint_no)} was changed after it was written`);
    if (cp.signature) {
      checkEd25519(cp.key_id, cp.public_key, cp.signature, String(cp.checkpoint_hash), `checkpoint ${String(cp.checkpoint_no)}`);
      const kid = String(cp.key_id);
      if (!keys[kid] || keys[kid].x !== cp.public_key) {
        problem("checkpoint_key_mismatch", `checkpoint ${String(cp.checkpoint_no)}'s key is not the one keys.json lists`);
      }
    }
  }
  for (const [no, cp] of byNo) {
    const next = byNo.get(no + 1);
    if (next && (next.prev_checkpoint_hash !== cp.checkpoint_hash || Number(next.from_seq) !== Number(cp.seq) + 1)) {
      problem("checkpoint_chain_broken", `checkpoint ${no + 1} does not follow checkpoint ${no}`);
    }
  }

  let recordsChecked = 0;
  let entriesChecked = 0;
  for (const [index, raw] of text("records.jsonl", "").split("\n").entries()) {
    if (!raw.trim()) continue;
    let line: Json;
    try {
      line = JSON.parse(raw) as Json;
    } catch {
      problem("malformed_record", `records.jsonl line ${index + 1} is not JSON`);
      continue;
    }
    recordsChecked += 1;
    const entry = line.entry as Json | undefined;
    if (!entry) continue;
    entriesChecked += 1;
    const seq = Number(entry.seq);
    let ok = false;
    try {
      ok =
        entry.hash_scheme === "jcs-1" &&
        jcsHash(line.record) === entry.record_hash &&
        chainEntryHash(entry) === entry.entry_hash;
    } catch {
      ok = false;
    }
    if (!ok) {
      problem("modified_entry", `record ${String(entry.record_id)} (entry ${seq}) does not match its chain entry: it was changed`, seq);
      continue;
    }
    const proof = (line.proof ?? {}) as Json;
    const cp = byNo.get(Number(proof.checkpoint_no));
    if (!cp) {
      problem("proof_invalid", `entry ${seq} points at a checkpoint the bundle does not hold`, seq);
      continue;
    }
    const start = Number(cp.from_seq);
    const end = Number(cp.seq);
    let fits = false;
    try {
      const root = merkleRootFromProof(
        Buffer.from(String(entry.entry_hash), "hex"),
        seq - start,
        end - start + 1,
        ((proof.audit_path ?? []) as string[]).map((h) => Buffer.from(h, "hex")),
      );
      fits =
        start <= seq &&
        seq <= end &&
        Number(proof.leaf_index) === seq - start &&
        Number(proof.tree_size) === end - start + 1 &&
        root !== null &&
        root.toString("hex") === cp.merkle_root;
    } catch {
      fits = false;
    }
    if (!fits) problem("proof_invalid", `entry ${seq} is not proven to be in checkpoint ${String(cp.checkpoint_no)}`, seq);
  }
  const counts = (manifest.counts ?? {}) as Json;
  if (counts.records !== undefined && Number(counts.records) !== recordsChecked) {
    problem("record_count_mismatch", `the manifest lists ${String(counts.records)} record(s) but the bundle holds ${recordsChecked}`);
  }
  if (recordsChecked === 0) problem("empty_bundle", "the bundle holds no records");
  if (((manifest.context ?? {}) as Json).legacy_signatures) {
    notes.push("The original format-1 KMS signature is carried in context.legacy_signatures; check it with the Python SDK.");
  }
  if (pins.size > 0 && trusted === 0 && !problems.some((p) => p.kind === "bundle_signature_invalid")) {
    const untrusted = signatures.filter((s) => s.status === "untrusted").map((s) => s.keyId);
    if (untrusted.length > 0) problem("unexpected_key", `signed only by key(s) you did not pin: ${untrusted.join(", ")}`);
    else problem("no_trusted_signature", "no signature by an expected key was found");
  }
  if (pins.size === 0) notes.push("No key was pinned: compare the signing key ids with the ones the record keeper published.");
  return {
    intact: problems.length === 0,
    formatVersion: BUNDLE_FORMAT_VERSION,
    subject: String(manifest.subject),
    recordsChecked,
    entriesChecked,
    checkpointsChecked: checkpoints.length,
    signatures,
    problems,
    notes,
  };
}
