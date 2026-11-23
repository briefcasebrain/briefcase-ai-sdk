"""Promotion of decision records into cases, and the local version store."""

from __future__ import annotations

import ast
import difflib
import fnmatch
import hashlib
import json
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

DATASET_SCHEMA_VERSION = "1"
HEADER_KEY = "briefcase_dataset"
CASE_KEYS = ("id", "input", "reference", "metadata", "content_sha256")
REDACTED = "[REDACTED]"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_VERSION_FILE_RE = re.compile(r"^v(\d+)\.jsonl$")

# Keys that @capture / provider capture write in "hash" or "none" mode: they
# describe content without holding it.
_CONTENT_FREE_KEYS = frozenset({
    "args_sha256", "args_chars", "kwargs_sha256", "kwargs_chars",
    "args_count", "kwargs_count", "result_sha256", "result_chars", "result_type",
    "sha256", "chars", "chunk_count", "stream_completed",
})
_STREAM_KEYS = frozenset({"chunk_count", "stream_completed"})

RECAPTURE_HINT = (
    "re-capture with content: briefcase.auto(\"<provider or framework>\", "
    "capture_content=\"full\") or BRIEFCASE_CAPTURE_CONTENT=full, "
    "`briefcase exec --capture-content full -- <command>`, or "
    "@briefcase.capture(capture_content=\"full\")"
)


class DatasetError(Exception):
    """A user-facing error: the message says what happened and how to fix it."""


# -- small helpers ----------------------------------------------------------

def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def case_content_hash(case_input: Any, reference: Any) -> str:
    return sha256_text(canonical({"input": case_input, "reference": reference}))


def case_id_for(case_input: Any) -> str:
    return "c-" + sha256_text(canonical(case_input))[:12]


def jsonable(value: Any) -> Any:
    """Round-trip through JSON so tuples become lists and objects become str."""
    return json.loads(json.dumps(value, default=str))


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise DatasetError(
            f"invalid dataset name {name!r}: use letters, digits, '.', '_' or '-' "
            "(for example --name support-triage)"
        )
    return name


def sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version("briefcase-ai")
    except Exception:
        try:
            import briefcase

            return str(getattr(briefcase, "__version__", "unknown"))
        except Exception:
            return "unknown"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- reading records --------------------------------------------------------

def read_records(path: Any) -> Tuple[List[Tuple[int, dict]], List[str], str]:
    """Read a decision-record JSONL file.

    Returns ``([(line_number, record), ...], [problem, ...], sha256_of_file)``.
    """
    p = Path(path)
    if not p.exists():
        raise DatasetError(
            f"{p} not found. Capture records to a file first: briefcase.observe(\"runs.jsonl\")"
        )
    data = p.read_bytes()
    records: List[Tuple[int, dict]] = []
    problems: List[str] = []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"line {number}: not JSON ({exc.msg})")
            continue
        if not isinstance(obj, dict):
            problems.append(f"line {number}: not a JSON object")
            continue
        records.append((number, obj))
    return records, problems, hashlib.sha256(data).hexdigest()


# -- filters ----------------------------------------------------------------

@dataclass(frozen=True)
class Condition:
    key: str
    op: str  # "=" or "!="
    pattern: str

    def __str__(self) -> str:
        return f"{self.key}{self.op}{self.pattern}"

    def matches(self, record: dict) -> bool:
        value = record_field(record, self.key)
        text = "" if value is None else (value if isinstance(value, str) else canonical(value))
        hit = fnmatch.fnmatchcase(text, self.pattern)
        return hit if self.op == "=" else not hit


def parse_where(items: Optional[Iterable[str]]) -> List[Condition]:
    """``["decision_type=triage, status=ok", "model=claude-*"]`` -> conditions.
    Values are shell-style globs; ``key!=value`` negates."""
    conditions: List[Condition] = []
    for item in items or []:
        for part in re.split(r",\s*(?=[A-Za-z_][\w.]*\s*!?=)", item):
            part = part.strip()
            if not part:
                continue
            match = re.match(r"^([A-Za-z_][\w.]*)\s*(!=|=)\s*(.*)$", part)
            if not match:
                raise DatasetError(
                    f"--where {part!r} is not KEY=VALUE (for example --where decision_type=triage,status=ok)"
                )
            conditions.append(Condition(match.group(1), match.group(2), match.group(3).strip()))
    return conditions


def record_status(record: dict) -> str:
    status = record.get("status")
    if isinstance(status, str) and status:
        return status
    return "error" if record.get("error") else "ok"


def record_field(record: dict, key: str) -> Any:
    if key == "status":
        return record_status(record)
    if key == "model":
        params = record.get("model_parameters")
        if isinstance(params, dict) and params.get("model_name"):
            return params["model_name"]
        return record.get("model")
    value: Any = record
    for part in key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


# -- record -> case ---------------------------------------------------------

def _has_content(value: Any) -> bool:
    if isinstance(value, dict):
        return any(k not in _CONTENT_FREE_KEYS for k in value)
    if isinstance(value, str):
        return not value.startswith("sha256:")
    return value is not None


def _content_state(value: Any) -> Optional[bool]:
    if value is None or value == {} or value == [] or value == "":
        return None
    if isinstance(value, dict) and set(value) <= _STREAM_KEYS:
        return None
    return _has_content(value)


def _literal(text: Any) -> Tuple[Any, bool]:
    if not isinstance(text, str):
        return text, True
    try:
        return jsonable(ast.literal_eval(text)), True
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return text, False


def extract_input(record: dict) -> Tuple[Any, bool]:
    """The call's input as a JSON value, and whether it was decoded exactly.

    ``@capture`` records hold reprs (``"('hi',)"``), decoded back to
    ``{"args": [...], "kwargs": {...}}`` when they are Python literals;
    provider records hold the request content as JSON already.
    """
    inputs = record.get("inputs")
    if not isinstance(inputs, dict):
        return inputs, True
    if set(inputs) <= {"args", "kwargs"}:
        out: Dict[str, Any] = {}
        exact = True
        if "args" in inputs:
            args, ok = _literal(inputs["args"])
            if ok and isinstance(args, list):
                out["args"] = args
            else:
                out["args_text"] = inputs["args"]
                exact = False
        if "kwargs" in inputs:
            kwargs, ok = _literal(inputs["kwargs"])
            if ok and isinstance(kwargs, dict):
                out["kwargs"] = kwargs
            else:
                out["kwargs_text"] = inputs["kwargs"]
                exact = False
        if not out:
            out = {"args": []}
        return out, exact
    return jsonable(inputs), True


def extract_output(record: dict) -> Tuple[Any, bool]:
    outputs = record.get("outputs")
    if not isinstance(outputs, dict):
        return jsonable(outputs), True
    if "result" in outputs and set(outputs) - _STREAM_KEYS <= {"result"}:
        return _literal(outputs["result"])
    content = {k: v for k, v in outputs.items() if k not in _STREAM_KEYS}
    if set(content) == {"content"}:
        return jsonable(content["content"]), True
    return jsonable(content), True


def _redact(value: Any, fields: frozenset) -> Any:
    if not fields:
        return value
    if isinstance(value, dict):
        return {
            k: (REDACTED if k.lower() in fields else _redact(v, fields))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v, fields) for v in value]
    return value


def _sanitize(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {(fn(k) if isinstance(k, str) else k): _sanitize(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v, fn) for v in value]
    return value


def native_sanitizer() -> Optional[Callable[[str], str]]:
    """``briefcase.sanitize.Sanitizer`` as a str -> str function, or None when
    the compiled extension is not available."""
    try:
        from briefcase.sanitize import Sanitizer

        sanitizer = Sanitizer()
        if not isinstance(getattr(sanitizer.sanitize("probe"), "sanitized", None), str):
            return None
    except Exception:
        return None

    def run(text: str) -> str:
        return sanitizer.sanitize(text).sanitized

    return run


@dataclass
class PromoteResult:
    cases: List[dict]
    metadata: dict
    total: int
    skipped: Dict[str, int] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    def explain(self) -> List[str]:
        """One line per skip reason, with the fix where there is one."""
        lines = []
        reasons = {
            "unreadable": "not valid JSON objects",
            "eval_records": "evaluation records (eval.case / eval.run); match them explicitly "
                            "with --where decision_type=eval.case to include them",
            "filtered": "did not match --where",
            "no_content": f"captured without content — {RECAPTURE_HINT}",
            "no_output": "had no recorded output (status error/cancelled, or output captured "
                         "without content); use --where status=ok, or --reference none to label later",
            "duplicate": "duplicate inputs (--dedupe)",
            "sampled_out": "not drawn by --sample",
        }
        for key, text in reasons.items():
            count = self.skipped.get(key, 0)
            if count:
                lines.append(f"{count} of {self.total} records skipped: {text}")
        return lines


def promote(
    records: Iterable[Tuple[int, dict]],
    *,
    source_name: str = "",
    source_sha256: str = "",
    where: Optional[Iterable[str]] = None,
    sample: Optional[int] = None,
    seed: int = 0,
    dedupe: bool = False,
    reference: str = "recorded",
    redact_fields: Optional[Iterable[str]] = None,
    sanitizer: Optional[Callable[[str], str]] = None,
    allow_unsanitized: bool = False,
    problems: Optional[List[str]] = None,
) -> PromoteResult:
    """Build dataset cases from decision records.

    Raises :class:`DatasetError` when content cannot be sanitized and
    ``allow_unsanitized`` was not given.
    """
    if reference not in ("recorded", "none"):
        raise DatasetError(f"--reference must be 'recorded' or 'none', got {reference!r}")
    if sample is not None and sample < 1:
        raise DatasetError("--sample must be a positive number of cases")
    if sanitizer is None and not allow_unsanitized:
        raise DatasetError(
            "cannot sanitize: the briefcase native extension (briefcase.sanitize) is not available, "
            "so PII in inputs/outputs would be copied into the dataset as-is. Install a wheel with the "
            "native core (pip install --force-reinstall briefcase-ai), or pass --allow-unsanitized "
            "to accept unsanitized content."
        )
    conditions = parse_where(where)
    explicit_eval = any(c.key == "decision_type" and c.op == "=" and c.pattern.startswith("eval")
                        for c in conditions)
    redact = frozenset(f.strip().lower() for f in (redact_fields or []) if f.strip())

    skipped: Dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    records = list(records)
    problems = list(problems or [])
    total = len(records) + len(problems)
    if problems:
        skipped["unreadable"] = len(problems)

    candidates: List[dict] = []
    seen: Dict[str, int] = {}
    for _line, record in records:
        dtype = str(record.get("decision_type") or "")
        if dtype in ("eval.case", "eval.run") and not explicit_eval:
            skip("eval_records")
            continue
        if not all(c.matches(record) for c in conditions):
            skip("filtered")
            continue
        # True: holds content; False: hashes/counts only; None: empty.
        in_content = _content_state(record.get("inputs"))
        out_content = _content_state(record.get("outputs"))
        # An empty input is a real zero-argument call only when the output
        # was captured with content; otherwise nothing was captured at all.
        if in_content is False or (in_content is None and not out_content):
            skip("no_content")
            continue
        case_input, exact = extract_input(record)
        ref = None
        if reference == "recorded":
            if record_status(record) != "ok" or out_content is None:
                skip("no_output")
                continue
            if out_content is False:
                skip("no_content")
                continue
            ref, _ = extract_output(record)

        case_input = _redact(case_input, redact)
        ref = _redact(ref, redact)
        if sanitizer is not None:
            case_input = _sanitize(case_input, sanitizer)
            ref = _sanitize(ref, sanitizer)

        cid = case_id_for(case_input)
        if cid in seen:
            if dedupe:
                skip("duplicate")
                continue
            seen[cid] += 1
            cid = f"{cid}-{seen[cid]}"
        else:
            seen[cid] = 1

        meta = {"decision_id": record.get("decision_id"), "decision_type": dtype or None,
                "function_name": record.get("function_name")}
        for key in ("prompt_name", "prompt_version", "context_version"):
            if record.get(key):
                meta[key] = record[key]
        model = record_field(record, "model")
        if model:
            meta["model"] = model
        if not exact:
            meta["input_decoded"] = False
        candidates.append({
            "id": cid,
            "input": case_input,
            "reference": ref,
            "metadata": {k: v for k, v in meta.items() if v is not None},
            "content_sha256": case_content_hash(case_input, ref),
        })

    if sample is not None and sample < len(candidates):
        rng = random.Random(seed)
        keep = set(rng.sample(range(len(candidates)), sample))
        skipped["sampled_out"] = len(candidates) - sample
        candidates = [c for i, c in enumerate(candidates) if i in keep]

    metadata = {
        "source": {"kind": "records", "file": os.path.basename(source_name) if source_name else None,
                   "sha256": source_sha256 or None, "records": total},
        "filters": [str(c) for c in conditions],
        "sample": {"n": sample, "seed": seed} if sample is not None else None,
        "dedupe": dedupe,
        "reference": reference,
        "redacted_fields": sorted(redact),
        "sanitized": sanitizer is not None,
        "skipped": dict(skipped),
    }
    return PromoteResult(candidates, metadata, total, skipped, problems)


# -- editable JSONL ---------------------------------------------------------

def parse_cases_jsonl(text: str) -> Tuple[Optional[dict], List[dict], List[str]]:
    """Parse the hand-editable dataset JSONL.

    Returns ``(header or None, cases, errors)``; every error names its line.
    ``content_sha256`` is recomputed, never trusted. A missing ``id`` is
    derived from the input.
    """
    header = None
    cases: List[dict] = []
    errors: List[str] = []
    ids: Dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {number}: not valid JSON ({exc.msg} at column {exc.colno})")
            continue
        if not isinstance(obj, dict):
            errors.append(f"line {number}: expected a JSON object like "
                          "{\"input\": ..., \"reference\": ...}")
            continue
        if HEADER_KEY in obj:
            if header is not None or cases:
                errors.append(f"line {number}: the {HEADER_KEY!r} header may only be the first line")
            elif not isinstance(obj[HEADER_KEY], dict):
                errors.append(f"line {number}: {HEADER_KEY!r} must be an object")
            else:
                header = obj[HEADER_KEY]
            continue
        unknown = [k for k in obj if k not in CASE_KEYS]
        bad = False
        for key in unknown:
            close = difflib.get_close_matches(key, CASE_KEYS, n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else f" (allowed: {', '.join(CASE_KEYS)})"
            errors.append(f"line {number}: unknown key {key!r}{hint}")
            bad = True
        if "input" not in obj:
            errors.append(f"line {number}: missing required key 'input'")
            bad = True
        metadata = obj.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            errors.append(f"line {number}: 'metadata' must be an object")
            bad = True
        cid = obj.get("id")
        if cid is not None and (not isinstance(cid, str) or not cid.strip()):
            errors.append(f"line {number}: 'id' must be a non-empty string")
            bad = True
        if bad:
            continue
        cid = cid or case_id_for(obj["input"])
        if cid in ids:
            errors.append(f"line {number}: duplicate id {cid!r} (first used on line {ids[cid]})")
            continue
        ids[cid] = number
        ref = obj.get("reference")
        cases.append({
            "id": cid,
            "input": obj["input"],
            "reference": ref,
            "metadata": metadata,
            "content_sha256": case_content_hash(obj["input"], ref),
        })
    return header, cases, errors


def version_hash(cases: List[dict]) -> str:
    return sha256_text(canonical(cases))


def render_jsonl(header: dict, cases: List[dict]) -> str:
    lines = [json.dumps({HEADER_KEY: header}, sort_keys=True, ensure_ascii=False)]
    lines += [json.dumps({k: c.get(k) for k in CASE_KEYS}, ensure_ascii=False) for c in cases]
    return "\n".join(lines) + "\n"


# -- store ------------------------------------------------------------------

@dataclass
class DatasetVersion:
    name: str
    number: int
    header: dict
    cases: List[dict]
    path: Path

    @property
    def tag(self) -> str:
        return f"v{self.number}"

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.tag}"

    @property
    def hash(self) -> str:
        return str(self.header.get("hash", ""))

    def summary(self) -> dict:
        h = self.header
        return {"name": self.name, "version": self.tag, "hash": self.hash, "cases": len(self.cases),
                "created_at": h.get("created_at"), "reference": h.get("reference"),
                "sanitized": h.get("sanitized"), "source": h.get("source"), "filters": h.get("filters"),
                "sample": h.get("sample"), "parent": h.get("parent")}


def parse_ref(ref: str) -> Tuple[str, Optional[str]]:
    name, _, version = ref.partition("@")
    return validate_name(name), (version or None)


def default_home() -> Path:
    env = os.environ.get("BRIEFCASE_HOME")
    return Path(env) if env else Path.home() / ".briefcase"


class DatasetStore:
    """Immutable dataset versions at ``<home>/datasets/<name>/v<N>.jsonl``.

    A version file is written once, with ``O_EXCL``, and made read-only. Its
    header carries the SHA-256 of its cases; loading re-checks it, so an
    edited version file is reported instead of silently used.
    """

    def __init__(self, home: Optional[Path] = None) -> None:
        self.home = Path(home) if home is not None else default_home()
        self.root = self.home / "datasets"

    # listing
    def names(self) -> List[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and _NAME_RE.match(p.name)
                      and any(self._numbers(p.name)))

    def _numbers(self, name: str) -> List[int]:
        directory = self.root / name
        if not directory.is_dir():
            return []
        nums = []
        for p in directory.iterdir():
            m = _VERSION_FILE_RE.match(p.name)
            if m:
                nums.append(int(m.group(1)))
        return sorted(nums)

    def versions(self, name: str) -> List[DatasetVersion]:
        return [self._load(name, n) for n in self._numbers(name)]

    # loading
    def _load(self, name: str, number: int, verify: bool = True) -> DatasetVersion:
        path = self.root / name / f"v{number}.jsonl"
        header, cases, errors = parse_cases_jsonl(path.read_text(encoding="utf-8"))
        ref = f"{name}@v{number}"
        if errors or header is None:
            raise DatasetError(f"{ref} is unreadable ({path}): {errors[0] if errors else 'no header'}")
        if verify and version_hash(cases) != header.get("hash"):
            raise DatasetError(
                f"{ref} was modified after it was created (content hash mismatch in {path}); versions "
                f"are immutable. Restore the file, or save your edit as a new version: "
                f"briefcase dataset import {path} --name {name}"
            )
        return DatasetVersion(name, number, header, cases, path)

    def get(self, ref: str) -> DatasetVersion:
        name, version = parse_ref(ref)
        numbers = self._numbers(name)
        if not numbers:
            known = self.names()
            hint = f" Known: {', '.join(known)}." if known else ""
            raise DatasetError(
                f"no dataset named {name!r}.{hint} Create one: "
                f"briefcase dataset from-records runs.jsonl --name {name}"
            )
        if version is None or version == "latest":
            return self._load(name, numbers[-1])
        m = re.match(r"^v?(\d+)$", version)
        if m and int(m.group(1)) in numbers:
            return self._load(name, int(m.group(1)))
        if re.match(r"^[0-9a-f]{6,64}$", version):
            hits = [v for v in self.versions(name) if v.hash.startswith(version)]
            if len(hits) == 1:
                return hits[0]
        tags = ", ".join(f"v{n}" for n in numbers)
        raise DatasetError(f"{name} has no version {version!r} (versions: {tags}). "
                           f"Run `briefcase dataset show {name}`.")

    # writing
    def save(self, name: str, cases: List[dict], metadata: dict) -> Tuple[DatasetVersion, bool]:
        """Write a new version; return ``(version, created)``. When the cases
        are identical to the latest version, nothing is written and that
        version is returned with ``created=False``."""
        validate_name(name)
        if not cases:
            raise DatasetError("refusing to create an empty dataset version")
        digest = version_hash(cases)
        numbers = self._numbers(name)
        parent = None
        if numbers:
            latest = self._load(name, numbers[-1])
            if latest.hash == digest:
                return latest, False
            parent = latest.hash
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.mkdir(mode=0o700, exist_ok=True)
        directory = self.root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        number = (numbers[-1] if numbers else 0) + 1
        header = {
            "name": name,
            "schema_version": DATASET_SCHEMA_VERSION,
            "sdk_version": sdk_version(),
            "created_at": now_iso(),
            "hash": digest,
            "parent": parent,
            "cases": len(cases),
            **metadata,
        }
        while True:
            header["version"] = f"v{number}"
            path = directory / f"v{number}.jsonl"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                number += 1
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(render_jsonl(header, cases))
            try:
                os.chmod(path, 0o400)
            except OSError:
                pass
            return DatasetVersion(name, number, header, cases, path), True


def diff_versions(a: DatasetVersion, b: DatasetVersion) -> dict:
    left = {c["id"]: c for c in a.cases}
    right = {c["id"]: c for c in b.cases}
    added = [right[k] for k in right if k not in left]
    removed = [left[k] for k in left if k not in right]
    changed = [
        {"id": k, "before": left[k], "after": right[k],
         "fields": [f for f in ("input", "reference", "metadata") if canonical(left[k].get(f))
                    != canonical(right[k].get(f))]}
        for k in left if k in right and canonical(left[k]) != canonical(right[k])
    ]
    meta_changes = {}
    for key in ("filters", "sample", "reference", "sanitized", "redacted_fields", "source"):
        if canonical(a.header.get(key)) != canonical(b.header.get(key)):
            meta_changes[key] = {"before": a.header.get(key), "after": b.header.get(key)}
    return {"from": a.ref, "to": b.ref, "from_hash": a.hash, "to_hash": b.hash,
            "added": added, "removed": removed, "changed": changed,
            "unchanged": sum(1 for k in left if k in right) - len(changed),
            "metadata": meta_changes}


def iter_text(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_text(v)
    elif isinstance(value, list):
        for v in value:
            yield from iter_text(v)
