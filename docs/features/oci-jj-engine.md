# Evaluation runs against oci-jj

`briefcase run`, `dataset`, `secret`, `stack` and `doctor` drive the oci-jj engine (the
artifact-graph server shipped as `oci-bai`). This page says what each command sends, which server
settings it supports, and how to test it against a live server.

## What talks to the server

The CLI reads the server's schema over gRPC reflection and calls `oci_jj.v1.VcsService`. There
are no generated stubs.

| Command | Server call | Notes |
| --- | --- | --- |
| `run submit` (gate) | `AttachBench` with `run=true` | Queues a `verdict_gate` job. The job id is printed and saved with the run |
| `run submit --mode hunt` | `RunHunt` | Queues a `hunt_episode` job |
| `run inspect` | `Provenance` | Prints the server's derivation tree |
| `run results` | `Diff` with `depth=BENCH` | Needs `--checkpoint`. Prints "not available" until a verdict is recorded |
| `doctor` | reflection `ListServices` | Checks that an `oci_jj.v1.*` service is served |
| `stack up/down/status/logs` | `docker compose` | Uses the bundled compose file, or `$BRIEFCASE_OCIJJ_COMPOSE` |
| `dataset`, `secret`, `run list/stop/delete` | none | Local registries under `~/.briefcase` |

What oci-jj does not have, and what the CLI does about it:

- **No dataset service.** A dataset is a local name for a URI. The URI is sent as `dataset_uri`
  and read by oci-jj's verdict worker.
- **No secret channel.** Over gRPC, stored secrets and `--secret`/`--env` values are not sent.
  The CLI names the keys it did not send. The one exception is `OCI_JJ_AUTH_TOKEN`, which
  becomes the bearer token (see below).
- **No stop or log RPC.** `run stop` only marks the run locally. `run logs` shows the
  `verdict-worker` container's logs, which exist only when the stack runs with the scorer.
- **Runs are queued, not scored,** unless oci-jj's private verdict worker is running.

## Secured servers

The CLI reads the same settings as oci-jj's own Rust and Go clients:

| Setting | Effect |
| --- | --- |
| `OCI_JJ_SERVER` or `--server` | Endpoint. `https://` or `grpcs://` turns on TLS |
| `OCI_JJ_AUTH_TOKEN` | Sent as `authorization: Bearer <token>` on every call, reflection included. Can also be stored with `briefcase secret set OCI_JJ_AUTH_TOKEN` |
| `OCI_JJ_TLS_CA` | PEM CA that replaces the system roots. Setting it also turns on TLS for an `http://` endpoint, as oci-jj's Go clients do |
| `OCI_JJ_TLS_CLIENT_CERT`, `OCI_JJ_TLS_CLIENT_KEY` | Client identity for mTLS. Both must be set |

The server roles map like this: a `tenant` token can submit, a `reader` token is refused with
"this token is read-only", and a token for another tenant gets "unknown ref" for refs it does
not own. Without a token, `briefcase doctor` reports `requires a bearer token; set
OCI_JJ_AUTH_TOKEN` rather than "unreachable".

The `oci-bai` subprocess engine (`BRIEFCASE_ENGINE=cli`) looks for `oci-bai`, then `oci-jj`,
on `PATH`. `BRIEFCASE_OCIJJ_BIN` overrides that.

## Live integration tests

`tests/cli/test_ocijj_live.py` runs the commands above against a real server. It is skipped
unless `BRIEFCASE_OCIJJ_ADDR` is set.

1. Start a server: `briefcase stack up --no-scorer`, oci-jj's own `docker compose`, or a natively
   built `oci-jj-server` with Postgres 16 and Apache AGE.
2. Push two images that share a layer through the gateway, a baseline and a candidate:

   ```bash
   mkdir -p l1 l2/app && echo base > l1/base.txt && echo v1 > l2/app/main.py
   tar -C l1 -cf l1.tar . && tar -C l2 -cf l2.tar .
   crane append --insecure -f l1.tar -t localhost:8080/rl-gym-env:base
   crane append --insecure -b localhost:8080/rl-gym-env:base -f l2.tar -t localhost:8080/rl-gym-env:cand
   ```

3. Run the suite (it needs the `run` extra):

   ```bash
   pip install 'briefcase-ai[run]' pytest
   BRIEFCASE_OCIJJ_ADDR=http://127.0.0.1:50051 pytest tests/cli/test_ocijj_live.py -v
   ```

   Optional settings: `BRIEFCASE_OCIJJ_REPO` (default `rl-gym-env`), `BRIEFCASE_OCIJJ_CANDIDATE`
   (default `cand`), `BRIEFCASE_OCIJJ_BASELINE` (default `base`), and the `OCI_JJ_*` settings
   above for a secured server. With `OCI_JJ_AUTH_TOKEN` set, the suite also checks that a call
   without the token is refused. `BRIEFCASE_OCIJJ_READER_TOKEN` adds the read-only check.

Each submit queues one real job on the server. Use a throwaway database.
