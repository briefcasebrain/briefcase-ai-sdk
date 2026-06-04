"""Command handlers for the ``briefcase`` evaluation-run CLI.

Each handler takes an argparse ``Namespace`` plus an injected ``store`` and ``engine`` (no globals),
so the lifecycle is unit-tested with fakes. Handlers return a process exit code.
"""
from __future__ import annotations

import sys
import time


def _split_kv(item: str):
    key, sep, value = item.partition("=")
    if not sep or not key:
        return None
    return key, value


def _inject_env(store, secret_items, env_items) -> dict:
    """Stored secrets, then per-run --secret, then per-run --env (later wins)."""
    env = store.get_secrets()
    for item in (secret_items or []):
        kv = _split_kv(item)
        if kv:
            env[kv[0]] = kv[1]
    for item in (env_items or []):
        kv = _split_kv(item)
        if kv:
            env[kv[0]] = kv[1]
    return env


def _rc_of(result) -> int:
    """A subprocess result carries ``.returncode``; a gRPC ``ResponseView`` (a returned response)
    means success. gRPC failures raise ``RpcError`` rather than returning a code."""
    return getattr(result, "returncode", 0)


def _maybe_print_rendered(result) -> None:
    """gRPC ``Diff``/``Provenance`` responses carry a human-readable ``rendered`` body (and, for
    ``Diff``, an ``unavailable_reason`` when a telemetry depth is disabled). The subprocess path
    prints to stdout itself, so this is a no-op there (no ``.get`` on a CompletedProcess)."""
    get = getattr(result, "get", None)
    if get is None:
        return
    reason = get("unavailable_reason", "")
    if reason:
        print(reason)
    rendered = get("rendered", "")
    if rendered:
        print(rendered)


# ---- datasets ----
def cmd_dataset_register(args, store, engine, stack=None) -> int:
    rec = store.register_dataset(args.name, args.uri)
    print(f"registered dataset {rec['name']} -> {rec['uri']}")
    return 0


def cmd_dataset_list(args, store, engine, stack=None) -> int:
    rows = store.list_datasets()
    if not rows:
        print("no datasets registered")
    for r in rows:
        print(f"{r['name']}\t{r['uri']}")
    return 0


# ---- secrets ----
def cmd_secret_set(args, store, engine, stack=None) -> int:
    kv = _split_kv(args.assignment)
    if kv is None:
        print("secret must be KEY=VALUE", file=sys.stderr)
        return 2
    store.set_secret(kv[0], kv[1])
    print(f"stored secret {kv[0]}")
    return 0


def cmd_secret_list(args, store, engine, stack=None) -> int:
    for key in store.list_secret_keys():
        print(key)
    return 0


# ---- runs ----
def cmd_run_submit(args, store, engine, stack=None) -> int:
    dataset = store.get_dataset(args.dataset)
    if dataset is None:
        print(f"unknown dataset {args.dataset!r}; register it first", file=sys.stderr)
        return 2
    uri = dataset["uri"]
    mode = getattr(args, "mode", "gate")
    if mode == "hunt":
        call = engine.call_hunt(args.repository, uri)
    else:
        call = engine.call_attach_bench(args.repository, args.checkpoint, uri, args.metric)

    run = {
        "name": args.name,
        "id": args.name,
        "repository": args.repository,
        "revision": getattr(args, "revision", None),
        "dataset": args.dataset,
        "dataset_uri": uri,
        "checkpoint": args.checkpoint,
        "metric": args.metric,
        "mode": mode,
        "call": call.record(),
        "created_at": time.time(),
    }

    if getattr(args, "dry_run", False):
        print("DRY RUN:", call.describe())
        run["status"] = "dry-run"
        store.record_run(run)
        return 0

    env = _inject_env(store, getattr(args, "secret", None), getattr(args, "env", None))
    result = engine.exec(call, env=env)
    rc = _rc_of(result)
    if rc != 0:
        _maybe_print_rendered(result)  # surface the gRPC/engine error detail (e.g. "unknown ref ...")
    run["status"] = "submitted" if rc == 0 else "error"
    store.record_run(run)
    print(f"submitted run {args.name} (mode={mode}) -> rc={rc}")
    return 0 if rc == 0 else 1


def cmd_run_list(args, store, engine, stack=None) -> int:
    runs = store.list_runs()
    if not runs:
        print("no runs submitted")
    for r in runs:
        print(f"{r['name']}\t{r.get('mode', '?')}\t{r.get('status', '?')}\t{r.get('repository', '')}")
    return 0


def cmd_run_inspect(args, store, engine, stack=None) -> int:
    run = store.get_run(args.id)
    if run is None:
        print(f"unknown run {args.id!r}", file=sys.stderr)
        return 2
    for key in ("name", "mode", "status", "repository", "checkpoint", "dataset", "dataset_uri", "metric"):
        print(f"{key}: {run.get(key, '')}")
    # Deeper provenance comes from the engine (best-effort; needs a live stack).
    result = engine.exec(engine.call_provenance(run["repository"]))
    _maybe_print_rendered(result)
    return 0


def cmd_run_logs(args, store, engine, stack=None) -> int:
    run = store.get_run(args.id)
    if run is None:
        print(f"unknown run {args.id!r}", file=sys.stderr)
        return 2
    # oci-jj has no per-job log RPC; surface the verdict-worker container logs via the bundled stack
    # (one compose source of truth; still honors $BRIEFCASE_OCIJJ_COMPOSE inside Stack for checkouts).
    from briefcase.cli.stack_commands import Stack

    stack = stack or Stack()
    return getattr(
        stack.logs(service="verdict-worker", follow=getattr(args, "follow", False)),
        "returncode", 0,
    )


def cmd_run_results(args, store, engine, stack=None) -> int:
    run = store.get_run(args.id)
    if run is None:
        print(f"unknown run {args.id!r}", file=sys.stderr)
        return 2
    result = engine.exec(engine.call_diff_bench(run["checkpoint"], run["repository"]))
    _maybe_print_rendered(result)
    return 0


def cmd_run_stop(args, store, engine, stack=None) -> int:
    if store.get_run(args.id) is None:
        print(f"unknown run {args.id!r}", file=sys.stderr)
        return 2
    # Best-effort: oci-jj runs are durable-queue jobs with no stop RPC; mark intent locally.
    store.update_run(args.id, status="stopped")
    print(f"marked run {args.id} stopped (best-effort)")
    return 0


def cmd_run_delete(args, store, engine, stack=None) -> int:
    if store.delete_run(args.id):
        print(f"deleted run {args.id}")
        return 0
    print(f"unknown run {args.id!r}", file=sys.stderr)
    return 2
