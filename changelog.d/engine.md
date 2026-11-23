### Added

- `briefcase engine install|up|down|status|logs|doctor|upgrade|uninstall`: install the licensed
  Briefcase engine (oci-jj) from the gated portal (https://observe.briefcasebrain.io/engine):
  `engine install --code bci_...` (single-use install code), `--token bcl_...` (automation), or
  offline `--bundle FILE --manifest FILE` (`docker load`, `skopeo` fallback, no network). The
  online flow calls `POST /v1/engine/install`, downloads from presigned URLs, logs in to ECR with
  a short-lived password on stdin, pulls every image by digest from the signed manifest and logs
  out. A digest-verified single-box compose with generated secrets, required gRPC auth and an
  optional `--tls`.
- Release verification: manifest v2 (`schema_version`, `key_id`, `min_sdk_version`, files with
  sha256/size/signature, images by digest) signed with an ECDSA P-256 KMS key via
  `cosign sign-blob --tlog-upload=false`, checked in Python with `cryptography` (or
  `cosign verify-blob --key` without it) against release keys pinned in
  `briefcase/engine/keys/release-*.pub`. No override exists. This build ships no key yet, so
  install refuses with "no trusted release key is bundled in this SDK build".
- `briefcase engine verify FILE [--signature S] [--manifest M]`: check a manually downloaded file
  against the pinned key; prints the key id.
- `engine status` shows the key id the install was verified with and, given a licence token,
  whether a newer release exists (`GET /v1/engine/index`). `engine up`/`doctor` refuse an image
  whose local tag no longer points at the image verified at install.
- Releases ship `oci-bai` for Linux only; on macOS (or any OS without a build) the install still
  succeeds and the commands that need `oci-bai` run it from the verified server image with
  `docker run`. A verified macOS build, should one ship, has `com.apple.quarantine` removed only
  after verification.
- `briefcase graph log|diff|search|provenance|whodepends|show|explain|resolve|referrers|optimize`
  (Review) and `briefcase env push|cohort|compose|checkout|mount|bookmark|undo|gc` (Capture).
- `briefcase run attach ID --result verdict.json`, `briefcase dataset publish NAME FILE`
  (tenant-scoped `s3://` dataset in the local engine), `briefcase verify --graph REF` (engine fsck).
- The `engine` extra (`grpcio`, `protobuf`, `cryptography`) and the vendored Apache-2.0 gRPC
  contract in `briefcase/engine/_proto` (regenerate with `scripts/engine/regen_proto.py`).
- Engine commands are listed in `--help`, `briefcase features` and `briefcase status` without an
  install, marked "engine not installed — `briefcase engine install`", and exit 3 when run.

### Changed

- The gRPC client builds messages from the vendored contract instead of server reflection
  (`BRIEFCASE_GRPC_REFLECTION=1` keeps reflection) and finds a local engine install on its own.
- `briefcase secret` says what it is for: the engine uses only `OCI_JJ_AUTH_TOKEN`.
- `run submit` says "was not submitted" when the engine refuses a run.

### Removed

- The v1 engine distribution paths: `POST /v1/engine/credentials`, GHCR / registry-mirror /
  registry-token login, GitHub release-asset downloads, and cosign keyless identity checks.

### Deprecated

- `briefcase stack`: use `briefcase engine` (one release).
