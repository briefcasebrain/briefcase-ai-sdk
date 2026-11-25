"""``briefcase-ai dataset`` subcommands for local, versioned evaluation datasets.

Not a ``*_commands`` module on purpose: the ``dataset`` command itself is
owned by the evaluation-job commands (``register`` / ``list``), which call
:func:`extend_dataset_parser` on their ``dataset`` subparsers to add:

    briefcase-ai dataset from-records runs.jsonl --name triage --where decision_type=triage,status=ok
    briefcase-ai dataset list | show triage@v2 | diff triage@v1 triage@v2
    briefcase-ai dataset export triage --format jsonl|inspect|lm-eval
    briefcase-ai dataset import edited.jsonl --name triage

Versions live in ``$BRIEFCASE_HOME/datasets/<name>/v<N>.jsonl`` (default
``~/.briefcase``), are written once and never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional

from briefcase.datasets import core
from briefcase.datasets.core import DatasetError, DatasetStore, DatasetVersion
from briefcase.datasets.formats import EXPORT_FORMATS, export_inspect, export_jsonl, export_lm_eval

_RAW = argparse.RawDescriptionHelpFormatter

# Test seam: replaced in tests to exercise the sanitized path without the
# compiled extension.
sanitizer_factory: Callable[[], Optional[Callable[[str], str]]] = core.native_sanitizer


# -- registration -------------------------------------------------------------

def _subparsers_of(parser: argparse.ArgumentParser) -> Optional[argparse._SubParsersAction]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def extend_from_top_level(sub: argparse._SubParsersAction) -> bool:
    """Find the ``dataset`` command among the top-level subparsers and extend
    it. Returns False (and adds nothing) when there is no ``dataset`` command."""
    parser = sub.choices.get("dataset")
    ds = _subparsers_of(parser) if parser is not None else None
    if ds is None:
        return False
    extend_dataset_parser(ds)
    return True


def extend_dataset_parser(ds: argparse._SubParsersAction) -> None:
    """Add the dataset-version subcommands to the existing ``dataset``
    subparsers object (the one ``register`` / ``list`` live in). Idempotent.
    Existing subcommands keep their flags and handlers, except that
    ``dataset list`` gains ``--json`` and also lists local versions (its old
    lines are printed first, unchanged)."""
    if "from-records" in ds.choices:
        return

    p = ds.add_parser(
        "from-records", formatter_class=_RAW,
        help="Create a dataset version from a decision-record JSONL file.",
        description="Turn captured decision records into an immutable, versioned evaluation dataset. "
                    "Each record's input becomes a case; its recorded output becomes the reference.",
        epilog="example:\n  briefcase-ai dataset from-records runs.jsonl --name triage "
               "--where decision_type=triage,status=ok --sample 50 --seed 1 --dedupe",
    )
    p.add_argument("file", metavar="FILE.jsonl", help="Records written by briefcase.observe(\"runs.jsonl\").")
    p.add_argument("--name", required=True, help="Dataset name (letters, digits, . _ -).")
    p.add_argument("--where", action="append", metavar="KEY=VALUE",
                   help="Keep records whose field matches (glob; KEY!=VALUE negates; comma-separate or "
                        "repeat). Keys: any record field or dotted path, plus status (ok/error/cancelled), "
                        "model, prompt_name, prompt_version.")
    p.add_argument("--sample", type=int, metavar="N", help="Keep N cases, drawn at random.")
    p.add_argument("--seed", type=int, default=0, help="Seed for --sample (default 0: same draw every time).")
    p.add_argument("--dedupe", action="store_true", help="Drop cases whose input repeats an earlier one.")
    p.add_argument("--reference", choices=("recorded", "none"), default="recorded",
                   help="recorded (default): the recorded output is the expected output. "
                        "none: no expected output; label later by editing an export.")
    p.add_argument("--redact-fields", metavar="A,B", default="",
                   help="Replace these keys anywhere in inputs/outputs with [REDACTED].")
    p.add_argument("--allow-unsanitized", action="store_true",
                   help="Proceed when the native sanitizer is unavailable (content copied as recorded).")
    p.add_argument("--dry-run", dest="ds_dry_run", action="store_true", help="Show what would be created.")
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    p.set_defaults(func=cmd_from_records)

    listp = ds.choices.get("list")
    if listp is None:
        listp = ds.add_parser("list", help="List datasets.")
    for choice in getattr(ds, "_choices_actions", []):
        if choice.dest == "list":
            choice.help = "List registered datasets and local dataset versions."
    listp.add_argument("--json", action="store_true", help="Machine-readable output.")
    listp.set_defaults(func=cmd_list)

    p = ds.add_parser("show", formatter_class=_RAW, help="Show a dataset version and its first cases.",
                      epilog="example:\n  briefcase-ai dataset show triage@v2")
    p.add_argument("ref", metavar="NAME[@VERSION]", help="NAME (latest), NAME@v2, or NAME@<hash prefix>.")
    p.add_argument("--cases", type=int, default=5, metavar="N", help="Cases to print (default 5).")
    p.add_argument("--json", action="store_true", help="Machine-readable output (all cases).")
    p.set_defaults(func=cmd_show)

    p = ds.add_parser("diff", formatter_class=_RAW, help="Compare two versions of a dataset.",
                      epilog="example:\n  briefcase-ai dataset diff triage@v1 triage@v2")
    p.add_argument("a", metavar="NAME@V1")
    p.add_argument("b", metavar="NAME@V2", help="A bare version (v2) means the same dataset as the first.")
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    p.set_defaults(func=cmd_diff)

    p = ds.add_parser(
        "export", formatter_class=_RAW, help="Write a version as jsonl, inspect-ai or lm-eval input.",
        description="jsonl: briefcase format, hand-editable, re-importable with `dataset import`. "
                    "inspect: inspect-ai Sample JSONL (json_dataset). lm-eval: docs JSONL + task YAML "
                    "(experimental; needs --out).",
        epilog="example:\n  briefcase-ai dataset export triage --format jsonl --out triage.jsonl",
    )
    p.add_argument("ref", metavar="NAME[@VERSION]")
    p.add_argument("--format", choices=EXPORT_FORMATS, default="jsonl")
    p.add_argument("--out", metavar="PATH", help="Write here instead of stdout (lm-eval: the docs .jsonl).")
    p.add_argument("--yes", "-y", action="store_true", help="Overwrite an existing --out file.")
    p.add_argument("--dry-run", dest="ds_dry_run", action="store_true", help="Show what would be written.")
    p.set_defaults(func=cmd_export)

    p = ds.add_parser(
        "import", formatter_class=_RAW, help="Create a version from an edited briefcase JSONL file.",
        description="Reads one case per line: {\"id\": ..., \"input\": ..., \"reference\": ..., "
                    "\"metadata\": {...}}. Only input is required. Errors are reported by line.",
        epilog="example:\n  briefcase-ai dataset import triage.jsonl --name triage",
    )
    p.add_argument("file", metavar="FILE.jsonl")
    p.add_argument("--name", help="Dataset name (default: the name in the file's header line).")
    p.add_argument("--redact-fields", metavar="A,B", default="")
    p.add_argument("--allow-unsanitized", action="store_true",
                   help="Proceed when the native sanitizer is unavailable.")
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    p.set_defaults(func=cmd_import)


# -- helpers ------------------------------------------------------------------

def _home(store: Any) -> Path:
    home = getattr(store, "home", None)
    return Path(home) if home is not None else core.default_home()


def _err(message: str, code: int = 2) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _short(value: Any, width: int = 48) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = repr(text) if isinstance(value, str) else text
    text = text.replace("\n", "\\n")
    return text if len(text) <= width else text[: width - 1] + "…"


def _fields(value: str) -> List[str]:
    return [f for f in (value or "").split(",") if f.strip()]


def _sanitized_line(sanitized: bool) -> str:
    if sanitized:
        return "yes (briefcase.sanitize, native extension)"
    return "NO — --allow-unsanitized given; native sanitizer not available, content copied as recorded"


def _sanitizer_for(args: Any) -> Optional[Callable[[str], str]]:
    fn = sanitizer_factory()
    if fn is None and not getattr(args, "allow_unsanitized", False):
        raise DatasetError(
            "the native sanitizer (briefcase.sanitize) is not available, so PII in inputs/outputs "
            "would be copied as-is. Install a wheel with the native core (pip install "
            "--force-reinstall briefcase-ai) or re-run with --allow-unsanitized"
        )
    return fn


def _registered_conflict(store: Any, name: str) -> Optional[str]:
    try:
        rec = store.get_dataset(name)
    except Exception:
        return None
    return rec.get("uri") if rec else None


# -- from-records -------------------------------------------------------------

def cmd_from_records(args, store, engine=None, stack=None) -> int:
    try:
        name = core.validate_name(args.name)
        uri = _registered_conflict(store, name)
        if uri:
            return _err(f"{name!r} is already registered for the evaluation engine ({uri}); "
                        f"choose another --name, e.g. --name {name}-records")
        sanitizer = _sanitizer_for(args)
        records, problems, source_hash = core.read_records(args.file)
        result = core.promote(
            records, source_name=str(args.file), source_sha256=source_hash, where=args.where,
            sample=args.sample, seed=args.seed, dedupe=args.dedupe, reference=args.reference,
            redact_fields=_fields(args.redact_fields), sanitizer=sanitizer,
            allow_unsanitized=args.allow_unsanitized, problems=problems,
        )
    except DatasetError as exc:
        return _err(str(exc))
    except OSError as exc:
        return _err(f"cannot read {args.file}: {exc.strerror or exc}", 1)

    explain = result.explain()
    if not result.cases:
        if args.json:
            print(json.dumps({"created": False, "cases": 0, "records": result.total,
                              "skipped": result.skipped, "reasons": explain}, indent=2))
        else:
            print(f"No dataset created: none of the {result.total} records in {args.file} became a case.")
            for line in explain:
                print(f"  {line}")
            if not result.total:
                print("  the file holds no records")
        return 1

    dry = getattr(args, "ds_dry_run", False) or getattr(args, "dry_run", False)
    version: Optional[DatasetVersion] = None
    created = False
    if not dry:
        try:
            version, created = DatasetStore(_home(store)).save(name, result.cases, result.metadata)
        except DatasetError as exc:
            return _err(str(exc), 1)

    if args.json:
        out = {"created": created, "dry_run": dry, "name": name, "cases": len(result.cases),
               "records": result.total, "skipped": result.skipped, "reasons": explain,
               "sanitized": result.metadata["sanitized"]}
        if version is not None:
            out.update(version=version.tag, ref=version.ref, hash=version.hash, path=str(version.path))
        print(json.dumps(out, indent=2))
        return 0

    md = result.metadata
    if version is None:
        print(f"Dry run: would create a version of {name} with {len(result.cases)} cases (nothing written).")
    elif created:
        print(f"Created {version.ref}: {len(result.cases)} cases, hash {version.hash[:12]}")
    else:
        print(f"Unchanged: same cases as {version.ref} (hash {version.hash[:12]}); no new version written.")
    src = md["source"]
    print(f"  source     {src['file']}  sha256 {str(src['sha256'])[:12]}  ({result.total} records)")
    print(f"  filters    {', '.join(md['filters']) or 'none'}")
    if md["sample"]:
        print(f"  sample     {md['sample']['n']} cases, seed {md['sample']['seed']}")
    print(f"  reference  {'recorded output' if md['reference'] == 'recorded' else 'none (label later)'}")
    if md["redacted_fields"]:
        print(f"  redacted   {', '.join(md['redacted_fields'])}")
    print(f"  sanitized  {_sanitized_line(md['sanitized'])}")
    for line in explain:
        print(f"  {line}")
    if version is not None:
        print(f"Next: briefcase-ai eval run --dataset {version.ref} --target module:function")
    return 0


# -- list / show / diff -------------------------------------------------------

def cmd_list(args, store, engine=None, stack=None) -> int:
    registered = store.list_datasets()
    ds_store = DatasetStore(_home(store))
    local: List[dict] = []
    for name in ds_store.names():
        try:
            versions = ds_store.versions(name)
        except DatasetError as exc:
            local.append({"name": name, "error": str(exc)})
            continue
        latest = versions[-1]
        local.append({**latest.summary(), "versions": [v.tag for v in versions]})
    if getattr(args, "json", False):
        print(json.dumps({"registered": registered, "versioned": local}, indent=2))
        return 0
    if not registered and not local:
        print("no datasets registered")
        print("Next: briefcase-ai dataset from-records runs.jsonl --name NAME")
        return 0
    for r in registered:
        print(f"{r['name']}\t{r['uri']}")
    for d in local:
        if "error" in d:
            print(f"{d['name']}\tlocal: unreadable ({d['error']})")
        else:
            print(f"{d['name']}\tlocal: {d['version']} ({d['cases']} cases, "
                  f"{len(d['versions'])} version{'s' if len(d['versions']) != 1 else ''}, {d['hash'][:12]})")
    return 0


def _get(store: Any, ref: str):
    return DatasetStore(_home(store)).get(ref)


def cmd_show(args, store, engine=None, stack=None) -> int:
    try:
        version = _get(store, args.ref)
        all_versions = DatasetStore(_home(store)).versions(version.name)
    except DatasetError as exc:
        return _err(str(exc))
    if args.json:
        print(json.dumps({**version.summary(), "header": version.header, "cases": version.cases,
                          "versions": [v.tag for v in all_versions]}, indent=2, ensure_ascii=False))
        return 0
    h = version.header
    print(f"{version.ref}  hash {version.hash[:12]}  {len(version.cases)} cases  created {h.get('created_at')}")
    tags = [v.tag for v in all_versions]
    print(f"  versions   {', '.join(tags)} (latest {tags[-1]})")
    src = h.get("source") or {}
    if src.get("kind") == "import":
        print(f"  source     import of {src.get('file')}  sha256 {str(src.get('sha256'))[:12]}")
    else:
        print(f"  source     {src.get('file')}  sha256 {str(src.get('sha256'))[:12]}  ({src.get('records')} records)")
    print(f"  filters    {', '.join(h.get('filters') or []) or 'none'}")
    if h.get("sample"):
        print(f"  sample     {h['sample'].get('n')} cases, seed {h['sample'].get('seed')}")
    print(f"  reference  {h.get('reference')}")
    print(f"  sanitized  {'yes' if h.get('sanitized') else 'no (--allow-unsanitized)'}")
    print(f"  sdk        briefcase-ai {h.get('sdk_version')}, dataset schema {h.get('schema_version')}")
    shown = version.cases[: max(0, args.cases)]
    if shown:
        print(f"  {'id':<16} {'input':<48} reference")
        for c in shown:
            print(f"  {c['id']:<16} {_short(c['input']):<48} {_short(c.get('reference'), 40)}")
        if len(version.cases) > len(shown):
            print(f"  … {len(version.cases) - len(shown)} more (--cases N, or --json)")
    print(f"Next: briefcase-ai eval run --dataset {version.ref} --target module:function")
    return 0


def cmd_diff(args, store, engine=None, stack=None) -> int:
    b_ref = args.b
    if "@" not in b_ref:
        b_ref = f"{args.a.partition('@')[0]}@{b_ref}"
    try:
        a, b = _get(store, args.a), _get(store, b_ref)
    except DatasetError as exc:
        return _err(str(exc))
    d = core.diff_versions(a, b)
    if args.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return 0
    print(f"{d['from']} ({d['from_hash'][:12]}) -> {d['to']} ({d['to_hash'][:12]})")
    print(f"  +{len(d['added'])} added  -{len(d['removed'])} removed  ~{len(d['changed'])} changed  "
          f"{d['unchanged']} unchanged")
    for key, change in d["metadata"].items():
        before, after = change["before"], change["after"]
        if key == "source":
            before, after = _source_text(before), _source_text(after)
        print(f"  {key}: {_short(before, 40)} -> {_short(after, 40)}")
    for c in d["added"][:20]:
        print(f"+ {c['id']:<16} {_short(c['input'])}  ref {_short(c.get('reference'), 30)}")
    for c in d["removed"][:20]:
        print(f"- {c['id']:<16} {_short(c['input'])}  ref {_short(c.get('reference'), 30)}")
    for c in d["changed"][:20]:
        parts = []
        for f in c["fields"]:
            parts.append(f"{f}: {_short(c['before'].get(f), 30)} -> {_short(c['after'].get(f), 30)}")
        print(f"~ {c['id']:<16} {'; '.join(parts)}")
    shown = max(len(d["added"]), len(d["removed"]), len(d["changed"]))
    if shown > 20:
        print("  (first 20 of each shown; --json for all)")
    return 0


def _source_text(source: Any) -> str:
    if not isinstance(source, dict):
        return str(source)
    kind = "import of" if source.get("kind") == "import" else "records from"
    return f"{kind} {source.get('file')}"


# -- export / import ----------------------------------------------------------

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def cmd_export(args, store, engine=None, stack=None) -> int:
    try:
        version = _get(store, args.ref)
    except DatasetError as exc:
        return _err(str(exc))
    dry = getattr(args, "ds_dry_run", False) or getattr(args, "dry_run", False)
    if args.format == "lm-eval":
        if not args.out:
            return _err(f"--format lm-eval writes two files; give the docs path: "
                        f"briefcase-ai dataset export {args.ref} --format lm-eval --out {version.name}.jsonl")
        docs_path = Path(args.out)
        docs, task_yaml = export_lm_eval(version, docs_path)
        outputs = [(docs_path, docs), (docs_path.with_suffix(".yaml"), task_yaml)]
    else:
        text = export_jsonl(version) if args.format == "jsonl" else export_inspect(version)
        if not args.out:
            sys.stdout.write(text)
            return 0
        outputs = [(Path(args.out), text)]

    existing = [p for p, _ in outputs if p.exists()]
    for path, _ in outputs:
        print(f"{'would write' if dry else 'writing'} {path} ({len(version.cases)} cases from {version.ref})")
    if dry:
        return 0
    if existing and not args.yes:
        return _err(f"{', '.join(map(str, existing))} already exists; re-run with --yes to overwrite", 1)
    for path, text in outputs:
        _write(path, text)
    if args.format == "jsonl":
        print(f"Next: edit {args.out}, then: briefcase-ai dataset import {args.out} --name {version.name}")
    elif args.format == "inspect":
        print(f"Next: in inspect-ai, Task(dataset=json_dataset({str(args.out)!r}), ...)")
    else:
        print(f"Next: lm_eval --include_path {docs_path.resolve().parent} --tasks "
              f"{core_task_name(version)} --model <model> --model_args <args>")
    return 0


def core_task_name(version) -> str:
    from briefcase.datasets.formats import lm_eval_task_name

    return lm_eval_task_name(version)


def cmd_import(args, store, engine=None, stack=None) -> int:
    path = Path(args.file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _err(f"cannot read {path}: {exc.strerror or exc}", 1)
    header, cases, errors = core.parse_cases_jsonl(text)
    if errors:
        if args.json:
            print(json.dumps({"created": False, "errors": errors}, indent=2))
        else:
            print(f"{path}: {len(errors)} problem{'s' if len(errors) != 1 else ''}; nothing imported.",
                  file=sys.stderr)
            for e in errors[:20]:
                print(f"  {path}:{e}", file=sys.stderr)
            if len(errors) > 20:
                print(f"  … {len(errors) - 20} more", file=sys.stderr)
            print("Each line is one case: {\"id\": \"c-1\", \"input\": ..., \"reference\": ...}; only input is "
                  "required.", file=sys.stderr)
        return 1
    if not cases:
        return _err(f"{path} has no cases; add lines like {{\"input\": \"...\", \"reference\": \"...\"}}", 1)
    name = args.name or (header or {}).get("name")
    if not name:
        return _err(f"no dataset name: pass --name, e.g. briefcase-ai dataset import {path} --name NAME")
    try:
        core.validate_name(name)
        uri = _registered_conflict(store, name)
        if uri:
            return _err(f"{name!r} is already registered for the evaluation engine ({uri}); choose another --name")
        sanitizer = _sanitizer_for(args)
    except DatasetError as exc:
        return _err(str(exc))
    redact = frozenset(f.strip().lower() for f in _fields(args.redact_fields))
    out = []
    for c in cases:
        inp, ref = core._redact(c["input"], redact), core._redact(c["reference"], redact)
        if sanitizer is not None:
            inp, ref = core._sanitize(inp, sanitizer), core._sanitize(ref, sanitizer)
        out.append({**c, "input": inp, "reference": ref, "content_sha256": core.case_content_hash(inp, ref)})
    with_ref = sum(1 for c in out if c["reference"] is not None)
    metadata = {
        "source": {"kind": "import", "file": path.name,
                   "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                   "from_hash": (header or {}).get("hash")},
        "filters": (header or {}).get("filters") or [],
        "sample": (header or {}).get("sample"),
        "reference": "none" if not with_ref else ("provided" if with_ref == len(out) else "partial"),
        "redacted_fields": sorted(redact),
        "sanitized": sanitizer is not None,
    }
    try:
        version, created = DatasetStore(_home(store)).save(name, out, metadata)
    except DatasetError as exc:
        return _err(str(exc), 1)
    if args.json:
        print(json.dumps({"created": created, "ref": version.ref, "hash": version.hash, "cases": len(out),
                          "sanitized": sanitizer is not None}, indent=2))
        return 0
    if created:
        print(f"Created {version.ref}: {len(out)} cases, hash {version.hash[:12]} (imported from {path.name})")
    else:
        print(f"Unchanged: same cases as {version.ref}; no new version written.")
    print(f"  sanitized  {_sanitized_line(sanitizer is not None)}")
    print(f"Next: briefcase-ai dataset diff {version.name}@v{max(1, version.number - 1)} {version.ref}"
          if created and version.number > 1 else
          f"Next: briefcase-ai eval run --dataset {version.ref} --target module:function")
    return 0


# -- status hook --------------------------------------------------------------

STATUS_ID = "datasets-from-records"


def status() -> dict:
    """``briefcase-ai status`` row: dataset count and the last eval result
    (exposed through ``eval_commands.status``)."""
    return {**_status_row(), "title": "Datasets"}


def _status_row() -> dict:
    from briefcase.datasets.evaluate import last_history

    home = core.default_home()
    ds_store = DatasetStore(home)
    try:
        names = ds_store.names()
    except OSError:
        names = []
    runs = last_history(home)
    if not names:
        return {"id": STATUS_ID, "state": "off", "detail": "no evaluation datasets yet",
                "next": "briefcase-ai dataset from-records runs.jsonl --name NAME"}
    detail = f"{len(names)} dataset{'s' if len(names) != 1 else ''} ({', '.join(names[:3])}" + \
        (", …)" if len(names) > 3 else ")")
    if not runs:
        return {"id": STATUS_ID, "state": "on", "detail": detail + "; never evaluated",
                "next": f"briefcase-ai eval run --dataset {names[0]} --target module:function"}
    parts = []
    for r in runs:
        rate = r.get("pass_rate")
        parts.append(f"{r.get('target')} {'n/a' if rate is None else f'{rate * 100:.1f}%'}")
    detail += f"; last eval {runs[-1].get('dataset')} ({str(runs[-1].get('at', ''))[:10]}): {', '.join(parts)}"
    failing = [r for r in runs if r.get("ok") is False]
    again = f"briefcase-ai eval run --dataset {runs[-1].get('dataset')} " + " ".join(
        f"--target {r.get('target')}" for r in runs)
    if failing:
        gate = failing[0].get("gate") or f"--fail-under {failing[0].get('fail_under')}"
        return {"id": STATUS_ID, "state": "attention",
                "detail": (detail + f"; gate failed ({gate}): "
                           + ", ".join(str(r.get("target")) for r in failing)),
                "next": again}
    return {"id": STATUS_ID, "state": "on", "detail": detail, "next": again}
