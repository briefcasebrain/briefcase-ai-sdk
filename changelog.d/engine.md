### Added

- `briefcase engine install|up|down|status|logs|doctor|upgrade|uninstall`: install the licensed
  Briefcase engine (oci-jj) with an account licence token from the platform
  (`POST /v1/engine/credentials`): sha256-checked `oci-bai` client, images pulled by digest with a
  short-lived credential, a digest-pinned single-box compose with generated secrets, required
  gRPC auth and an optional `--tls`.
- `briefcase graph log|diff|search|provenance|whodepends|show|explain|resolve|referrers|optimize`
  (Review) and `briefcase env push|cohort|compose|checkout|mount|bookmark|undo|gc` (Capture).
- `briefcase run attach ID --result verdict.json`, `briefcase dataset publish NAME FILE`
  (tenant-scoped `s3://` dataset in the local engine), `briefcase verify --graph REF` (engine fsck).
- The `engine` extra (`grpcio`, `protobuf`) and the vendored Apache-2.0 gRPC contract in
  `briefcase/engine/_proto` (regenerate with `scripts/engine/regen_proto.py`).
- Engine commands are listed in `--help`, `briefcase features` and `briefcase status` without an
  install, marked "engine not installed — `briefcase engine install`", and exit 3 when run.

### Changed

- The gRPC client builds messages from the vendored contract instead of server reflection
  (`BRIEFCASE_GRPC_REFLECTION=1` keeps reflection) and finds a local engine install on its own.
- `briefcase secret` says what it is for: the engine uses only `OCI_JJ_AUTH_TOKEN`.
- `run submit` says "was not submitted" when the engine refuses a run.

### Deprecated

- `briefcase stack`: use `briefcase engine` (one release).
