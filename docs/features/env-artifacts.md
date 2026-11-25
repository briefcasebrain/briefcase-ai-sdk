# Environments in the engine: push, compose, check out, bookmark

Put container environments into the engine and work with them as versioned objects: push an
image, assemble a stack from existing refs, plan what a sparse checkout or lazy mount would
fetch, and move bookmarks with an undo that never rewrites history.

## The one command

```bash
briefcase-ai env push my-env:latest
```

Real output against an engine built from oci-jj be043d9 (`briefcase-ai engine up`):

```text
$ briefcase-ai env push rl-env:base
pushed rl-env:base -> 127.0.0.1:8080/local/rl-env:base; the gateway recorded it as a commit
Next: briefcase-ai graph log local/rl-env
$ briefcase-ai env checkout cand --paths /app
cand: 1 files, 9 bytes in 1 segments (17 files not selected)
  /app/main.py  9 bytes
This is the plan; nothing was written.
Next: briefcase-ai env mount cand
$ briefcase-ai env mount cand
cand: graph blake3:7c5377f1f1c0a009f011e98366dad1f801d456327628f271d0368973e1ce08bf, 18 files, 4417162 bytes in 18 segments
This is the fetch plan a lazy mount would serve; the mount itself needs the engine's snapshotter (Linux, FUSE) and is not started from here.
Next: briefcase-ai verify --graph cand
$ briefcase-ai env bookmark list
tag       base                     daf12cba
tag       cand                     c1efb76c  head
Next: briefcase-ai env bookmark set NAME REF
$ briefcase-ai env undo --yes
newest operation: 10 tag_move {"from":"daf12cba-3df7-48e7-b6ca-25f1a579262e","kind":"tag","name":"cand",...
inverted operation 10; new op 11 (history is kept)
Next: briefcase-ai env bookmark list
```

## Commands and what each uses

| Command | How |
| --- | --- |
| `env push IMAGE [--as NAME:TAG]` | `docker login <gateway>` with your tenant token (stdin), `docker tag`, `docker push`, `docker logout`. The gateway authenticates you, stores the layers and records the commit (`VcsService.Push`). Names go under your registry namespace: `local/<image>` on a local install, else learned from `VcsService.WhoAmI`. |
| `env cohort DIR` | `oci-bai cohort push DIR`: the installed client chunks the images locally, uploads only new segments, then one atomic `VcsService.CohortPush` |
| `env compose --runtime REF ... --tag T` | `VcsService.Compose` |
| `env checkout REF [--paths a,b]` | `VcsService.CheckoutPlan` (the plan; see limits) |
| `env mount REF [TARGET]` | `VcsService.MountPlan` (the plan; see limits) |
| `env bookmark list` / `set NAME TARGET [--kind tag]` | `VcsService.ListRefs` / `SetRef` |
| `env undo [--op ID] [--yes]` | `VcsService.OpLog` (shows the newest operation and asks) + `Undo` with that explicit id |
| `env gc` | none: garbage collection runs inside the engine's workers every `OCI_JJ_GC_INTERVAL` (5 min locally) |

Why push and cohort run locally: they move image bytes from your machine. Everything else is
one RPC, so `checkout` and `mount` use gRPC directly (the `oci-bai` commands of the same names
make the same plan RPCs).

## What it writes or sends, and where

`env push` adds one tag to your local Docker image store and sends the image to the engine's
gateway (127.0.0.1:8080 on a local install). The token is passed to `docker login` on stdin and
removed by `docker logout` right after. The other commands send only their arguments.

## Defaults and how to change them

Repository `OCI_JJ_REPO` / `--repo`; gateway `BRIEFCASE_ENGINE_GATEWAY` (default: the install's,
127.0.0.1:8080); address, token and TLS as in [engine.md](engine.md).

## How to turn it off or undo it

`env undo` inverts an operation (a push, a bookmark move) by appending its inverse.
`env bookmark set` moves a ref back.

## Limits and failure modes

- **Needs a newer engine:** `env checkout --dest DIR` (the engine only materializes checkouts on
  its own disk, `MaterializeCheckout` under `OCI_JJ_CHECKOUT_DIR`; there is no client-side
  materialization RPC) and `env gc --now` (GC is an operator-only RPC). Both say so and exit 1.
- A real lazy mount needs the engine's snapshotter (Linux, FUSE); `env mount` shows its plan.
- The engine's own "undo the last operation" is not always the newest operation id, so
  `env undo` without `--op` shows the newest one and asks (`--yes` to skip), then names it.
- The gateway refuses pushes outside your namespace and pushes with a read-only token
  (`docker push failed`, see `briefcase-ai engine logs gateway`).

## Troubleshooting

| Symptom | Do this |
| --- | --- |
| `engine not installed` (exit 3) | `briefcase-ai engine install` |
| `docker login ... failed` | `briefcase-ai engine status`: is the gateway up? Is `OCI_JJ_AUTH_TOKEN` a tenant token? |
| `docker push failed` | Name it `<your tenant>/<image>`; use a tenant (not read-only) token |
| `cannot tell your registry namespace` | `--as <tenant>/<image>:<tag>` |
| `requires a newer engine` | `briefcase-ai engine upgrade` once a release adds it |
| `the oci-bai client ... is not installed` | `briefcase-ai engine install` (it installs `oci-bai`) |
