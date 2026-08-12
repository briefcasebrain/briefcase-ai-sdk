"""Stdlib-only parsers for inspect-ai and lm-eval-harness log files.

Reads eval artifacts off disk and normalizes them into ``ParsedEvalLog``, whose
cases feed straight into :meth:`EvalRun.ingest`. Neither framework is imported,
so replaying a log needs no eval dependency installed. Parsing is best-effort:
missing optional keys are skipped, and a file that does not look like the
expected format raises ValueError naming that format.

Usage:
    from briefcase.integrations.evals import from_inspect_log, replay

    replay(from_inspect_log("logs/2026-08-12_gsm8k.eval"), exporter=my_exporter)
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from briefcase.integrations.evals.run import EvalRun

# inspect-ai letter grades: correct, incorrect, partial, no-answer.
_GRADE_VALUES = {"C": 1.0, "I": 0.0, "P": 0.5, "N": 0.0}

# ZIP compression method 93. inspect-ai writes .eval entries with it; stdlib
# zipfile only decodes it on Python 3.14+, so older versions need a backend.
_ZIP_ZSTANDARD = 93

# lm-eval sample keys that are not metrics.
_LM_EVAL_NON_METRIC_KEYS = {
    "doc_id", "doc", "doc_hash", "target", "target_hash", "arguments", "resps",
    "filtered_resps", "filter", "metrics", "task", "task_hash", "subtask", "epoch",
    "prompt_hash", "arguments_hash",
}

# lm-eval results entries mix metrics with labels and counts.
_LM_EVAL_NON_METRIC_RESULT_KEYS = {"alias", "name", "sample_len", "samples", "n-samples"}

# 'samples_arc_easy_2026-08-12T07-35-18.286140.jsonl' names its task; rows in
# newer lm-eval releases carry no 'task' key of their own.
_SAMPLES_NAME_RE = re.compile(r"^samples_(?P<task>.+?)_\d{4}-\d{2}-\d{2}T[\d\-.]+$")


@dataclass
class ParsedEvalLog:
    """Normalized view of an eval log file."""

    source: str
    name: str
    model: Optional[str] = None
    cases: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)


# -- inspect-ai ------------------------------------------------------------

def from_inspect_log(path: Union[str, Path]) -> ParsedEvalLog:
    """Parse an inspect-ai ``.json`` log or ``.eval`` archive."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"inspect-ai log not found: {path}")

    if zipfile.is_zipfile(path):
        header, samples = _read_inspect_archive(path)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        header = payload if isinstance(payload, dict) else {}
        samples = header.get("samples") or []

    spec = header.get("eval")
    if not isinstance(spec, dict):
        raise ValueError(
            f"{path} is not an inspect-ai log: expected a top-level 'eval' object "
            "(a .json EvalLog or a .eval archive containing header.json)"
        )

    task = spec.get("task") or "inspect-eval"
    return ParsedEvalLog(
        source="inspect-ai",
        name=str(task),
        model=spec.get("model"),
        cases=[_inspect_case(sample, str(task)) for sample in samples],
        metrics=_inspect_metrics(header.get("results")),
    )


def _read_inspect_archive(path: Path):
    """Return (header, samples) from a .eval zip archive."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "header.json" not in names:
            raise ValueError(
                f"{path} is not an inspect-ai .eval archive: no header.json entry"
            )
        header = json.loads(_read_entry(path, archive, "header.json"))
        sample_names = sorted(
            (n for n in names if n.startswith("samples/") and n.endswith(".json")),
            key=_sample_sort_key,
        )
        samples = [json.loads(_read_entry(path, archive, n)) for n in sample_names]
    return header, samples


def _sample_sort_key(name: str) -> Tuple[int, int, str]:
    """Order 'samples/2_epoch_1.json' before 'samples/10_epoch_1.json'."""
    stem = name.rsplit("/", 1)[-1]
    lead = stem.split("_", 1)[0]
    return (0, int(lead), stem) if lead.isdigit() else (1, 0, stem)


def _read_entry(path: Path, archive: zipfile.ZipFile, name: str) -> bytes:
    """Read one zip entry, decoding zstd entries stdlib zipfile cannot."""
    info = archive.getinfo(name)
    if info.compress_type != _ZIP_ZSTANDARD:
        return archive.read(name)

    decompress = _zstd_decompressor()
    if decompress is None:
        raise ValueError(
            f"{path} stores its entries with zstd compression, which this "
            "Python's zipfile cannot decode. Install a zstd backend "
            "(pip install 'briefcase-ai[evals]', or pip install zstandard), run "
            "on Python 3.14+, or export the log with --log-format json."
        )
    return decompress(_raw_entry_bytes(path, info))


def _raw_entry_bytes(path: Path, info: zipfile.ZipInfo) -> bytes:
    """Return an entry's stored bytes, skipping its local file header."""
    with open(path, "rb") as handle:
        handle.seek(info.header_offset)
        header = handle.read(30)
        if header[:4] != b"PK\x03\x04":
            raise ValueError(f"{path} has a corrupt local header for {info.filename}")
        name_length = int.from_bytes(header[26:28], "little")
        extra_length = int.from_bytes(header[28:30], "little")
        handle.seek(info.header_offset + 30 + name_length + extra_length)
        return handle.read(info.compress_size)


def _zstd_decompressor() -> Optional[Callable[[bytes], bytes]]:
    """Return a zstd decompress callable, or None when no backend is present."""
    try:
        from compression import zstd  # type: ignore[import-not-found]  # Python 3.14+

        return zstd.decompress
    except ImportError:
        pass
    try:
        import zstandard

        return lambda data: zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(data)
        ).read()
    except ImportError:
        pass
    try:
        import pyzstd  # type: ignore[import-not-found]

        return pyzstd.decompress
    except ImportError:
        return None


def _inspect_metrics(results: Any) -> Dict[str, float]:
    metrics: Dict[str, float] = {}
    if not isinstance(results, dict):
        return metrics
    for score in results.get("scores") or []:
        if not isinstance(score, dict):
            continue
        scorer = score.get("name") or score.get("scorer") or "score"
        for metric_name, metric in (score.get("metrics") or {}).items():
            value = metric.get("value") if isinstance(metric, dict) else metric
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[f"{scorer}/{metric_name}"] = float(value)
    return metrics


def _inspect_case(sample: Dict[str, Any], task: str) -> Dict[str, Any]:
    scores = {}
    for name, score in (sample.get("scores") or {}).items():
        value = _score_value(score.get("value") if isinstance(score, dict) else score)
        if value is not None:
            scores[name] = value

    output = sample.get("output") or {}
    choices = output.get("choices") if isinstance(output, dict) else None
    text = None
    if choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            text = _flatten_content(message.get("content"))

    return {
        "case_id": f"{task}/{sample.get('id')}",
        "inputs": _flatten_input(sample.get("input")),
        "outputs": text,
        "target": _flatten_content(sample.get("target")),
        "scores": scores,
        "passed": _passed(scores),
        "metadata": {"epoch": sample.get("epoch")} if sample.get("epoch") else {},
    }


def _score_value(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _GRADE_VALUES.get(value.strip().upper())
    return None


def _flatten_input(value: Any) -> Optional[str]:
    """Flatten a chat-message list into 'role: content' lines."""
    if isinstance(value, list):
        lines = []
        for message in value:
            if isinstance(message, dict):
                role = message.get("role", "user")
                lines.append(f"{role}: {_flatten_content(message.get('content'))}")
            else:
                lines.append(str(message))
        return "\n".join(lines)
    return _flatten_content(value)


def _flatten_content(content: Any) -> Optional[str]:
    """Reduce inspect content (string, list of parts, or dict) to text."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or part.get("content") or "")
            else:
                parts.append(str(part))
        return "".join(str(p) for p in parts)
    return str(content)


def _passed(scores: Dict[str, float]) -> Optional[bool]:
    if not scores:
        return None
    return all(value == 1.0 for value in scores.values())


# -- lm-eval-harness -------------------------------------------------------

def from_lm_eval_results(
    results_path: Union[str, Path],
    samples_path: Optional[Union[str, Path]] = None,
    *,
    task: Optional[str] = None,
) -> ParsedEvalLog:
    """Parse an lm-eval-harness ``results.json``, optionally with a samples jsonl."""
    results_path = Path(results_path)
    if not results_path.exists():
        raise FileNotFoundError(f"lm-eval results not found: {results_path}")

    payload = json.loads(results_path.read_text(encoding="utf-8"))
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, dict) or not results:
        raise ValueError(
            f"{results_path} is not an lm-eval-harness results file: expected a "
            "non-empty top-level 'results' object"
        )

    if task is not None:
        if task not in results:
            raise ValueError(
                f"task {task!r} is not in {results_path}; available: "
                f"{', '.join(sorted(results))}"
            )
        selected = {task: results[task]}
    else:
        selected = results

    tasks = sorted(selected)
    metrics: Dict[str, float] = {}
    for task_name in tasks:
        prefix = f"{task_name}/" if len(tasks) > 1 else ""
        for key, value in _lm_eval_metrics(selected[task_name]).items():
            metrics[f"{prefix}{key}"] = value

    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    cases = _lm_eval_cases(
        samples_path,
        set(tasks) if task else None,
        default_task=_default_task(samples_path, tasks),
    )

    return ParsedEvalLog(
        source="lm-eval-harness",
        name="+".join(tasks),
        model=config.get("model"),
        cases=cases,
        metrics=metrics,
    )


def _lm_eval_metrics(entry: Any) -> Dict[str, float]:
    """Strip ',<filter>' suffixes and drop stderr, labels, and sample counts."""
    metrics: Dict[str, float] = {}
    if not isinstance(entry, dict):
        return metrics
    for key, value in entry.items():
        if key in _LM_EVAL_NON_METRIC_RESULT_KEYS:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        name = key.split(",")[0]
        if name.endswith("_stderr"):
            continue
        metrics[name] = float(value)
    return metrics


def _default_task(
    samples_path: Optional[Union[str, Path]], tasks: List[str]
) -> str:
    """Task name for sample rows that carry none: filename first, then results."""
    if samples_path is not None:
        match = _SAMPLES_NAME_RE.match(Path(samples_path).stem)
        if match:
            return match.group("task")
    return tasks[0] if len(tasks) == 1 else "task"


def _lm_eval_cases(
    samples_path: Optional[Union[str, Path]],
    keep_tasks: Optional[set],
    *,
    default_task: str = "task",
) -> List[Dict[str, Any]]:
    if samples_path is None:
        return []
    samples_path = Path(samples_path)
    if not samples_path.exists():
        raise FileNotFoundError(f"lm-eval samples file not found: {samples_path}")

    # Streamed a line at a time: a leaderboard samples file runs to tens of MB.
    cases = []
    with samples_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            task_name = row.get("task") or default_task
            if keep_tasks is not None and task_name not in keep_tasks:
                continue
            cases.append(_lm_eval_case(row, task_name))
    return cases


def _lm_eval_case(row: Dict[str, Any], task_name: str) -> Dict[str, Any]:
    scores = {
        key: float(value)
        for key, value in row.items()
        if key not in _LM_EVAL_NON_METRIC_KEYS
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    }
    return {
        "case_id": f"{task_name}/{row.get('doc_id')}",
        "inputs": _lm_eval_prompt(row),
        "outputs": _lm_eval_response(row),
        "target": _flatten_content(row.get("target")),
        "scores": scores,
        "passed": _passed(scores),
    }


def _lm_eval_prompt(row: Dict[str, Any]) -> Optional[str]:
    """Pull the request prompt out of `arguments`, list form or gen_args mapping."""
    arguments = row.get("arguments")
    if isinstance(arguments, dict) and arguments:
        first = arguments.get("gen_args_0", arguments[sorted(arguments)[0]])
        if isinstance(first, dict):
            return _flatten_content(first.get("arg_0") or first.get("input"))
        return _flatten_content(first)
    if isinstance(arguments, list) and arguments:
        first = arguments[0]
        if isinstance(first, (list, tuple)) and first:
            return str(first[0])
        if isinstance(first, dict):
            return _flatten_input(first.get("arg_0") or first.get("input"))
        return str(first)
    doc = row.get("doc")
    return json.dumps(doc, sort_keys=True) if isinstance(doc, dict) else None


def _lm_eval_response(row: Dict[str, Any]) -> Optional[str]:
    """Return the generated text, or the JSON responses for loglikelihood tasks.

    A generative task yields one string; a multiple-choice task yields one
    [logprob, is_greedy] pair per choice, which stays JSON rather than being
    flattened into an unreadable concatenation.
    """
    for key in ("filtered_resps", "resps"):
        value = row.get(key)
        if not isinstance(value, list) or not value:
            continue
        if len(value) == 1:
            inner = value[0]
            if isinstance(inner, str):
                return inner
            if isinstance(inner, list) and len(inner) == 1 and isinstance(inner[0], str):
                return inner[0]
        return json.dumps(value)
    return None


# -- replay ----------------------------------------------------------------

def replay(
    parsed: ParsedEvalLog,
    *,
    exporter: Any = None,
    name: Optional[str] = None,
    async_capture: bool = False,
) -> EvalRun:
    """Emit a parsed log as one ``eval.run`` plus one ``eval.case`` per case."""
    run = EvalRun(
        name or parsed.name,
        exporter=exporter,
        async_capture=async_capture,
        model=parsed.model,
        metadata={"source": parsed.source, "metrics": dict(parsed.metrics)},
    )
    run.ingest(parsed.cases)
    run.finish()
    return run


__all__ = [
    "ParsedEvalLog",
    "from_inspect_log",
    "from_lm_eval_results",
    "replay",
]
