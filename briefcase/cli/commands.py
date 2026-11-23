"""Command handlers for the ``briefcase-ai`` evaluation-run CLI.

Each handler takes an argparse ``Namespace`` plus an injected ``store`` and ``engine`` (no globals),
so the lifecycle is unit-tested with fakes. Handlers return a process exit code.
"""
from __future__ import annotations

import os
import sys
import time


def _split_kv(item: str):
    key, sep, value = item.partition("=")
    if not sep or not key:
        return None
    return key, value


def _inject_env(store, secret_items, env_items) -> dict:
    """Stored secrets, then per-run --secret, then per-run --env (later wins). A bare --secret KEY
    takes its value from the caller's environment, keeping the value out of argv."""
    env = store.get_secrets()
    for item in (secret_items or []):
        kv = _split_kv(item)
        if kv is None and item and "=" not in item:
            if item in os.environ:
                kv = (item, os.environ[item])
            else:
                print(
                    f"warning: --secret {item} not found in the environment; not injected",
                    file=sys.stderr,
                )
        if kv:
            env[kv[0]] = kv[1]
    for item in (env_items or []):
        kv = _split_kv(item)
        if kv:
            env[kv[0]] = kv[1]
    return env


def _response_field(result, name):
    """A field of a gRPC ``ResponseView``; ``None`` for a subprocess result (no ``.get``)."""
    get = getattr(result, "get", None)
    if get is None:
        return None
    try:
        return get(name, None)
    except Exception:
        return None


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
    if rendered and rendered != reason:  # oci-jj repeats the reason as the body when unavailable
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
def _read_secret_value(key: str):
    """Read a secret value off stdin (getpass on a tty) so it never appears in argv; None if
    no value is available."""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            import getpass

            value = getpass.getpass(f"value for {key}: ")
        else:
            value = (sys.stdin.readline() if sys.stdin is not None else "").rstrip("\n")
    except (EOFError, OSError):
        return None
    return value or None


def cmd_secret_set(args, store, engine, stack=None) -> int:
    kv = _split_kv(args.assignment)
    if kv is None:
        key = args.assignment
        if not key or "=" in key:
            print("secret must be KEY=VALUE or a bare KEY (value read from stdin)", file=sys.stderr)
            return 2
        value = _read_secret_value(key)
        if value is None:
            print(f"no value provided for {key}", file=sys.stderr)
            return 2
        kv = (key, value)
    store.set_secret(kv[0], kv[1])
    print(f"stored secret {kv[0]}")
    return 0


# The only stored key the engine uses: the client's own bearer token. oci-jj has no channel
# that carries secrets or env vars into a run, so every other key stays on this machine.
ENGINE_SECRET_KEYS = ("OCI_JJ_AUTH_TOKEN",)


def cmd_secret_list(args, store, engine, stack=None) -> int:
    keys = store.list_secret_keys()
    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps([{"key": k, "used_by_engine": k in ENGINE_SECRET_KEYS} for k in keys], indent=2))
        return 0
    for key in keys:
        print(key if key in ENGINE_SECRET_KEYS else f"{key}\t(kept locally; the engine has no secret channel for runs)")
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
    gated = _engine_gate(args, engine)
    if gated is not None:
        return gated

    env = _inject_env(store, getattr(args, "secret", None), getattr(args, "env", None))
    result = engine.exec(call, env=env)
    rc = _rc_of(result)
    if rc != 0:
        _maybe_print_rendered(result)  # surface the gRPC/engine error detail (e.g. "unknown ref ...")
    run["status"] = "submitted" if rc == 0 else "error"
    # The gRPC response carries the server's verdict_queue job id (AttachBench/RunHunt.job_id); keep
    # it so the run can be correlated with the queue (`oci-bai op log`, dead letters).
    job_id = _response_field(result, "job_id") if rc == 0 else None
    if job_id:
        run["job_id"] = int(job_id)
    store.record_run(run)
    suffix = f", server job {int(job_id)}" if job_id else ""
    if rc != 0:
        print(f"run {args.name} was not submitted: the engine refused it (rc={rc})", file=sys.stderr)
        return 1
    print(f"submitted run {args.name} (mode={mode}) -> rc={rc}{suffix}")
    return 0


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
    for key in ("name", "mode", "status", "repository", "checkpoint", "dataset", "dataset_uri", "metric",
                "job_id"):
        print(f"{key}: {run.get(key, '')}")
    # Deeper provenance comes from the engine (best-effort; needs a live stack).
    if _engine_gate(args, engine) is not None:
        return 0  # the local record above is all there is without an engine
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
    if not run.get("checkpoint"):
        # The bench diff needs an explicit baseline ref; a None here used to reach the request
        # builder and crash with a TypeError.
        print(f"run {args.id!r} has no --checkpoint, so there is no baseline to diff its bench "
              "results against; resubmit with --checkpoint <baseline-ref>", file=sys.stderr)
        return 2
    gated = _engine_gate(args, engine)
    if gated is not None:
        return gated
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


# ---- engine gate for the run verbs that reach the engine ----
def _engine_gate(args, engine):
    """``None`` when the engine is reachable-configured; else the not-installed exit code (3).
    A ``--dry-run`` only builds the call, so it is never gated."""
    if getattr(args, "dry_run", False):
        return None
    from briefcase import engine as _engine

    return _engine.gate(args, engine)


# ---- run attach: record a verdict computed elsewhere ----
def cmd_run_attach(args, store, engine, stack=None) -> int:
    import json as _json

    from briefcase.cli import _ux

    run = store.get_run(args.id)
    repository = getattr(args, "repository", None) or (run or {}).get("repository")
    baseline = getattr(args, "checkpoint", None) or (run or {}).get("checkpoint")
    if not repository:
        return _ux.fail(f"unknown run {args.id!r} and no --repository to attach the verdict to",
                        fix=f"briefcase-ai run attach {args.id} --result {args.result} --repository <ref>", code=2)
    for label, path in (("--result", args.result), ("--manifest", getattr(args, "manifest", None))):
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                _json.load(f)
        except (OSError, ValueError) as exc:
            return _ux.fail(f"cannot read {label} {path}: {exc}", fix="pass the verdict.json verdictml wrote",
                            code=2)
    call = engine.call_attach_result(repository, baseline, args.result, getattr(args, "manifest", None))
    if getattr(args, "dry_run", False):
        print("DRY RUN:", call.describe() if not hasattr(call, "request") else
              f"AttachBench(repo={call.request.get('repo')!r}, ref={repository!r}, verdict_json=<{args.result}>)")
        return 0
    gated = _engine_gate(args, engine)
    if gated is not None:
        return gated
    result = engine.exec(call)
    rc = _rc_of(result)
    if rc != 0:
        _maybe_print_rendered(result)
        return _ux.fail("the engine did not record the verdict", fix="briefcase-ai engine status")
    decision = _response_field(result, "decision")
    summary_id = _response_field(result, "summary_id")
    fields = {"status": "verdict-attached", "verdict_file": args.result}
    if decision:
        fields["decision"] = decision
    if summary_id:
        fields["summary_id"] = summary_id
    if run is not None:
        store.update_run(args.id, **fields)
    else:
        store.record_run({"name": args.id, "id": args.id, "repository": repository, "checkpoint": baseline,
                          "mode": "attach", "created_at": time.time(), **fields})
    print(f"verdict recorded on {repository}" + (f": {decision}" if decision else "")
          + (f" (summary {summary_id})" if summary_id else ""))
    _ux.print_next(f"briefcase-ai graph provenance {repository}")
    return 0


# ---- dataset publish: into the engine's per-tenant dataset bucket ----
def cmd_dataset_publish(args, store, engine, stack=None) -> int:
    import subprocess
    from pathlib import Path

    from briefcase import engine as _engine
    from briefcase.cli import _ux
    from briefcase.engine import datasets

    path = Path(args.path)
    if not path.is_file():
        return _ux.fail(f"{path} is not a file", fix="briefcase-ai dataset publish NAME ./data.parquet", code=2)
    record = _engine.current_install()
    tenant = (record or {}).get("tenant") or "local"
    digest = datasets.sha256_hex(path)
    try:
        key = datasets.object_key(tenant, digest, path.name)
    except ValueError as exc:
        return _ux.fail(str(exc), fix="convert it to Parquet or CSV with a `target` column", code=2)
    uri = datasets.uri(key)
    if getattr(args, "dataset_dry_run", False) or getattr(args, "dry_run", False):
        plan = {"name": args.name, "file": str(path), "sha256": digest, "uri": uri, "dry_run": True}
        if getattr(args, "json", False):
            return _ux.print_json(plan)
        print(f"would upload {path} ({path.stat().st_size} bytes) to the local engine as {uri}")
        print(f"and register dataset {args.name} -> {uri}")
        return 0
    gated = _engine.gate(args, engine)
    if gated is not None:
        return gated
    if record is None:
        return _ux.fail("dataset publish uploads into a local engine install; this engine runs elsewhere",
                        fix="upload to s3://<its dataset bucket>/tenants/<tenant>/..., then "
                            f"briefcase-ai dataset register {args.name} --uri s3://...")
    with open(path, "rb") as handle:
        try:
            result = subprocess.run(datasets.upload_argv(record, key), stdin=handle, capture_output=True, text=False)
        except FileNotFoundError:
            return _ux.fail("docker is not installed", fix="briefcase-ai engine doctor")
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()[:300]
        return _ux.fail(f"upload failed ({detail or 'exit ' + str(result.returncode)})",
                        fix="briefcase-ai engine up   # MinIO must be running")
    rec = store.register_dataset(args.name, uri)
    if getattr(args, "json", False):
        return _ux.print_json({**rec, "sha256": digest, "size": path.stat().st_size})
    print(f"published {path.name} ({path.stat().st_size} bytes) as {uri}")
    print(f"registered dataset {rec['name']} -> {rec['uri']}")
    _ux.print_next(f"briefcase-ai run submit NAME --repository REF --dataset {args.name}")
    return 0


# ---- verify --graph: the engine's fsck ----
def cmd_verify_graph(args, store=None, engine=None, stack=None) -> int:
    from briefcase.cli import _ux
    from briefcase.engine import cli_support as cs

    def body(client):
        resp = client.call("Fsck", {"repo": client.repo, "ref": args.graph})
        if getattr(args, "json", False):
            _ux.print_json({"ref": args.graph, **resp, "verdict": "intact" if resp.get("ok") else "damaged"})
            return 0 if resp.get("ok") else 1
        if resp.get("ok"):
            print(f"{args.graph}: intact ({resp.get('trees_checked', 0)} trees, {resp.get('inodes_checked', 0)} "
                  f"inodes, {resp.get('segments_checked', 0)} segments re-hashed)")
            _ux.print_next(f"briefcase-ai graph provenance {args.graph}")
            return 0
        print(f"{args.graph}: NOT intact")
        for problem in resp.get("problems") or []:
            print(f"  - {problem}")
        return 1
    return cs.run(args, engine, body)
