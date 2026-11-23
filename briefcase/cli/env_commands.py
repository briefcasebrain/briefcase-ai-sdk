"""``briefcase-ai env``: put environments into the engine and work with them.

    briefcase-ai env push my-env:latest
    briefcase-ai env compose --runtime python311 --model llama --tag stack-v1
    briefcase-ai env checkout cand --paths /app

Most subcommands are one ``oci_jj.v1.VcsService`` call over gRPC (named in their ``--help``).
Two do heavy work on this machine instead: ``push`` (``docker push`` through the engine's
registry gateway, which records the commit) and ``cohort`` (the ``oci-bai`` client chunks the
images locally and uploads only new segments). Needs the engine (``briefcase-ai engine install``).
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional

from briefcase.cli import _ux
from briefcase.engine import cli_support as cs

GROUP = "capture"
ORDER = 80


def register(sub) -> None:
    from briefcase import engine as _engine

    p = sub.add_parser(
        "env",
        help=_engine.help_text("Engine environments: push, compose, check out, bookmark, undo."),
        description="Capture container environments in the engine and work with them: push images "
                    "and cohorts, compose stacks, plan checkouts and mounts, move bookmarks, undo.",
        epilog="example:\n  briefcase-ai env push my-env:latest\n\n" + cs.FOOTER,
        formatter_class=cs.RAW,
    )
    sp = p.add_subparsers(dest="env_sub", metavar="<subcommand>")

    q = cs.sub(sp, "push", "Push a local Docker image into the engine (via its registry gateway).",
               "briefcase-ai env push my-env:latest", "docker push to the gateway, which calls VcsService.Push")
    q.add_argument("image", help="A local image, e.g. my-env:latest.")
    q.add_argument("--as", dest="target", metavar="NAME:TAG",
                   help="Name in the engine (default: <your tenant>/<the image's name>:<tag>).")
    q.add_argument("--dry-run", dest="env_dry_run", action="store_true", help="Print the docker commands only.")
    q.set_defaults(func=cmd_push)

    q = cs.sub(sp, "cohort", "Push N sibling images atomically, deduplicated on this machine.",
               "briefcase-ai env cohort ./cohort-dir", "oci-bai cohort push (chunks locally, then VcsService.CohortPush)")  # noqa: E501
    q.add_argument("dir", help="Directory with cohort.json (see the oci-bai docs for its format).")
    q.add_argument("--dry-run", dest="env_dry_run", action="store_true", help="Print the oci-bai command only.")
    q.set_defaults(func=cmd_cohort)

    q = cs.sub(sp, "compose", "Assemble an environment from existing refs (runtime, model, app...).",
               "briefcase-ai env compose --runtime python311 --app my-app --tag stack-v1", "VcsService.Compose")
    for flag in ("runtime", "toolchain", "model", "dataset", "benchmark", "requires", "provides"):
        q.add_argument(f"--{flag}", action="append", default=[], metavar="REF",
                       help=f"A {flag} domain ref (repeatable).")
    q.add_argument("--incompatible-with", action="append", default=[], metavar="REF")
    q.add_argument("--app", default="", metavar="REF")
    q.add_argument("--tag", default="", help="Tag for the composed commit.")
    q.set_defaults(func=cmd_compose)

    q = cs.sub(sp, "checkout", "Plan a sparse checkout: which files and segments a ref's paths need.",
               "briefcase-ai env checkout cand --paths /app,/etc", "VcsService.CheckoutPlan")
    q.add_argument("ref")
    q.add_argument("--paths", default="", help="Comma-separated path prefixes (default: whole tree).")
    q.add_argument("--dest", help="Write the files here (needs a newer engine; see --help).")
    q.set_defaults(func=cmd_checkout)

    q = cs.sub(sp, "mount", "Plan a lazy mount: the segment fetch order for a ref.",
               "briefcase-ai env mount cand", "VcsService.MountPlan")
    q.add_argument("ref")
    q.add_argument("target", nargs="?", help="Where a real mount would attach (shown, not mounted).")
    q.set_defaults(func=cmd_mount)

    bm = sp.add_parser("bookmark", help="List or move bookmarks and tags.",
                       description="List refs (ListRefs) or move one (SetRef).",
                       epilog="example:\n  briefcase-ai env bookmark set prod cand\n\n" + cs.FOOTER,
                       formatter_class=cs.RAW)
    bsp = bm.add_subparsers(dest="bookmark_sub", metavar="<subcommand>")
    q = cs.sub(bsp, "list", "List refs and divergent heads.", "briefcase-ai env bookmark list", "VcsService.ListRefs")
    q.set_defaults(func=cmd_bookmark_list)
    q = cs.sub(bsp, "set", "Point a bookmark (or tag) at a ref or commit.", "briefcase-ai env bookmark set prod cand",
               "VcsService.SetRef")
    q.add_argument("name")
    q.add_argument("target")
    q.add_argument("--kind", choices=("bookmark", "tag"), default="bookmark")
    q.set_defaults(func=cmd_bookmark_set)

    q = cs.sub(sp, "undo", "Invert an operation (default: the newest, after asking). History is never rewritten.",
               "briefcase-ai env undo --op 41", "VcsService.OpLog (to show it) + VcsService.Undo")
    q.add_argument("--op", default="", help="Operation id to invert (default: the newest one, shown first).")
    q.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    q.set_defaults(func=cmd_undo)

    q = cs.sub(sp, "gc", "Garbage collection: runs inside the engine; this explains when.",
               "briefcase-ai env gc", "none (no tenant-plane GC RPC in oci_jj.v1)")
    q.add_argument("--now", action="store_true", help="Start a collection now (needs a newer engine).")
    q.set_defaults(func=cmd_gc)


# --------------------------------------------------------------------------- local-work commands


def _run(argv: List[str], env: Optional[Dict[str, str]] = None) -> int:
    try:
        return subprocess.run(argv, env={**os.environ, **(env or {})}).returncode
    except FileNotFoundError:
        return 127


def _namespace(args) -> str:
    """The caller's registry namespace: the local install's tenant, else ``VcsService.WhoAmI``."""
    from briefcase import engine as _engine

    record = _engine.current_install()
    if record is not None:
        return record.get("tenant") or "local"
    try:
        from briefcase.engine.client import EngineClient

        return EngineClient(server=getattr(args, "server", None)).call("WhoAmI", {}).get("registry_namespace") or ""
    except Exception:  # noqa: BLE001 - the caller then has to name the namespace
        return ""


def cmd_push(args, store, engine, stack) -> int:
    from briefcase import engine as _engine

    rc = _engine.gate(args, engine)
    if rc is not None:
        return rc
    gateway = _engine.gateway_address()
    name = args.target or args.image.rsplit("/", 1)[-1]
    if ":" not in name.rsplit("/", 1)[-1]:
        name += ":latest"
    if "/" not in name:
        # The gateway only accepts repositories under the caller's namespace: "<tenant>/<image>".
        namespace = _namespace(args)
        if not namespace:
            return _ux.fail("cannot tell your registry namespace", fix=f"briefcase-ai env push {args.image} "
                            "--as <tenant>/<image>:<tag>", code=2)
        name = f"{namespace}/{name}"
    remote = f"{gateway}/{name}"
    plan = [["docker", "login", gateway, "--username", "briefcase", "--password-stdin"],
            ["docker", "tag", args.image, remote], ["docker", "push", remote], ["docker", "logout", gateway]]
    if getattr(args, "env_dry_run", False) or getattr(args, "dry_run", False) or getattr(args, "json", False):
        if getattr(args, "json", False):
            return _ux.print_json({"gateway": gateway, "remote": remote, "commands": plan, "dry_run": True})
        for argv in plan:
            print(" ".join(argv))
        return 0
    token = _engine.client_settings(server=getattr(args, "server", None))["token"]
    if not token:
        return _ux.fail("the gateway needs your engine token", fix="export OCI_JJ_AUTH_TOKEN=<your tenant token>")
    try:
        login = subprocess.run(plan[0], input=token, text=True, capture_output=True)
    except FileNotFoundError:
        return _ux.fail("docker is not installed", fix="install Docker, then re-run")
    if login.returncode != 0:
        return _ux.fail(f"docker login {gateway} failed: {(login.stderr or '').strip()[:200]}",
                        fix="briefcase-ai engine status   # is the gateway up, is the token right?")
    try:
        for argv in plan[1:3]:
            code = _run(argv)
            if code != 0:
                return _ux.fail(f"{' '.join(argv[:2])} failed (exit {code})",
                                fix="briefcase-ai engine logs gateway")
    finally:
        try:
            subprocess.run(plan[3], capture_output=True)
        except FileNotFoundError:
            pass
    print(f"pushed {args.image} -> {remote}; the gateway recorded it as a commit")
    _ux.print_next(f"briefcase-ai graph log {name.split(':')[0]}")
    return 0


def cmd_cohort(args, store, engine, stack) -> int:
    from briefcase import engine as _engine
    from briefcase.cli.engine import OciJJEngine

    rc = _engine.gate(args, engine)
    if rc is not None:
        return rc
    if not os.path.isfile(os.path.join(args.dir, "cohort.json")):
        return _ux.fail(f"{args.dir} has no cohort.json", fix="see `oci-bai cohort push --help` for its format",
                        code=2)
    sub = OciJJEngine(server=getattr(args, "server", None),
                      repo=getattr(args, "engine_repo", None) or getattr(args, "repo", None))
    argv = sub._base() + ["cohort", "push", os.path.abspath(args.dir) if sub.container else args.dir]
    if getattr(args, "env_dry_run", False) or getattr(args, "dry_run", False):
        print(" ".join(sub.command(argv)))
        return 0
    try:
        result = sub.exec(argv)
    except FileNotFoundError:
        return _ux.fail(f"the oci-bai client ({'docker' if sub.container else sub.binary}) is not installed",
                        fix="briefcase-ai engine install   # installs it under ~/.briefcase/engine")
    code = getattr(result, "returncode", 1)
    if code != 0:
        return _ux.fail(f"oci-bai cohort push failed (exit {code})", fix="briefcase-ai engine doctor")
    _ux.print_next("briefcase-ai graph log " + sub.repo)
    return 0


# --------------------------------------------------------------------------- gRPC commands


def cmd_compose(args, store, engine, stack) -> int:
    def body(client):
        resp = client.call("Compose", {
            "repo": client.repo, "runtime": args.runtime, "toolchain": args.toolchain, "model": args.model,
            "dataset": args.dataset, "benchmark": args.benchmark, "app": args.app, "tag": args.tag,
            "requires": args.requires, "provides": args.provides, "incompatible_with": args.incompatible_with})
        if getattr(args, "json", False):
            return _ux.print_json(resp)
        print(f"composed commit {resp.get('commit_id')} (op {resp.get('op_id')}), OCI export: "
              f"{resp.get('exportability') or 'unknown'}")
        for warning in resp.get("warnings") or []:
            print(f"  warning: {warning}")
        _ux.print_next(f"briefcase-ai graph show {args.tag or resp.get('commit_id')}")
        return 0
    return cs.run(args, engine, body)


def _paths(text: str) -> List[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def cmd_checkout(args, store, engine, stack) -> int:
    if args.dest:
        return cs.needs_newer_engine(args, engine, "writing a checkout to this machine (--dest)",
                                     "client-side materialization RPC (MaterializeCheckout writes on the server "
                                     "under OCI_JJ_CHECKOUT_DIR only)")

    def body(client):
        resp = client.call("CheckoutPlan", {"repo": client.repo, "ref": args.ref, "paths": _paths(args.paths)})
        if getattr(args, "json", False):
            return _ux.print_json(resp)
        files = resp.get("files") or []
        print(f"{args.ref}: {resp.get('file_count', len(files))} files, {resp.get('total_bytes', 0)} bytes in "
              f"{len(resp.get('segments') or [])} segments ({resp.get('skipped_count', 0)} files not selected)")
        for f in files[:50]:
            print(f"  {f.get('path')}  {f.get('size')} bytes")
        if len(files) > 50:
            print(f"  ... {len(files) - 50} more (--json for all)")
        for missing in resp.get("unmatched_paths") or []:
            print(f"  warning: {missing} selected nothing")
        print("This is the plan; nothing was written.")
        _ux.print_next(f"briefcase-ai env mount {args.ref}")
        return 0
    return cs.run(args, engine, body)


def cmd_mount(args, store, engine, stack) -> int:
    def body(client):
        resp = client.call("MountPlan", {"repo": client.repo, "ref": args.ref})
        if getattr(args, "json", False):
            return _ux.print_json(resp)
        print(f"{args.ref}: graph {resp.get('graph_root')}, {resp.get('file_count', 0)} files, "
              f"{resp.get('total_bytes', 0)} bytes in {resp.get('segment_count', 0)} segments")
        unresolved = resp.get("unresolved") or []
        if unresolved:
            print(f"  {len(unresolved)} segments are referenced but missing: run briefcase-ai verify --graph {args.ref}")  # noqa: E501
        where = f" at {args.target}" if args.target else ""
        print(f"This is the fetch plan a lazy mount{where} would serve; the mount itself needs the engine's "
              "snapshotter (Linux, FUSE) and is not started from here.")
        _ux.print_next(f"briefcase-ai verify --graph {args.ref}")
        return 0
    return cs.run(args, engine, body)


def cmd_bookmark_list(args, store, engine, stack) -> int:
    def body(client):
        resp = client.call("ListRefs", {"repo": client.repo})
        if getattr(args, "json", False):
            return _ux.print_json(resp)
        refs = resp.get("refs") or []
        if not refs:
            print(f"no refs in {client.repo}. Push an image first: briefcase-ai env push IMAGE")
            return 0
        for ref in refs:
            head = "  head" if ref.get("is_head") else ""
            print(f"{ref.get('kind'):<9} {ref.get('name'):<24} {cs.short(ref.get('commit_id'), 8)}{head}")
        if resp.get("head_count", 0) > 1:
            print(f"{resp['head_count']} divergent heads")
        _ux.print_next("briefcase-ai env bookmark set NAME REF")
        return 0
    return cs.run(args, engine, body)


def cmd_bookmark_set(args, store, engine, stack) -> int:
    def body(client):
        resp = client.call("SetRef", {"repo": client.repo, "name": args.name, "target": args.target,
                                      "kind": args.kind, "actor": os.environ.get("USER", "")})
        if getattr(args, "json", False):
            return _ux.print_json(resp)
        print(f"{args.kind} {args.name} -> {resp.get('commit_id')} (op {resp.get('op_id')})")
        if resp.get("protected_warning"):
            print(f"  warning: {args.name} is a protected name; {resp.get('message') or ''}".rstrip())
        _ux.print_next("briefcase-ai env undo", "if that was a mistake")
        return 0
    return cs.run(args, engine, body)


def cmd_undo(args, store, engine, stack) -> int:
    def body(client):
        op_ref = args.op
        if not op_ref:
            # Name the operation explicitly: the engine's own "last" is not always the newest op id.
            ops = client.stream("OpLog", {"after_op_id": 0, "limit": 0})
            if not ops:
                return _ux.fail("there is no operation to undo", fix="briefcase-ai env bookmark list")
            newest = max(ops, key=lambda op: int(op.get("op_id") or 0))
            payload = (newest.get("payload_json") or "")[:120]
            print(f"newest operation: {newest.get('op_id')} {newest.get('op_kind')} {payload}")
            answer = _ux.confirm(f"Invert operation {newest.get('op_id')}?", default=False,
                                 assume_yes=bool(getattr(args, "yes", False)))
            if not answer:
                return _ux.fail("nothing was undone", fix=f"briefcase-ai env undo --op {newest.get('op_id')}",
                                code=2 if answer is None else 1)
            op_ref = str(newest.get("op_id"))
        resp = client.call("Undo", {"op_ref": op_ref})
        if getattr(args, "json", False):
            return _ux.print_json({"op": op_ref, **resp})
        print(f"inverted operation {op_ref}; new op {resp.get('inverse_op_id')} (history is kept)")
        _ux.print_next("briefcase-ai env bookmark list")
        return 0
    return cs.run(args, engine, body)


def cmd_gc(args, store, engine, stack) -> int:
    if args.now:
        return cs.needs_newer_engine(args, engine, "starting garbage collection from the client",
                                     "tenant-plane GC RPC (IngestService.RunGc is operator-only)")
    from briefcase import engine as _engine

    rc = _engine.gate(args, engine)
    if rc is not None:
        return rc
    message = ("Garbage collection runs inside the engine: its workers sweep unreferenced blobs every "
               "OCI_JJ_GC_INTERVAL (5m in the local stack). Nothing to do here.")
    if getattr(args, "json", False):
        return _ux.print_json({"automatic": True, "message": message})
    print(message)
    _ux.print_next("briefcase-ai engine logs workers")
    return 0


def status() -> List[Dict[str, Any]]:
    from briefcase import engine as _engine
    from briefcase.cli.engine_commands import status_row as engine_row

    row = _engine.status_row("env-artifacts", "Engine environments",
                             "pushes and composes into the engine at " + _engine.client_settings()["addr"])
    row["next"] = None
    return [engine_row(), row]
