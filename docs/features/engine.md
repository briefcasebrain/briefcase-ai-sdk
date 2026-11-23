# The Briefcase engine (oci-jj): install and run it

The engine stores container environments as a versioned, content-addressed graph and scores
evaluation runs against them. It is licensed separately: this package holds only its open gRPC
contract, the client, and the release key it trusts; entitled accounts get the engine itself from
the gated Briefcase portal, and `briefcase-ai engine install` verifies everything before installing it.

## The one command

Get the engine from **https://observe.briefcasebrain.io/engine**. If your account is not
entitled yet, that page has a "Request access" button. If it is, pick a version and copy the
one-command install from the "Install with one command" box:

```bash
briefcase-ai engine install --code bci_...     # single use, valid for 15 minutes
```

Real output (the CLI against the test fixtures: a local fake platform and a fake `docker`,
release signed with a throwaway key pinned for the run; `$E` is a temporary directory):

```text
$ briefcase-ai engine install --code bci_good --yes
Install the engine from http://127.0.0.1:PORT:
  - POST http://127.0.0.1:PORT/v1/engine/install: redeem the install code (single use)
  - download engine-manifest.json + .sig and verify them against the pinned release key
  - download the oci-bai build for linux/x86_64, check size, sha256 and signature -> $E/home/.briefcase/engine/<version>/bin/oci-bai
  - download the image bundle for x86_64, check its size, sha256 and signature
  - docker load the bundle (skopeo if docker cannot), check every image (<each engine image>) is the one the manifest pins
  - tag the images localhost/briefcase-engine/<name>:<version>
  - write $E/home/.briefcase/engine/<version>/docker-compose.yml
  - generate secrets once in $E/home/.briefcase/engine/engine.env (mode 0600)
  - record the install in $E/home/.briefcase/engine/installed.json
manifest for engine 0.4.0 verified (key sha256:e3c42cbf051c1b65a5317f85c31cddc5027bf4cc6bf57fa3be25b8eadfb52c25)
oci-bai-0.4.0-linux-x86_64.tar.gz verified (signature and sha256)
engine-images-0.4.0-x86_64.tar verified (signature and sha256)
loaded 5 images with docker load
engine 0.4.0 installed in $E/home/.briefcase/engine/0.4.0 (verified with key sha256:e3c42cbf051c1b65a5317f85c31cddc5027bf4cc6bf57fa3be25b8eadfb52c25)
  client: $E/home/.briefcase/engine/0.4.0/bin/oci-bai
Next: briefcase-ai engine up
```

Install codes work once. If anything fails after the code was redeemed, get a new code from the
portal rather than re-running the same command (verified downloads are reused):

```text
$ briefcase-ai engine install --code bci_good --yes
...
error: the install code was already used; install codes work once, for 15 minutes
Fix: Get a new install code at https://observe.briefcasebrain.io/engine
```

For automation and CI, use a licence token (platform: Engine page, Licence tokens for CI and servers) instead of a code:
`BRIEFCASE_LICENSE_TOKEN=bcl_... briefcase-ai engine install --yes [--version X]`.

### Offline (air-gapped)

Download from the portal, on a connected machine: `engine-manifest.json`,
`engine-manifest.json.sig`, the bundle `engine-images-<version>-<arch>.tar` and its `.sig`, and
optionally `oci-bai-<version>-<os>-<arch>.tar.gz` and its `.sig`. Copy them into one directory on
the target and run:

```text
$ briefcase-ai engine verify $E/downloads/engine-images-0.4.0-x86_64.tar --manifest $E/downloads/engine-manifest.json
verified: engine-images-0.4.0-x86_64.tar (engine 0.4.0)
  key:     sha256:de9422895b1cccf02f33e73c4908584b733c8b5f308e2d10f552a5e764a82c37 (release-de9422895b1cccf0.pub)
  checked: size, sha256, signature
Next: briefcase-ai engine install --bundle FILE --manifest engine-manifest.json  # install a verified offline bundle
$ briefcase-ai engine install --bundle $E/downloads/engine-images-0.4.0-x86_64.tar --manifest $E/downloads/engine-manifest.json --yes
Install engine 0.4.0 from the offline bundle:
  - verify $E/downloads/engine-manifest.json and $E/downloads/engine-images-0.4.0-x86_64.tar against the pinned release key (no network)
  - download oci-bai-0.4.0-linux-x86_64.tar.gz, check size, sha256 and signature -> $E/home/.briefcase/engine/0.4.0/bin/oci-bai
  - docker load the bundle (skopeo if docker cannot), check every image is the one the manifest pins
  ...
manifest for engine 0.4.0 verified (key sha256:de94...); bundle signature verified
oci-bai-0.4.0-linux-x86_64.tar.gz verified (signature and sha256)
loaded 5 images with docker load
engine 0.4.0 installed in $E/home/.briefcase/engine/0.4.0 (verified with key sha256:de94...)
Next: briefcase-ai engine up
```

Nothing goes over the network. The binary is picked up next to the manifest (or `--binary FILE`);
the manifest signature defaults to `engine-manifest.json.sig` (or `--signature FILE`). The bundle
is an OCI image layout that also carries Docker's `manifest.json`, loaded with `docker load`; if
that fails and `skopeo` is installed, each image is copied with `skopeo copy oci-archive:...`
instead. Each loaded image must have the ID the signed manifest allows (the manifest digest on
Docker's containerd image store, the bundled config digest on the classic store); it is then
tagged `localhost/briefcase-engine/<image>:<version>` and the bundle's own
`briefcase-engine/<image>:<version>` tag is removed. A plain `docker save` tarball is accepted
too, matched by image ID.

### Verifying a download by hand

`briefcase-ai engine verify FILE [--signature FILE.sig] [--manifest engine-manifest.json]` checks any
file from the portal against the release key pinned in this SDK and prints the key id; exit 0
when it verifies, 1 when it does not. The portal shows the same key id.

Then `briefcase-ai engine up`, `briefcase-ai engine status`, and the engine commands:
[graph.md](graph.md) (Review) and [env-artifacts.md](env-artifacts.md) (Capture). Runs
(`briefcase-ai run submit|attach|results`) and `briefcase-ai dataset publish` are below.

| Subcommand | What it does |
| --- | --- |
| `engine install --code C \| --token T [--version X] \| --bundle F --manifest M [--signature S] [--binary B]`, plus `[--platform-url URL] [--tls] [--no-images] [--dry-run] [--yes]` | The flows above. Idempotent: verified files and images are reused; an interrupted download resumes. `--dry-run` never redeems a code. |
| `engine verify FILE [--signature S] [--manifest M]` | Verify a downloaded file against the pinned release key. |
| `engine up [--wait]` / `down [--volumes]` / `logs [SERVICE] [-f]` | `docker compose` on the generated file, project `briefcase-engine`. |
| `engine status [--json] [--token T]` | What is installed, the key it was verified with, which services run, whether gRPC answers, and (with a licence token) whether a newer release exists. Exit 3 when nothing is installed. |
| `engine doctor [--json]` | Docker, compose, the pinned keys, the stored manifest's signature, each image against the ID verified at install, the secrets file mode, and one authenticated gRPC call. |
| `engine upgrade --code C \| --token T [--version X]` | Installs the newest release (or the code's) and restarts onto it when the engine is running; the release becomes the current one only after the restart guard passed and `compose up` succeeded, so a refused or failed upgrade can simply be run again. The restart and the switch happen under one lock, any docker error (it cannot say what runs, `compose up` fails) fails the command and leaves the old release current, and an upgrade keeps the installed TLS setting unless you pass `--tls` or `--no-tls`. Secrets and data volumes are kept. |
| `engine uninstall [--purge] [--dry-run]` | Removes the images and the version directory, and takes the stack down only when that release is the one running (releases share one Compose project, so "running" means every first-party container runs this release's recorded images, with the Compose file as tie-break; a mix, an unrecognised engine or two releases that fit are "unknown" and block). If docker cannot say what runs, or `compose down` fails, nothing is deleted and the command exits 1. `--purge` also deletes the data volumes and then the secrets and certificates, but only on the Docker daemon the install used (its context is recorded) and when no other release is installed and every engine volume is positively confirmed absent. Docker missing, failing or refusing to inspect counts as "unknown" and blocks (`--force-without-docker` is accepted only for an install whose record proves no volume was ever created). When it blocks, the secrets and the install record are kept and the command exits 1; other engine volumes may already have been removed, and the same command can be run again. |

## The signed release index

`engine status` (with a licence token) learns the newest version from `releases/index.json`,
which is a single object, not a file plus a `.sig`. (`engine upgrade` gets its release from the install API and
verifies that release's manifest; it does not read the index.)

```json
{"schema_version": 2, "key_id": "sha256:<hex>", "payload": "<the index document, as JSON text>", "signature": "<base64>"}
```

`signature` is the base64 DER ECDSA P-256 signature, over the SHA-256 of the `payload` string's UTF-8 bytes, of
the release key named by `key_id`. The SDK takes the key from the ones it ships (an unknown `key_id` is refused),
verifies the signature over the payload string exactly as it appears in the envelope, and only then parses it as
the index (its own `key_id` must match). A bad signature, an unknown key or a malformed envelope is a trust
error and nothing in the payload is used. `briefcase-ai engine verify index.json` checks a downloaded copy the
same way; it needs no `.sig`. Per-version manifests are unchanged.

## What it writes or sends, and where

| What | Where |
| --- | --- |
| Install code (in the body) or licence token (bearer), `{os, arch, version}` | `POST <platform>/v1/engine/install`. Nothing about your machine beyond OS and architecture. |
| Licence token (bearer), only for `engine status` with a token | `GET <platform>/v1/engine/index` (newest version). It answers with one presigned `index_url`; that object is the whole signed index (see below). |
| Downloads | presigned S3 URLs from the platform (5-minute expiry), fetched with no credentials |
| Verified release manifest and signature | `~/.briefcase/engine/<version>/engine-manifest.json(.sig)` |
| `oci-bai` client binary | `~/.briefcase/engine/<version>/bin/oci-bai` (download kept in `<version>/download/`) |
| Compose file | `~/.briefcase/engine/<version>/docker-compose.yml` |
| Secrets: Postgres superuser and `oci_jj_app` passwords, MinIO root credentials, the internal worker token, your tenant and read-only tokens, dedup HMAC key | `~/.briefcase/engine/engine.env`, mode 0600, generated once and reused by every upgrade |
| Install record (version, key id, image digests and IDs) | `~/.briefcase/engine/installed.json` |
| Images | Docker's image store, tagged `localhost/briefcase-engine/<name>:<version>`. The downloaded bundle stays in `~/.briefcase/engine/<version>/download/` so a re-run reuses it. |

The stack is oci-jj's single-box layout: Postgres + Apache AGE, MinIO, a backing registry, the
server, gateway, reconstructor and workers (plus the verdict worker when the release has it).
Ports are bound to 127.0.0.1 only: gRPC on 50051, the registry gateway on 8080. It follows the
engine's security model (oci-jj e89ddbf, 8a52297, be043d9):

- gRPC authentication is required. `OCI_JJ_AUTH_TOKENS` holds an internal worker token (admin
  role, given only to `workers` and `verdict-worker`), your tenant token (tenant `local`) and a
  read-only token. The CLI sends the tenant token automatically.
- The gateway and reconstructor hold no credential; they forward the caller's token. `env push`
  logs in to the gateway with your tenant token and pushes to `local/<image>` (your registry
  namespace), then logs out.
- The `oci_jj_app` (row-level security) role gets its password from `OCI_JJ_APP_PASSWORD` on the
  first boot of the database; nothing is hard-coded.
- Verdict datasets live in `s3://oci-jj-datasets/tenants/local/...`; the server presigns a URL
  per job and the verdict worker holds no object-store credentials. (The server presigns with
  the MinIO root key, as oci-jj's dev compose does; its single-box guide uses a read-only key.)

## Defaults and how to change them

| Setting | Default | Change with |
| --- | --- | --- |
| Engine address for `run`, `graph`, `env`, `verify --graph` | the local install (`http://127.0.0.1:50051`) | `--server`, `BRIEFCASE_ENGINE_ADDR`, or `OCI_JJ_SERVER` |
| Bearer token | the local install's tenant token | `OCI_JJ_AUTH_TOKEN` (also storable with `briefcase-ai secret set OCI_JJ_AUTH_TOKEN`) |
| TLS | off; plaintext on loopback (`OCI_JJ_ALLOW_PLAINTEXT=1` inside the stack) | `engine install --tls` (self-signed certificate under `~/.briefcase/engine/tls`, needs `openssl`); `OCI_JJ_TLS_CA` for an engine elsewhere |
| Release | newest (with `--token`); the code's version (with `--code`) | `--version X` with `--token`, or pick it in the portal |
| Platform | `https://observe.briefcasebrain.io` | `--platform-url`, `BRIEFCASE_PLATFORM_URL` |
| Repository | `rl-gym-env` | `--repo` / `OCI_JJ_REPO` |

## How to turn it off or undo it

`briefcase-ai engine down` stops it; `briefcase-ai engine uninstall` removes it;
`briefcase-ai engine uninstall --purge` also deletes the data. On the platform, revoking the
licence token or the account's entitlement stops new installs at once.

## Without an install

Every engine command is listed in `briefcase-ai --help`, `briefcase-ai features` and
`briefcase-ai status`, marked "engine not installed — `briefcase-ai engine install`". Running one
prints exactly that and exits with code **3** (`--json`: `{"error": "engine_not_installed"}`).
Real output in a fresh HOME:

```text
$ briefcase-ai graph log rl-gym-env
error: engine not installed — `briefcase-ai engine install`
$ echo $?
3
$ briefcase-ai status | grep -i engine
  off       Engine (oci-jj)      engine not installed — `briefcase-ai engine install`
  off       Engine environments  engine not installed — `briefcase-ai engine install`
  off       Artifact graph       engine not installed — `briefcase-ai engine install`
```

Setting `BRIEFCASE_ENGINE_ADDR` (or `OCI_JJ_SERVER`) to an engine run elsewhere counts as
installed for every command except `up`/`down`/`logs`/`uninstall`.

## Evaluation runs and datasets

| Command | Engine call |
| --- | --- |
| `run submit` (gate) | `VcsService.AttachBench` with `run=true`: queues a `verdict_gate` job; the job id is kept with the run |
| `run submit --mode hunt` | `VcsService.RunHunt` |
| `run attach ID --result verdict.json [--manifest m.json]` | `VcsService.AttachBench` with `run=false`: records a verdict computed elsewhere (`oci-bai attach-bench --result`) |
| `run inspect` / `run results` | `VcsService.Provenance` / `VcsService.Diff` with `depth=BENCH` |
| `run logs` | `docker compose logs verdict-worker` (there is no log RPC) |
| `dataset publish NAME FILE.parquet\|.csv` | Pipes the file into `mc` inside the local MinIO container (`docker compose exec -T minio mc pipe`) at `oci-jj-datasets/tenants/local/datasets/<sha256>/<file>`; registers `NAME -> s3://oci-jj-datasets/tenants/local/datasets/<sha256>/<file>`. No RPC: the engine validates the URI when a run is submitted |
| `dataset register/list`, `secret`, `run list/stop/delete` | none: local registries under `~/.briefcase` |
| `verify --graph REF` | `VcsService.Fsck` |

The client builds its messages from the vendored contract (`briefcase/engine/_proto`,
Apache-2.0, regenerate with `scripts/engine/regen_proto.py`). `BRIEFCASE_GRPC_REFLECTION=1`
uses server reflection instead, for an engine newer than the vendored contract.
`BRIEFCASE_ENGINE=cli` runs the installed `oci-bai` binary instead of gRPC.

What the engine does not have, and what the CLI does about it:

- **No secret channel for runs.** `briefcase-ai secret` stores values locally; only
  `OCI_JJ_AUTH_TOKEN` is used (as the bearer token). `secret list` marks every other key as
  kept locally, and `run submit` names the keys it did not send.
- **Datasets must be tenant-scoped.** The engine accepts only `synthetic://<name>` and
  `s3://<its dataset bucket>/tenants/<tenant>/...` (`file://` only in its insecure dev mode). There
  is no content-addressed `cas://` form: dataset bytes are not stored in the engine's CAS. `dataset
  publish` therefore works on a local install; for an engine run elsewhere, upload to its dataset
  bucket under your tenant prefix and use `dataset register NAME --uri s3://...`.
- **No stop RPC.** `run stop` only marks the run locally.

## Settings for an engine you run elsewhere

The CLI reads the same settings as oci-jj's own Rust and Go clients:

| Setting | Effect |
| --- | --- |
| `BRIEFCASE_ENGINE_ADDR`, `OCI_JJ_SERVER` or `--server` | Endpoint. `https://` or `grpcs://` turns on TLS |
| `OCI_JJ_AUTH_TOKEN` | Sent as `authorization: Bearer <token>` on every call |
| `OCI_JJ_TLS_CA` | PEM CA that replaces the system roots; also turns on TLS for an `http://` endpoint |
| `OCI_JJ_TLS_CLIENT_CERT`, `OCI_JJ_TLS_CLIENT_KEY` | Client identity for mTLS (both) |

A `tenant` token can submit and push, a `reader` token is refused on writes ("this token is
read-only"), and another tenant's refs are "unknown ref".

## Verification and the release key

Every engine release is signed with one ECDSA P-256 key, held by the release operator, as base64
DER ECDSA over the SHA-256 of each file: nothing goes to a public transparency log. The signed
`engine-manifest.json` (schema 2) names the key (`key_id` = `sha256:` + hex of the SHA-256 of the
DER public key), every file with its sha256, size and signature, and every image by digest.

`engine install` refuses, before touching Docker:

- a manifest whose `key_id` is not one of the keys in `briefcase/engine/keys/release-*.pub`,
  or whose signature does not verify with it;
- an unknown `schema_version`, or a `min_sdk_version` above this SDK;
- any file whose size, sha256 or signature does not match.

The image bundle is downloaded (or given with `--bundle`), its size, sha256 and signature are
checked against the signed manifest, and every loaded image ID is checked against the bundle's
OCI index; online and offline installs share that one path. The images are then tagged `localhost/briefcase-engine/<name>:<version>` and the
compose file uses those tags with `pull_policy: never` (a `docker load`-ed image has no registry
digest to reference); `engine up` and `engine doctor` refuse an image whose tag no longer points
at the ID verified at install.

Signatures are checked in Python with `cryptography` (in the `engine` extra). Without it the
installer runs `cosign verify-blob --key` instead; with neither, it refuses. There is no flag or
variable that skips verification.

**Key rotation.** A new key is first shipped in an SDK release (a new `release-<id>.pub` next to
the old one), and only then used to sign releases; new manifests raise `min_sdk_version` to that
SDK. Removing a key file in an SDK release stops trusting it. See `briefcase/engine/keys/README.md`.
This SDK build ships **no** key yet, so install refuses:

```text
$ briefcase-ai engine install --code bci_good --yes
error: no trusted release key is bundled in this SDK build; the engine cannot be installed until a release key is published
Fix: pip install -U 'briefcase-ai[engine]'   # once a release with the engine key is out
```

## Platforms without an `oci-bai` build

Releases ship `oci-bai` for linux x86_64 and aarch64 only (oci-jj 6d1db6a: the macOS build links
Apple frameworks and cannot be cross-compiled on the Linux release runners). On macOS, and on any
OS without a build, the install still succeeds and says once:

```text
no oci-bai build for darwin/aarch64 in engine 0.4.0: commands that need it (env cohort, run attach, run submit) run it from localhost/briefcase-engine/server:0.4.0 with `docker run`
```

Those commands then run `docker run --rm` of the verified server image with `--entrypoint
/usr/local/bin/oci-bai`, on the stack's network (`briefcase-engine_default`, server
`oci-jj-server:50051`), with host paths bind-mounted at the same absolute path and the token
passed by name (`-e OCI_JJ_AUTH_TOKEN`), never in argv. `briefcase-ai env cohort DIR --dry-run`
prints the exact command. Should a macOS build ever ship, it would be unsigned and not notarized:
after it verifies, the installer removes a `com.apple.quarantine` attribute if one is set (files
it downloads itself carry none).

## Live tests

- `tests/cli/test_engine_live.py`: `BRIEFCASE_ENGINE_LIVE_DOCKER=1` runs an offline install of a
  generated, signed OCI archive against a real Docker daemon. `BRIEFCASE_ENGINE_LIVE=1` (with
  `BRIEFCASE_PLATFORM_URL`, `BRIEFCASE_LICENSE_TOKEN` and an SDK build that carries the release
  key) runs the portal flow; `BRIEFCASE_ENGINE_LIVE_UP=1` also starts the engine and queries it.
- `tests/cli/test_ocijj_live.py`: the run commands against any running engine. Set
  `BRIEFCASE_OCIJJ_ADDR` (plus `OCI_JJ_AUTH_TOKEN`; `BRIEFCASE_OCIJJ_REPO`, `_CANDIDATE`,
  `_BASELINE`, `_READER_TOKEN` are optional). Push a baseline and a candidate that share a
  layer first (`briefcase-ai env push`). Each submit queues one real job: use a throwaway database.

## Limits and failure modes

- Install trusts only the release key shipped in `briefcase/engine/keys/`; with none there, it refuses.
- Presigned download links last 5 minutes; an install code lasts 15 minutes and works once.
- `engine install` needs Docker unless `--no-images` (client binary only).
- Third-party images (`minio`, `registry:2`) come from Docker Hub at the first `engine up` unless
  the release manifest and bundle carry them; an air-gapped host needs them in the bundle.

## What was verified

- Offline tests (`tests/cli/test_engine_install.py`, `test_engine_dataset.py`): a throwaway
  P-256 key signing fixture artifacts in the cosign blob format, a fake platform with
  presigned-style download paths and single-use codes, a fake `docker`/`skopeo`/`xattr` on PATH,
  and OCI layout bundles. The suite also runs with `cryptography` unimportable, through a fake
  `cosign` that verifies with `openssl`.
- Real: `docker load` of a generated OCI archive, image-ID checks and tagging, against private
  Docker 29.10 daemons with the classic (vfs) store and with the containerd image store
  (`tests/cli/test_engine_live.py`, `BRIEFCASE_ENGINE_LIVE_DOCKER=1`). The classic store refused
  an OCI layout without `manifest.json` ("invalid archive: does not contain a manifest.json"),
  so bundles must carry the `manifest.json` that `docker save` writes; the containerd store loads
  either.
- Not run: the real portal and its object storage, a real `cosign`, a real release bundle (fixtures follow
  oci-jj 6d1db6a `scripts/release/engine_release.py`), macOS.

## Troubleshooting

| Symptom | Do this |
| --- | --- |
| `engine not installed` (exit 3) | Get the engine at https://observe.briefcasebrain.io/engine and run the command it shows, or set `BRIEFCASE_ENGINE_ADDR` for an engine elsewhere |
| `no trusted release key is bundled in this SDK build` | `pip install -U 'briefcase-ai[engine]'` once a release with the key is out |
| `your account isn't entitled to the engine` | Request access at https://observe.briefcasebrain.io/engine |
| `the install code has expired` / `was already used` / `is now used up` | Get a new install code at https://observe.briefcasebrain.io/engine |
| `the licence token has expired` / `did not accept the licence token` | Create a new licence token on the Engine page |
| `cannot serve engine downloads right now` | The platform's operator has not finished the engine setup (release storage) |
| `signed by key ..., which this SDK does not trust` | Upgrade the SDK; if it is current, do not install that file |
| `signature does not verify` / `does not match the signed manifest` | Download again from the portal; do not install the file |
| `needs briefcase-ai X or newer` / `schema_version` | `pip install -U 'briefcase-ai[engine]'` |
| `neither the cryptography package nor cosign is installed` | `pip install 'briefcase-ai[engine]'` |
| `Docker is not installed or its daemon is not running` | Start Docker, or `--no-images` for the client only |
| `docker load ... failed` | Update Docker to 25+, or install `skopeo`, then re-run |
| `an engine image changed since it was verified at install` | Re-install (new code, or the offline bundle) |
| `engine 0.4.0 installed, not running` in `briefcase-ai status` | `briefcase-ai engine up` |
| `the engine is not reachable at ...` | `briefcase-ai engine status`, then `briefcase-ai engine logs oci-jj-server` |
| `... is not available on this engine` | `briefcase-ai engine upgrade --token ...` |
