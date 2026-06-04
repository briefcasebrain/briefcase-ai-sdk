# Proposal — Connecting & distributing the Briefcase evaluation-run stack

- **Status:** Draft / for review
- **Date:** 2026-06-04
- **Scope:** `briefcase-ai-sdk` (Python+Rust), `oci-jj` (Rust/Go/Python engine), `verdictml` (private),
  `briefcase-ai-sdk-docs`, `telemetry-sdk-examples`
- **Decision drivers (agreed):** connect via a gRPC client **with no generated/checked-in stubs**,
  fully compatible with the live oci-jj contract; distribute the engine as **published images +
  `briefcase stack`**; this document is **proposal-only** (no code changes yet).

---

## 1. Summary

The `briefcase` evaluation-run CLI (shipped in `briefcase-ai`) currently reaches the oci-jj engine by
shelling out to an `oci-jj` binary and a `docker compose` checkout. Both are *implicit* couplings to a
repo that has **no release pipeline and no published artifacts**. This proposal replaces those seams
with two stable boundaries that already exist in the design:

1. **The proto contract**, consumed at runtime via **gRPC server reflection** — no `*_pb2` stubs to
   generate, vendor, or keep in sync. The Python client discovers the server's schema live, so it is
   forward-compatible by construction.
2. **Published, versioned container images** pulled by a new `briefcase stack` command, with the
   private-beta `verdict-worker` delivered as a **prebuilt private image** (pull-token access) rather
   than a build-time source clone.

A small **compatibility manifest** binds the independently-released repos together.

---

## 2. Current state — the four seams

| Seam | Today | Fragility |
| --- | --- | --- |
| SDK façade → `oci-jj` **binary** | `subprocess`, assumes `oci-jj` on `PATH` (`briefcase/cli/engine.py`) | oci-jj has only `ci.yml` — **no release, no published binary, no cargo-dist**. Users must `cargo build`. Output is parsed as text. |
| SDK façade → **engine stack** | `docker compose` against an oci-jj checkout (`briefcase/cli/commands.py:cmd_run_logs`) | Dockerfiles exist (`oci-jj/docker/*.Dockerfile`) but **images are never pushed**; compose *builds* them. Requires cloning oci-jj. |
| verdict worker → **verdictml** | private git dep pinned `@v0.1.0`, cloned at **image build** via `VERDICTML_SPEC` build-arg secret | Build-time token; beta users still need source access and a local image build. |
| SDK / docs / examples → **versions** | four independent version lines (`briefcase-ai 3.2.2`, oci-jj M-series, `verdictml v0.1.0`) | No compatibility contract; nothing pins what works with what. |

**Assets to build on:** the SDK already has a mature **tag-driven publish pipeline**
(`.github/workflows/publish.yml`: `briefcase-core` → crates.io, abi3 wheels + sdist → PyPI); the proto
is the **single source of truth** (oci-jj ADR-0005); and oci-jj already proves Python can speak the
contract (the verdict worker, ADR-0015).

---

## 3. Goals / non-goals

**Goals**
- `pip install 'briefcase-ai[run]'` → `briefcase stack up` → `briefcase run submit …` with **no oci-jj
  checkout and no Rust toolchain**.
- A client that stays **compatible with an evolving server** without regeneration.
- Private-beta `verdictml` consumable **without source access** (pull a prebuilt image).
- An explicit, machine-checkable **compatibility contract** across the four repos.

**Non-goals (now)**
- A hosted/managed control plane (future).
- Replacing the `oci-jj` CLI for human use — it remains the engine's native CLI; this is about the
  *programmatic* SDK seam.
- Changing the data model, proto semantics, or verdictml.

---

## 4. Target architecture

```
 pip install 'briefcase-ai[run]'
        │
        ▼
 briefcase CLI ───(Layer 1: gRPC + reflection, no stubs)──▶ oci-jj-server ──▶ verdict_queue ──▶ verdict-worker (verdictml)
        │                                                         ▲
        └──(Layer 2: briefcase stack → docker compose)───────────┘
              pulls PUBLISHED images (GHCR), pinned by
              (Layer 3) compat.json shipped in the wheel
```

Each repo keeps its own toolchain and release cadence; they connect only through the **proto contract**
and **image tags**, bound by the **compat manifest**.

---

## 5. Layer 1 — gRPC client with no generated stubs

### 5.1 Approach: dynamic client over server reflection

Instead of running `protoc`/`grpcio-tools` to emit `*_pb2.py` / `*_pb2_grpc.py` (checked-in artifacts
that drift and pin a proto revision), the SDK uses a **dynamic gRPC client**:

- **Server side (oci-jj):** enable the gRPC **reflection service** (`tonic-reflection`) on
  `oci-jj-server`, registering the `FileDescriptorSet` for `vcs`, `ingest`, `telemetry`, `common`,
  `chunk`. *This is the one required server change — reflection is not enabled today.*
- **Client side (SDK):** use `grpcio` + `grpc-reflection` to fetch the server's `FileDescriptorProto`s
  at connect time, build **dynamic message classes** via `google.protobuf.descriptor_pool` +
  `message_factory`, and invoke methods with `channel.unary_unary(...)` using the descriptor-derived
  request/response serializers. No code generation, nothing vendored.

The client caches the resolved descriptors per endpoint (keyed by the server's reported version) so
reflection happens once per process.

### 5.2 Why this satisfies "fully compatible, no stub"

- **No stub to drift.** The schema is read from the *running* server, so the client always matches
  what the server actually serves — honoring ADR-0005 (proto = single source of truth) at *runtime*,
  not at some past build.
- **Forward-compatible by protobuf rules.** Additive changes (new fields, new RPCs) are absorbed with
  no client change; the client only reads the fields it knows. A declared **minimum API version**
  (see Layer 3) guards against removals/renames.
- **No build dependency.** No `grpcio-tools` at install time, no generated files in the repo, no
  per-release codegen step.

### 5.3 Client design in the SDK (drop-in for the current engine)

Introduce `briefcase/cli/grpc_engine.py: GrpcEngine` implementing the **same interface** the façade
already depends on (`briefcase/cli/engine.py`), so `commands.py` is unchanged:

| Façade verb | Today (subprocess) | Proposed (gRPC, dynamic) |
| --- | --- | --- |
| `run submit --mode gate` | `oci-jj attach-bench … --run` | `VcsService.AttachBench` (enqueues a verdict job) |
| `run submit --mode hunt` | `oci-jj hunt …` | `VcsService.Hunt` |
| `run results` | `oci-jj diff … --depth bench` | `VcsService.Diff(depth=BENCH)` |
| `run inspect` | `oci-jj provenance` | `VcsService.Provenance` / `Log` |
| `run list` | local registry | local registry + `VcsService.Log` |

`engine.py` stays as a **fallback**: if reflection isn't reachable (older server, locked-down network),
fall back to the `oci-jj` binary if present. Selection via `BRIEFCASE_ENGINE={grpc,cli,auto}` (default
`auto`). The existing `argv_*` builders are retained for that fallback and remain unit-tested.

### 5.4 Packaging

- `briefcase-ai[run]` gains `grpcio` + `grpc-reflection` (or `googleapis-common-protos` for the
  descriptor pool). No `grpcio-tools` (runtime needs no codegen).
- Tests: the dynamic client is unit-tested against an in-process tonic/grpc reflection stub or a
  recorded `FileDescriptorSet` fixture; the RPC-mapping layer is tested with a fake channel — same
  injection style as the current `OciJJEngine(runner=…)`.

### 5.5 Alternative considered (rejected)

Ship the `.proto` files in the wheel and compile them at **runtime** with `grpcio-tools`. Avoids
checked-in stubs but (a) adds a heavy build dep, (b) keeps a *copy* of the proto that can lag the
server, and (c) still pins a proto revision. Reflection avoids all three.

### 5.6 Transport choice — reflection now, Connect/JSON as a swappable upgrade (recommended)

Two stub-free transports can satisfy "no generated code, fully compatible":

| | **A. gRPC + reflection** (recommended now) | **B. Connect / gRPC-Gateway JSON-over-HTTP** |
| --- | --- | --- |
| Client deps | `grpcio` + `grpc-reflection` (C-extension) | `httpx` only — **zero gRPC deps** |
| Server change | `tonic-reflection` — official, small | JSON shim on tonic (axum) or `connect-rust` — larger, less mature in Rust |
| Streaming (`Log`/`Diff`/`Search`) | native server-streaming | needs Connect streaming / SSE framing |
| Debuggability | `grpcurl` | `curl`-able JSON |
| Stub-free? | yes (live descriptors) | yes (plain JSON) |
| Maturity on a **tonic** server | high | low–medium |

**Recommendation: ship A (reflection) first, behind a swappable transport seam so B can drop in later.**
Reflection is the smallest, best-supported *server* change (official `tonic-reflection`), preserves
native streaming for `Log`/`Diff`/`Search`, and matches the agreed gRPC direction; the `grpcio` client
dependency is the accepted cost. The risk of B today is entirely server-side — the Rust Connect/JSON
story on a tonic server is immature, so betting the first release on it is unwarranted.

To keep B as a cheap future option, **abstract the wire format** behind a `Transport` interface in
`GrpcEngine` — one method per RPC shape (`unary(service, method, request) -> response`,
`server_stream(service, method, request) -> iterator`) — so the verb mapping in `commands.py` never
sees the transport. Then B becomes a drop-in: if removing `grpcio` becomes a priority, add a
Connect-compatible JSON endpoint to oci-jj-server plus an `httpx` `Transport`, with **no change to the
façade or the verb layer**. The decision is therefore *sequenced, not forked*: reflection now, a
zero-dep JSON transport later if and when the Rust Connect tooling is worth adopting.

**Decision on the `grpcio` dependency: defer removal; ship reflection now.** The cost of `grpcio`
falls only on `[run]` users, who already run Docker and the oci-jj stack — a prebuilt C-extension wheel
is negligible there, and it buys native streaming for free. The JSON alternative's cost is entirely
*server-side* (an immature Rust Connect crate or a hand-maintained axum JSON↔proto shim — a second
contract surface that can drift), and the rest of the system is already gRPC end-to-end (ADR-0005,
ADR-0015). The `Transport` seam makes reversal cheap, so this is sequenced, not forked.

**Pull the JSON transport forward only on a concrete trigger:**
1. **`protobuf` conflicts in real ML environments.** `grpcio` drags in `protobuf`; Briefcase's
   AI/ML users may pin `protobuf` for other libraries (e.g. TensorFlow). If real conflict reports
   appear, that's the signal — `httpx`/JSON has no `protobuf` dependency. (Mitigated today by keeping
   `grpcio` in `[run]` only, and because the eval stack is usually run separately from a training env.)
2. **oci-jj commits to a first-class HTTP/JSON API** for other consumers (web console, the cloud
   endpoint in §10.3, third-party integrations). Then JSON is shared infra, not SDK-only, and the SDK
   should ride it — still behind the same seam.

Absent either trigger, removing `grpcio` is a premature, server-heavy investment against a hypothetical.

---

## 6. Layer 2 — Engine distribution: published images + `briefcase stack`

### 6.1 Publish versioned images (oci-jj)

Add a tag-driven release workflow to oci-jj (companion to the SDK's `publish.yml`) that builds and
pushes the existing Dockerfiles to **GHCR** with the release tag:
`ghcr.io/briefcasebrain/oci-jj-{server,gateway,reconstructor,workers}` (public) and
`ghcr.io/briefcasebrain/oci-jj-verdict-worker` (**private**). The compose file moves from
`build:` contexts to `image:` references.

### 6.2 `briefcase stack` in the SDK

Ship a **pinned** `docker-compose.yml` as package data inside the wheel (e.g.
`briefcase/cli/stack/docker-compose.yml`) referencing published image tags, and add commands:

```
briefcase stack up        # docker compose up -d against the bundled, pinned compose
briefcase stack down
briefcase stack status
briefcase stack logs [-f] [service]
```

Now onboarding is: `pip install 'briefcase-ai[run]'` → `briefcase stack up` → `run_demo.sh`. No oci-jj
checkout, no `cargo build`, no local image builds. `cmd_run_logs` retargets to `briefcase stack logs
verdict-worker`.

### 6.3 Private-beta delivery (verdictml)

The `verdict-worker` image — with `verdictml` already baked in — is pushed to a **private** GHCR path.
Beta access becomes: request access → granted a **read-scoped pull token** → `docker login ghcr.io` →
`briefcase stack up` pulls it. This removes the build-time `VERDICTML_SPEC` token and the need for any
source access. If the user lacks beta access, `briefcase stack up` brings up everything *except*
`verdict-worker` and prints how to request access (runs enqueue but don't score — the honest gap we
already document).

---

## 7. Layer 3 — Compatibility across independently-released repos

Ship a small `compat.json` as package data in the SDK wheel:

```json
{
  "briefcase_ai": "3.3.0",
  "oci_jj_api_min": "v1",
  "oci_jj_images": "ghcr.io/briefcasebrain/oci-jj-*:M2.1",
  "verdictml": "v0.1.0"
}
```

- `briefcase stack up` pulls exactly these image tags; `GrpcEngine` checks the server's reported API
  version against `oci_jj_api_min` and refuses (with guidance) on a mismatch.
- Add a `briefcase doctor` command: verifies Docker, image pull access (incl. beta), server
  reachability/version, and prints the resolved matrix.
- **Docs:** a new install/compat-matrix page in `briefcase-ai-sdk-docs` (Reference group), generated
  from or kept in sync with `compat.json`.
- **Examples:** `telemetry-sdk-examples/examples/evaluation-run/` pins `pip install
  'briefcase-ai==X'` and relies on `briefcase stack up` for the matching engine.
- **Release choreography:** oci-jj cuts images first → SDK bumps `compat.json` + releases → docs/examples
  follow. Documented so the four lines never silently diverge.

---

## 8. Rollout plan

| Phase | Repo | Work | Exit criteria |
| --- | --- | --- | --- |
| 1 | oci-jj | Enable `tonic-reflection` on `oci-jj-server`; companion ADR | `grpcurl -plaintext … list` returns the services |
| 2 | SDK | `GrpcEngine` (dynamic/reflection) behind `BRIEFCASE_ENGINE=auto`; `[run]` deps; tests | `briefcase run submit` works with no `oci-jj` binary; subprocess fallback still green |
| 3 | oci-jj | Image-publish release workflow → GHCR (public + private verdict-worker); compose `build:`→`image:` | `docker pull` of each tag succeeds; private one needs auth |
| 4 | SDK | Bundled pinned compose + `briefcase stack {up,down,status,logs}`; retarget `run logs` | `pip install` → `stack up` → `run_demo.sh` end-to-end, no checkout |
| 5 | SDK + docs + examples | `compat.json`, `briefcase doctor`, compat-matrix page, example/version pins | `briefcase doctor` green; docs build; example runs against pulled images |

Phases 1–2 deliver the highest-value seam (no binary, no Rust) without touching distribution; 3–4 deliver
the one-command UX; 5 makes it durable.

---

## 9. Risks & mitigations

- **Reflection disabled / blocked in some deployments** → keep the subprocess `oci-jj` fallback
  (`BRIEFCASE_ENGINE=cli`); `briefcase doctor` reports which path is active.
- **Dynamic-message ergonomics / performance** → descriptors cached per endpoint; the verb surface is
  small (≈5 RPCs); only ~1 KB verdict JSON crosses the wire (ADR-0019), so payloads stay tiny.
- **Proto removals/renames break the dynamic client** → `oci_jj_api_min` floor + additive-only proto
  discipline (already ADR-0005); CI contract test in oci-jj asserts no breaking changes within a major.
- **GHCR private-image access friction** → `briefcase doctor` checks pull access and prints the exact
  `docker login` + access-request steps; stack still comes up without the scorer.
- **Cross-repo version skew** → `compat.json` + release choreography + `doctor` make mismatches loud.

---

## 10. Open questions

1. **Reflection vs. a Connect/JSON endpoint — decided in §5.6:** reflection now, behind a swappable
   `Transport` seam; `grpcio` removal is **deferred** and gated on two explicit triggers (real
   `protobuf` conflicts in ML user environments, or oci-jj committing to a first-class HTTP/JSON API).
   No remaining sub-question unless one of those triggers fires.
2. Registry choice — GHCR (assumed, matches the `briefcasebrain` org) vs. a dedicated registry for the
   private beta.
3. Does `briefcase stack` target only the OSS compose, or also a future cloud endpoint via the same
   command (`briefcase stack use <profile>`)?

---

## 11. Where changes land (for the eventual PRs)

- **oci-jj:** `rust/crates/oci-jj-server/` (reflection); `.github/workflows/release.yml` (images);
  `docker-compose.yml` (`build:`→`image:`); new ADRs (e.g. `docs/adr/00NN-grpc-reflection-for-sdk.md`,
  `00NN-image-publishing.md`) in the repo's existing ADR format.
- **briefcase-ai-sdk:** `briefcase/cli/grpc_engine.py` with a `Transport` seam (§5.6; reflection
  transport now, `httpx`/JSON transport later) (+ tests in `tests/cli/`), `briefcase/cli/stack/`
  (bundled compose + `stack`/`doctor` commands), `compat.json`, `pyproject.toml` (`[run]` deps +
  package-data), `engine.py` retained as fallback.
- **briefcase-ai-sdk-docs:** install/compat-matrix page; update `evaluate/architecture.mdx` and
  `evaluate/quickstart.mdx` to lead with `briefcase stack up` (drop the manual `make up`/`cargo build`
  path to an "advanced / from source" note).
- **telemetry-sdk-examples:** pin versions; replace the `make up`/`make seed` prerequisites with
  `briefcase stack up` (keep the from-source path as an appendix).
