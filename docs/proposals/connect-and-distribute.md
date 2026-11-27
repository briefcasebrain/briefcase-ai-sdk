# Managed evaluation client design

The SDK connects to the licensed engine through a Briefcase-operated service. The original image-distribution proposal has been retired. Current access and legacy migration procedures are in the [engine guide](../features/engine.md).

## gRPC client with no generated stubs

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
