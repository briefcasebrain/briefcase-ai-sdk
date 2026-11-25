# Environment graph: history, diffs and provenance

The engine records every environment you push as a commit in a content-addressed graph.
`briefcase-ai graph` reads it: what changed between two environments, where a file came from,
what was derived from what, and what depends on a given environment.

## The one command

```bash
briefcase-ai graph log cand
```

Real output from an engine built from oci-jj be043d9, after pushing two images with
`briefcase-ai env push` (`OCI_JJ_REPO=local/rl-env`):

```text
$ briefcase-ai graph log cand
c1efb76c  2026-11-22 09:47:49  parents daf12cba  filesystem  local/rl-env:cand
daf12cba  2026-11-22 09:47:47  parents -  filesystem  local/rl-env:base
Next: briefcase-ai graph show cand
$ briefcase-ai graph diff base cand
file: +1 -0 ~0
  + app/main.py
Next: briefcase-ai graph explain cand
$ briefcase-ai graph resolve cand:/app/main.py
/app/main.py: provided by layer[2] (commit c1efb76c)
  tree path /app/main.py
  segment   blake3:205d3a8abd5d2a34190f6fbc7275820cf807be210fbc09e77cdfdcf6ee53fc2d
Next: briefcase-ai graph provenance cand
$ briefcase-ai graph explain cand
Provenance:
  c1efb76c local/rl-env:cand
    daf12cba local/rl-env:base

Changes from parent base (package depth):
  package: (no change; catalog may still be ingesting, §16)
Next: briefcase-ai graph whodepends cand
$ briefcase-ai verify --graph cand
cand: intact (14 trees, 460 inodes, 19 segments re-hashed)
Next: briefcase-ai graph provenance cand
```

## Commands and the engine call each makes

All are one or two `oci_jj.v1.VcsService` calls over gRPC; nothing is computed locally. Every
one takes `--json` (bytes fields such as digests come out as hex) and `--repo`.

| Command | Engine call |
| --- | --- |
| `graph log REF [--limit N]` | `Log` (stream) |
| `graph diff FROM TO [--depth bytes\|file\|package\|semantic\|imports\|loaded-libs\|syscalls\|bench]` | `Diff` |
| `graph search QUERY [--semantic]` | `Search` (stream) |
| `graph provenance REF` | `Provenance` |
| `graph whodepends REF` | `WhoDepends` (stream) |
| `graph show REF` | `Log` (limit 1) + `Provenance` |
| `graph explain REF` | `Log` + `Provenance` + `ListRefs` + `Diff` (package depth, from the parent's ref) |
| `graph resolve REF:/PATH` | `Resolve` |
| `graph referrers sha256:DIGEST [--mine]` | `ListReferrers`, or `ListOwnReferrers` (your private verdicts too) |
| `graph optimize REF [--dataset NAME] [--apply --apply-tag T]` | `Optimize` (stream of rationales) |
| `verify --graph REF` | `Fsck` (re-hashes the tree and checks every referenced segment) |

## What it writes or sends, and where

Reads only, except `graph optimize --apply` (the engine may commit a rewrite). Requests go to
the engine address (the local install, or `BRIEFCASE_ENGINE_ADDR` / `OCI_JJ_SERVER`) with your
bearer token. Nothing is written under `~/.briefcase`.

## Defaults and how to change them

Repository: `OCI_JJ_REPO` or `--repo` (default `rl-gym-env`; with the engine's registry
namespaces, repositories are `<tenant>/<image>`, e.g. `local/rl-env`). Address, token and TLS:
see [engine.md](engine.md#defaults-and-how-to-change-them).

## How to turn it off

Nothing runs in the background. Without the engine every `graph` command prints
"engine not installed — `briefcase-ai engine install`" and exits 3.

## Limits and failure modes

- Telemetry depths (`imports`, `loaded-libs`, `syscalls`, `bench`) say "not available" until the
  engine has recorded that telemetry; `package` depth fills in after the engine's ingest worker
  has catalogued the image.
- `Diff` takes ref names, not commit ids; `explain` looks the parent's ref up and says so when
  the parent has none.
- An RPC the engine does not implement prints "... is not available on this engine" with
  `Fix: briefcase-ai engine upgrade`.

## Troubleshooting

| Symptom | Do this |
| --- | --- |
| `engine not installed` (exit 3) | `briefcase-ai engine install` |
| `no commits for 'X'` | Push first: `briefcase-ai env push IMAGE`; check `--repo` |
| `the engine is not reachable` | `briefcase-ai engine status` |
| `refused the call (missing or invalid bearer token)` | Unset a stale `OCI_JJ_AUTH_TOKEN`, or set the right one |
| `unknown ref` | `briefcase-ai env bookmark list` shows the refs that exist |
| `this token is read-only` | Use a tenant token, not the read-only one |
