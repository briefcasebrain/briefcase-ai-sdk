"""``briefcase-ai graph``: read the engine's artifact graph (history, diffs, provenance, search).

    briefcase-ai graph log rl-gym-env
    briefcase-ai graph diff base cand --depth package
    briefcase-ai graph provenance cand

Every subcommand is one ``oci_jj.v1.VcsService`` call over gRPC (named in its ``--help``);
nothing runs locally. Needs the engine (``briefcase-ai engine install``) and the ``engine`` extra.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from briefcase.cli import _ux
from briefcase.engine import cli_support as cs

GROUP = "review"
ORDER = 60

DEPTHS = ("bytes", "file", "package", "semantic", "imports", "loaded-libs", "syscalls", "bench")


def register(sub) -> None:
    from briefcase import engine as _engine

    p = sub.add_parser(
        "graph",
        help=_engine.help_text("Engine artifact graph: history, diffs, provenance, search."),
        description="Read what the engine recorded about environments: commits, diffs at any depth, "
                    "derivation trees, reverse dependencies and referrers.",
        epilog="example:\n  briefcase-ai graph log rl-gym-env\n\n" + cs.FOOTER,
        formatter_class=cs.RAW,
    )
    sp = p.add_subparsers(dest="graph_sub", metavar="<subcommand>")

    q = cs.sub(sp, "log", "Commit history of a ref or image family.", "briefcase-ai graph log rl-gym-env",
               "VcsService.Log (stream)")
    q.add_argument("ref")
    q.add_argument("--limit", type=int, default=50, help="Most commits to show (default 50).")
    q.set_defaults(func=cmd_log)

    q = cs.sub(sp, "diff", "What changed between two refs, at a chosen depth.",
               "briefcase-ai graph diff base cand --depth package", "VcsService.Diff")
    q.add_argument("from_ref", metavar="FROM")
    q.add_argument("to_ref", metavar="TO")
    q.add_argument("--depth", choices=DEPTHS, default="file", help="Diff depth (default file).")
    q.set_defaults(func=cmd_diff)

    q = cs.sub(sp, "search", "Search the package catalog across environments.",
               'briefcase-ai graph search "torch>=2.3"', "VcsService.Search (stream)")
    q.add_argument("query")
    q.add_argument("--semantic", action="store_true", help="Semantic search (the engine may not support it yet).")
    q.add_argument("--limit", type=int, default=20)
    q.set_defaults(func=cmd_search)

    q = cs.sub(sp, "provenance", "Full derivation tree of a ref.", "briefcase-ai graph provenance cand",
               "VcsService.Provenance")
    q.add_argument("ref")
    q.set_defaults(func=cmd_provenance)

    q = cs.sub(sp, "whodepends", "Refs derived from this one (reverse derivation walk).",
               "briefcase-ai graph whodepends base", "VcsService.WhoDepends (stream)")
    q.add_argument("ref")
    q.set_defaults(func=cmd_whodepends)

    q = cs.sub(sp, "show", "One commit: its metadata and provenance.", "briefcase-ai graph show cand",
               "VcsService.Log (limit 1) + VcsService.Provenance")
    q.add_argument("ref")
    q.set_defaults(func=cmd_show)

    q = cs.sub(sp, "explain", "Why a ref looks the way it does: provenance plus the diff from its parent.",
               "briefcase-ai graph explain cand", "VcsService.Log + Provenance + Diff (depth package)")
    q.add_argument("ref")
    q.set_defaults(func=cmd_explain)

    q = cs.sub(sp, "resolve", "Which layer or domain provides a path, and what it shadows.",
               "briefcase-ai graph resolve cand:/usr/bin/python3", "VcsService.Resolve")
    q.add_argument("target", metavar="REF:/PATH")
    q.set_defaults(func=cmd_resolve)

    q = cs.sub(sp, "referrers", "Artifacts that refer to an image (SBOMs, verdicts, nydus).",
               "briefcase-ai graph referrers sha256:<digest> --mine",
               "VcsService.ListReferrers, or ListOwnReferrers with --mine")
    q.add_argument("subject", metavar="sha256:DIGEST")
    q.add_argument("--mine", action="store_true", help="Your tenant's own referrers, including private verdicts.")
    q.add_argument("--artifact-type", help="Filter, e.g. application/vnd.oci-jj.verdict.v1+json.")
    q.set_defaults(func=cmd_referrers)

    q = cs.sub(sp, "optimize", "Suggested layout changes for a ref, each with its rationale.",
               "briefcase-ai graph optimize cand", "VcsService.Optimize (stream of Rationale)")
    q.add_argument("ref")
    q.add_argument("--dataset", help="Registered dataset name: back the suggestions with a verdict.")
    q.add_argument("--metric", default="", help="Verdict metric with --dataset (engine default when empty).")
    q.add_argument("--apply", action="store_true", help="Gate a rewrite commit on an accept verdict.")
    q.add_argument("--apply-tag", help="Tag for the applied commit.")
    q.set_defaults(func=cmd_optimize)


# --------------------------------------------------------------------------- output


def _emit(args, data: Any, lines: List[str], empty: str, next_cmd: str) -> int:
    if getattr(args, "json", False):
        return _ux.print_json(data)
    if not lines:
        print(empty)
    for line in lines:
        print(line)
    if lines:
        _ux.print_next(next_cmd)
    return 0


def _commit_line(commit: Dict[str, Any]) -> str:
    message = (commit.get("message") or "").splitlines()[0] if commit.get("message") else ""
    parents = ",".join(cs.short(p, 8) for p in commit.get("parents") or []) or "-"
    return (f"{cs.short(commit.get('commit_id'), 8)}  {commit.get('created_at', '')[:19]}  "
            f"parents {parents}  {commit.get('artifact_class') or ''}  {message}").rstrip()


def _rendered(resp: Dict[str, Any]) -> List[str]:
    text = resp.get("rendered") or ""
    return text.rstrip("\n").splitlines() if text else []


# --------------------------------------------------------------------------- handlers


def cmd_log(args, store, engine, stack) -> int:
    def body(client):
        commits = client.stream("Log", {"repo": client.repo, "ref": args.ref, "limit": args.limit},
                                limit=args.limit)
        return _emit(args, {"repo": client.repo, "ref": args.ref, "commits": commits},
                     [_commit_line(c) for c in commits],
                     f"no commits for {args.ref!r} in {client.repo}. Push an image first: briefcase-ai env push IMAGE",
                     f"briefcase-ai graph show {args.ref}")
    return cs.run(args, engine, body)


def cmd_diff(args, store, engine, stack) -> int:
    def body(client):
        resp = client.call("Diff", {"repo": client.repo, "from_ref": args.from_ref, "to_ref": args.to_ref,
                                    "depth": args.depth.upper().replace("-", "_")})
        if not resp.get("available", True) and not getattr(args, "json", False):
            print(resp.get("unavailable_reason") or f"depth {args.depth} is not available on this engine")
            _ux.print_next(f"briefcase-ai graph diff {args.from_ref} {args.to_ref} --depth file")
            return 0
        structured = resp.get("structured_json")
        if structured:
            try:
                resp["structured"] = json.loads(structured)
            except ValueError:
                pass
        return _emit(args, resp, _rendered(resp), "no differences",
                     f"briefcase-ai graph explain {args.to_ref}")
    return cs.run(args, engine, body)


def cmd_search(args, store, engine, stack) -> int:
    def body(client):
        hits = client.stream("Search", {"query": args.query, "semantic": args.semantic, "limit": args.limit},
                             limit=args.limit)
        lines = [f"{h.get('repo')}  {cs.short(h.get('commit_id'), 8)}  {h.get('rank', 0):.3f}  {h.get('snippet', '')}"
                 for h in hits]
        first = hits[0] if hits else {}
        return _emit(args, {"query": args.query, "hits": hits}, lines,
                     f"nothing matches {args.query!r}. Packages are indexed after an image is pushed and ingested.",
                     f"briefcase-ai graph show {first.get('commit_id', '<ref>')}")
    return cs.run(args, engine, body)


def cmd_provenance(args, store, engine, stack) -> int:
    def body(client):
        resp = client.call("Provenance", {"repo": client.repo, "ref": args.ref})
        return _emit(args, resp, _rendered(resp), f"no provenance recorded for {args.ref!r}",
                     f"briefcase-ai graph whodepends {args.ref}")
    return cs.run(args, engine, body)


def cmd_whodepends(args, store, engine, stack) -> int:
    def body(client):
        commits = client.stream("WhoDepends", {"repo": client.repo, "ref": args.ref})
        return _emit(args, {"ref": args.ref, "dependents": commits}, [_commit_line(c) for c in commits],
                     f"nothing is derived from {args.ref!r}", f"briefcase-ai graph provenance {args.ref}")
    return cs.run(args, engine, body)


def cmd_show(args, store, engine, stack) -> int:
    def body(client):
        commits = client.stream("Log", {"repo": client.repo, "ref": args.ref, "limit": 1}, limit=1)
        if not commits:
            if getattr(args, "json", False):
                return _ux.print_json({"ref": args.ref, "commit": None})
            return _ux.fail(f"no commit for {args.ref!r} in {client.repo}", fix=f"briefcase-ai graph log {client.repo}")
        prov = client.call("Provenance", {"repo": client.repo, "ref": args.ref})
        commit = commits[0]
        lines = [f"commit     {commit.get('commit_id')}",
                 f"manifest   sha256:{commit.get('manifest_digest', '')}",
                 f"graph      blake3:{commit.get('mount_graph_id', '')}",
                 f"class      {commit.get('artifact_class') or '-'}",
                 f"builder    {commit.get('builder') or '-'}",
                 f"created    {commit.get('created_at', '')}",
                 f"parents    {', '.join(commit.get('parents') or []) or '-'}",
                 f"message    {commit.get('message') or ''}", ""] + _rendered(prov)
        return _emit(args, {"commit": commit, "provenance": prov}, lines, "", f"briefcase-ai graph explain {args.ref}")
    return cs.run(args, engine, body)


def cmd_explain(args, store, engine, stack) -> int:
    from briefcase.engine.client import EngineError

    def body(client):
        commits = client.stream("Log", {"repo": client.repo, "ref": args.ref, "limit": 1}, limit=1)
        prov = client.call("Provenance", {"repo": client.repo, "ref": args.ref})
        parent = ((commits[0].get("parents") or [None])[0]) if commits else None
        diff, parent_ref, note = None, None, ""
        if parent:
            # Diff takes ref names; find one that points at the parent commit.
            refs = client.call("ListRefs", {"repo": client.repo}).get("refs") or []
            parent_ref = next((r.get("name") for r in refs if r.get("commit_id") == parent), None)
            if parent_ref:
                try:
                    diff = client.call("Diff", {"repo": client.repo, "from_ref": parent_ref, "to_ref": args.ref,
                                                "depth": "PACKAGE"})
                except EngineError as exc:
                    note = f"(no diff: {exc})"
            else:
                note = "(the parent has no tag or bookmark to diff against; set one with briefcase-ai env bookmark set)"
        lines = ["Provenance:"] + ["  " + line for line in _rendered(prov) or ["(none recorded)"]]
        if parent:
            label = parent_ref or cs.short(parent, 8)
            lines += ["", f"Changes from parent {label} (package depth):"]
            reason = (diff or {}).get("unavailable_reason")
            body_lines = _rendered(diff or {}) or ([reason] if reason else [])
            lines += ["  " + line for line in (body_lines or [note or "(no package changes)"])]
        else:
            lines += ["", "No parent commit: this is a base."]
        return _emit(args, {"ref": args.ref, "commit": commits[0] if commits else None, "provenance": prov,
                            "parent": parent, "parent_ref": parent_ref, "diff_from_parent": diff}, lines, "",
                     f"briefcase-ai graph whodepends {args.ref}")
    return cs.run(args, engine, body)


def cmd_resolve(args, store, engine, stack) -> int:
    ref, sep, path = args.target.partition(":/")
    path = "/" + path if sep else "/"

    def body(client):
        resp = client.call("Resolve", {"repo": client.repo, "ref": ref, "path": path})
        lines = [f"{path}: provided by {resp.get('provided_by') or '?'}",
                 f"  tree path {resp.get('tree_path') or '-'}"]
        if resp.get("segment"):
            lines.append(f"  segment   blake3:{resp['segment']}")
        lines += [f"  shadows   {s.get('path')} ({s.get('source')})" for s in resp.get("shadowed") or []]
        return _emit(args, resp, lines if resp.get("provided_by") or resp.get("tree_path") else [],
                     f"{path} is not in {ref}", f"briefcase-ai graph provenance {ref}")
    return cs.run(args, engine, body)


def cmd_referrers(args, store, engine, stack) -> int:
    subject = args.subject.split(":", 1)[-1]
    try:
        digest = bytes.fromhex(subject)
    except ValueError:
        return _ux.fail(f"{args.subject!r} is not a sha256 digest", fix="briefcase-ai graph show <ref>   # prints it",
                        code=2)

    def body(client):
        method = "ListOwnReferrers" if args.mine else "ListReferrers"
        resp = client.call(method, {"subject_digest": digest, "artifact_type": args.artifact_type or ""})
        rows = resp.get("descriptors") or []
        lines = [f"sha256:{r.get('artifact_digest')}  {r.get('artifact_type')}  {r.get('size')} bytes"
                 f"{'' if r.get('is_public', True) else '  (private)'}" for r in rows]
        hint = "" if args.mine else " Add --mine to include your private verdicts."
        return _emit(args, resp, lines, f"no referrers for sha256:{subject}.{hint}",
                     f"briefcase-ai graph referrers sha256:{subject}{'' if args.mine else ' --mine'}")
    return cs.run(args, engine, body)


def cmd_optimize(args, store, engine, stack) -> int:
    dataset_uri = ""
    if args.dataset:
        record = store.get_dataset(args.dataset)
        if record is None:
            return _ux.fail(f"unknown dataset {args.dataset!r}", fix="briefcase-ai dataset list", code=2)
        dataset_uri = record["uri"]

    def body(client):
        rationales = client.stream("Optimize", {"repo": client.repo, "ref": args.ref, "apply": args.apply,
                                                "apply_tag": args.apply_tag or "", "dataset_uri": dataset_uri,
                                                "metric": args.metric})
        lines = []
        for r in rationales:
            effect = r.get("effect") or {}
            lines.append(f"{r.get('action_id')}: {r.get('summary')}  (confidence {r.get('confidence', 0):.2f}"
                         f"{', ' + r['verdict_decision'] if r.get('verdict_decision') else ''})")
            if effect.get("metric"):
                lines.append(f"  {effect['metric']}: {effect.get('baseline')} -> {effect.get('projected')}")
        return _emit(args, {"ref": args.ref, "rationales": rationales}, lines,
                     f"no suggestions for {args.ref!r}", f"briefcase-ai graph explain {args.ref}")
    return cs.run(args, engine, body)


def status() -> Dict[str, Any]:
    from briefcase import engine as _engine

    row = _engine.status_row("graph", "Artifact graph", "reads the engine at " + _engine.client_settings()["addr"])
    row["next"] = None
    return row
