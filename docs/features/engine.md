# The Briefcase engine (oci-jj): install and run it

The engine stores container environments as a versioned, content-addressed graph and scores
evaluation runs against them. It is licensed separately: this package holds only its open gRPC
contract and the client; `briefcase engine install` fetches the engine itself with a licence
token from your Briefcase account.

## The one command

```bash
export BRIEFCASE_LICENSE_TOKEN=bcl_...        # Settings → API Keys → Engine licence tokens
briefcase engine install --platform-url https://YOUR-PLATFORM
```

Real output (a sandbox platform, see "What was verified" below; `$E` is a temporary directory):

```text
Install engine 0.2.0 (linux/x86_64) from http://127.0.0.1:3999:
  - download oci-bai-0.2.0-linux-x86_64.tar.gz -> $E/home/.briefcase/engine/0.2.0/bin/oci-bai (sha256 d41ad0c01504...)
  - check its sha256 only (this release is not signed)
  - docker login 127.0.0.1:5002 (short-lived credential, expires 2026-11-22T08:15:53.000Z)
  - docker pull 127.0.0.1:5002/briefcasebrain/oci-jj-server@sha256:d96fd2f7b7205cb7d56e356fd6526921588720c61a388fb6956eb64557d0c10d
  - docker pull 127.0.0.1:5002/briefcasebrain/oci-jj-gateway@sha256:ed410062fed4a9c5b8bb6331152975e827f133e9e85abee3d04d3569ae01c736
  - docker pull 127.0.0.1:5002/briefcasebrain/oci-jj-workers@sha256:93125b0df3690e4de431e97b5e3dc0b52362f4b878a9cb87466fbd0a4f930ed6
  - docker pull 127.0.0.1:5002/briefcasebrain/oci-jj-postgres@sha256:378dff918778b7043a6d7424ebcea3a73f934a487645bc92f4dbe045a85c1ec6
  - docker pull 127.0.0.1:5002/briefcasebrain/oci-jj-reconstructor@sha256:33f3a9c3c1dafced60623f57b710df3e0ff3f3867fc023346cff56b3cc3b4ad8
  - docker logout 127.0.0.1:5002
  - write $E/home/.briefcase/engine/0.2.0/docker-compose.yml (images pinned by digest)
  - generate secrets once in $E/home/.briefcase/engine/engine.env (mode 0600)
  - record the install in $E/home/.briefcase/engine/installed.json
downloaded oci-bai-0.2.0-linux-x86_64.tar.gz (sha256 ok)
warning: this engine release is not signed; only its sha256 was checked
pulled 127.0.0.1:5002/briefcasebrain/oci-jj-server@sha256:d96fd2f7b7205cb7d56e356fd6526921588720c61a388fb6956eb64557d0c10d
...
engine 0.2.0 installed in $E/home/.briefcase/engine/0.2.0
  client: $E/home/.briefcase/engine/0.2.0/bin/oci-bai  (unsigned)
Next: briefcase engine up
```

Then `briefcase engine up`, `briefcase engine status`, and the engine commands:
[graph.md](graph.md) (Review) and [env-artifacts.md](env-artifacts.md) (Capture). Runs
(`briefcase run submit|attach|results`) and `briefcase dataset publish` are below.

| Subcommand | What it does |
| --- | --- |
| `engine install [--version X] [--token T] [--platform-url URL] [--tls] [--no-images] [--dry-run] [--yes]` | The flow above. Idempotent: re-running checks files and images and only fetches what is missing; an interrupted download resumes. |
| `engine up [--wait]` / `down [--volumes]` / `logs [SERVICE] [-f]` | `docker compose` on the generated file, project `briefcase-engine`. |
| `engine status [--json]` | What is installed, which services run, whether gRPC answers. Exit 3 when nothing is installed. |
| `engine doctor [--json]` | Docker, compose, the install, each image by digest, the secrets file mode, and one authenticated gRPC call. |
| `engine upgrade [--version X]` | Installs the release, restarts onto it when the engine is running. Secrets and data volumes are kept. |
| `engine uninstall [--purge] [--dry-run]` | Stops the stack, removes the images and the version directory; `--purge` also deletes data volumes, secrets and certificates. |

`briefcase stack up|down|status|logs` still works for one release; it prints one line pointing
at `briefcase engine` and uses the engine install when there is one.

## What it writes or sends, and where

| What | Where |
| --- | --- |
| Licence token (bearer), `{os, arch, version}` | `POST <platform>/v1/engine/credentials`. Nothing about your machine beyond OS and architecture. |
| `oci-bai` client binary | `~/.briefcase/engine/<version>/bin/oci-bai` (download kept in `<version>/download/`) |
| Compose file | `~/.briefcase/engine/<version>/docker-compose.yml` |
| Secrets: Postgres superuser and `oci_jj_app` passwords, MinIO root credentials, the internal worker token, your tenant and read-only tokens, dedup HMAC key | `~/.briefcase/engine/engine.env`, mode 0600, generated once and reused by every upgrade |
| Install record | `~/.briefcase/engine/installed.json` |
| Images | Docker's image store. The short-lived registry credential lives in Docker's credential store only between `docker login --password-stdin` and the `docker logout` that always follows. |

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
| Bearer token | the local install's tenant token | `OCI_JJ_AUTH_TOKEN` (also storable with `briefcase secret set OCI_JJ_AUTH_TOKEN`) |
| TLS | off; plaintext on loopback (`OCI_JJ_ALLOW_PLAINTEXT=1` inside the stack) | `engine install --tls` (self-signed certificate under `~/.briefcase/engine/tls`, needs `openssl`); `OCI_JJ_TLS_CA` for an engine elsewhere |
| Release | latest | `--version X` |
| Repository | `rl-gym-env` | `--repo` / `OCI_JJ_REPO` |

## How to turn it off or undo it

`briefcase engine down` stops it; `briefcase engine uninstall` removes it;
`briefcase engine uninstall --purge` also deletes the data. On the platform, revoking the
licence token or the account's entitlement stops new installs and new image pulls at once.

## Without an install

Every engine command is listed in `briefcase --help`, `briefcase features` and
`briefcase status`, marked "engine not installed — `briefcase engine install`". Running one
prints exactly that and exits with code **3** (`--json`: `{"error": "engine_not_installed"}`).
Real output in a fresh HOME:

```text
$ briefcase graph log rl-gym-env
error: engine not installed — `briefcase engine install`
$ echo $?
3
$ briefcase status | grep -i engine
  off       Engine (oci-jj)      engine not installed — `briefcase engine install`
  off       Engine environments  engine not installed — `briefcase engine install`
  off       Artifact graph       engine not installed — `briefcase engine install`
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

- **No secret channel for runs.** `briefcase secret` stores values locally; only
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

## Live tests

- `tests/cli/test_engine_live.py`: the install flow against a real platform. Set
  `BRIEFCASE_ENGINE_LIVE=1`, `BRIEFCASE_PLATFORM_URL`, `BRIEFCASE_LICENSE_TOKEN`;
  `BRIEFCASE_ENGINE_LIVE_UP=1` also starts the engine and queries it.
- `tests/cli/test_ocijj_live.py`: the run commands against any running engine. Set
  `BRIEFCASE_OCIJJ_ADDR` (plus `OCI_JJ_AUTH_TOKEN`; `BRIEFCASE_OCIJJ_REPO`, `_CANDIDATE`,
  `_BASELINE`, `_READER_TOKEN` are optional). Push a baseline and a candidate that share a
  layer first (`briefcase env push`). Each submit queues one real job: use a throwaway database.

## Limits and failure modes

- Signing: the engine's release workflow does not sign anything yet, so only sha256 is
  checked and every install says so. When a release ships a cosign bundle, the install
  requires `cosign` and refuses on a failed check.
- The registry credential lasts at most an hour; an install interrupted for longer just runs
  again.
- `engine install` needs Docker unless `--no-images`.

## What was verified

- Offline tests (`tests/cli/test_engine_install.py`, `test_engine_dataset.py`,
  `test_engine_grpc.py`): a fake platform with GitHub's 302-to-signed-URL behaviour, a fake
  `docker` recording every call, and an in-process gRPC server built from the vendored contract.
- Real, in a sandbox, with Docker: the platform from the briefcase-ai repository on MySQL 8.4,
  a fake GitHub API, and `registry:2` with token auth pointing at the platform. Engine images
  built from oci-jj be043d9 (server, postgres, gateway, workers, reconstructor; no verdict
  worker, which needs the private verdictml) and the `oci-bai` it builds.
  `engine install` (login, five pulls by digest, logout), `engine upgrade` (0.3.0 to 0.3.1,
  restarted), `engine up --wait`, `status`, `doctor`, `install --tls` (server TLS, clients over
  https), `env push` through the authenticating gateway, `graph`/`env`/`verify --graph`,
  `dataset publish` into MinIO and `run submit` (job queued; a URI outside the tenant prefix and
  a `file://` URI were refused by the engine), a read-only token refused on writes, a wrong
  token refused, a revoked entitlement refused by the platform (install 403, `docker login` 401).
- Not run: real GHCR, a real GitHub App, signing, the verdict worker scoring a run.

## Troubleshooting

| Symptom | Do this |
| --- | --- |
| `engine not installed` (exit 3) | `briefcase engine install`, or set `BRIEFCASE_ENGINE_ADDR` for an engine elsewhere |
| `your account isn't entitled to the engine` | Ask your Briefcase account owner to enable the engine for the account |
| `the licence token has expired` / `did not accept the licence token` | Create a new token on the API Keys page |
| `cannot issue engine credentials right now` | The platform's operator has not configured engine downloads (ENGINE_API.md) |
| `Docker is not installed or its daemon is not running` | Start Docker, or `--no-images` for the client only |
| `docker pull ... failed` | Re-run `briefcase engine install`; it resumes and gets a fresh credential |
| `sha256 mismatch` | Re-run; if it repeats, report it: the download is not what the release published |
| `engine 0.2.0 installed, not running` in `briefcase status` | `briefcase engine up` |
| `the engine is not reachable at ...` | `briefcase engine status`, then `briefcase engine logs oci-jj-server` |
| `... is not available on this engine` | `briefcase engine upgrade` |
