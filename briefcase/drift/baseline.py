"""Baseline-versus-window drift for decision records.

Build a small profile from a set of records (a JSONL file, a time range, or
the last N), save it, and later compare a window of records against it::

    from briefcase.drift.baseline import build_profile, check, select_records

    base = build_profile(select_records("runs.jsonl", last=500), name="prod")
    result = check(base, select_records("today.jsonl"))
    for signal in result["signals"]:
        print(signal["state"], signal["reading"])

A profile holds only aggregates and hashes (quantile tables, bin
proportions, counts, hashed labels, hashed character n-gram bins, and an
optional embedding centroid), never prompt or output text, so it is safe to
commit or share. Everything is pure Python and stdlib only.

Each signal reports ``value``, ``threshold``, sample sizes and a
plain-language ``reading``; ``state`` is ``ok``, ``attention``,
``insufficient_data`` (fewer than ``min_n`` records on a side; nothing is
scored) or ``unavailable`` (the records do not carry that field). Formulas
and their limits are in :mod:`briefcase.drift._stats` and
``docs/features/drift-baseline.md``. No signal claims statistical
significance beyond its own test: PSI and Jensen-Shannon thresholds are
rules of thumb; the KS, two-proportion and chi-square figures are the
textbook asymptotic tests and are reported as such.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import statistics
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

from briefcase.drift import _stats

PROFILE_VERSION = 1

Embed = Callable[[List[str]], Sequence[Sequence[float]]]

#: Documented defaults; override any of them with ``thresholds=``.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "min_n": 30,              # records needed on each side before a signal is scored
    "psi": 0.25,              # PSI above this: large distribution shift (rule of thumb)
    "ks": 0.2,                # KS D above this AND above the 5% critical value
    "ks_alpha": 0.05,
    "mean_rel_change": 0.25,  # +-25% change in mean tokens / cost per call
    "rate_alpha": 0.01,       # two-proportion test level (Bonferroni across finish reasons)
    "rate_min_abs": 0.02,     # and at least 2 percentage points of change
    "model_mix_jsd": 0.1,
    "model_new_share": 0.05,  # a model absent from the baseline above 5% of the window
    "category_jsd": 0.1,
    "lexical_jsd": 0.1,       # flagged only above max(this, 1.5 x baseline noise floor)
    "embedding_distance": 0.05,  # 1 - cosine(centroid_baseline, centroid_window)
}

#: Finish reasons that mean the provider refused or filtered the answer, as
#: found in the installed SDKs: openai 3.57.0 ``content_filter``; anthropic
#: 2.12.0 ``refusal``; google-genai 3.6.1 ``SAFETY``, ``RECITATION``,
#: ``BLOCKLIST``, ``PROHIBITED_CONTENT``, ``SPII``, ``IMAGE_SAFETY``,
#: ``IMAGE_PROHIBITED_CONTENT``, ``MODEL_ARMOR``; botocore 1.44.9
#: ``guardrail_intervened``, ``content_filtered``.
REFUSAL_REASONS = frozenset({
    "content_filter", "refusal", "safety", "recitation", "blocklist", "prohibited_content",
    "spii", "image_safety", "image_prohibited_content", "model_armor",
    "guardrail_intervened", "content_filtered",
})

_CATEGORY_MAX_LABELS = 20
_CATEGORY_MAX_CHARS = 40
_NUMERIC = ("latency_ms", "output_tokens", "output_chars")
_MEANS = ("prompt_tokens", "total_tokens", "cost_usd")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


# -- record access ------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dig(record: Dict[str, Any], path: str) -> Any:
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _text(record: Dict[str, Any]) -> Optional[str]:
    outputs = record.get("outputs")
    if not isinstance(outputs, dict):
        return None
    for key in ("content", "result"):
        if isinstance(outputs.get(key), str):
            return outputs[key]
    return None


def _cost(record: Dict[str, Any]) -> Optional[float]:
    value = _num(record.get("cost_usd"))
    if value is not None:
        return value
    cost = record.get("cost")
    if isinstance(cost, dict):
        for key in ("total_usd", "usd", "total"):
            if _num(cost.get(key)) is not None:
                return _num(cost.get(key))
        return None
    return _num(cost)


def _features(record: Dict[str, Any]) -> Dict[str, Any]:
    usage = _as_dict(record.get("token_usage"))
    text = _text(record)
    finish = record.get("finish_reason")
    outputs = _as_dict(record.get("outputs"))
    status = record.get("status")
    return {
        "latency_ms": _num(record.get("execution_time_ms")),
        "output_tokens": _num(usage.get("completion_tokens")),
        "prompt_tokens": _num(usage.get("prompt_tokens")),
        "total_tokens": _num(usage.get("total_tokens")),
        "output_chars": float(len(text)) if text is not None else None,
        "cost_usd": _cost(record),
        "text": text,
        "error": status in ("error", "cancelled") or (status is None and bool(record.get("error"))),
        "refusal": bool(outputs.get("refusal"))
        or (isinstance(finish, str) and finish.lower() in REFUSAL_REASONS),
        "finish_reason": str(finish) if finish is not None else None,
        "model": _dig(record, "model_parameters.model_name"),
    }


def normalize_label(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip(" .!\n\t")


def label_hash(text: str) -> str:
    return hashlib.sha256(normalize_label(text).encode("utf-8")).hexdigest()[:16]


# -- selecting records ----------------------------------------------------------

def parse_when(value: str, now: Optional[datetime] = None) -> datetime:
    """An ISO-8601 time, or a relative age such as ``30m``, ``24h``, ``7d``."""
    now = now or datetime.now(timezone.utc)
    match = re.fullmatch(r"(\d+)\s*([smhdw])", value.strip())
    if match:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
        return now - timedelta(**{unit[match.group(2)]: int(match.group(1))})
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _started(record: Dict[str, Any]) -> Optional[datetime]:
    value = record.get("started_at") or record.get("ended_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _matches(record: Dict[str, Any], where: Dict[str, str]) -> bool:
    for key, pattern in where.items():
        value = _dig(record, key)
        if value is None or not fnmatch.fnmatchcase(str(value), pattern):
            return False
    return True


def read_jsonl(path: Union[str, Path]) -> Iterable[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def select_records(source: Union[str, Path, Iterable[Dict[str, Any]]], *,
                   where: Optional[Dict[str, str]] = None, since: Optional[str] = None,
                   until: Optional[str] = None, last: Optional[int] = None,
                   now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Records from a JSONL file or iterable, filtered by ``where``
    (dotted key -> glob, e.g. ``{"model_parameters.model_name": "gpt-4o*"}``),
    a ``since`` / ``until`` time range on ``started_at``, and ``last`` N
    (by ``started_at``)."""
    records = read_jsonl(source) if isinstance(source, (str, Path)) else source
    lo = parse_when(since, now) if since else None
    hi = parse_when(until, now) if until else None
    out = []
    for record in records:
        if where and not _matches(record, where):
            continue
        if lo or hi:
            started = _started(record)
            if started is None or (lo and started < lo) or (hi and started > hi):
                continue
        out.append(record)
    if last is not None:
        out.sort(key=lambda r: _started(r) or datetime.min.replace(tzinfo=timezone.utc))
        out = out[-last:] if last > 0 else []
    return out


# -- profile --------------------------------------------------------------------

def _numeric_profile(values: List[float]) -> Dict[str, Any]:
    ordered = sorted(values)
    edges = _stats.psi_edges(ordered)
    return {
        "n": len(ordered),
        "mean": statistics.fmean(ordered),
        "p50": _stats.quantile(ordered, 0.5),
        "p95": _stats.quantile(ordered, 0.95),
        "quantiles": [round(q, 6) for q in _stats.quantiles(ordered)],
        "psi_edges": edges,
        "psi_props": _stats.proportions(ordered, edges),
    }


def _categorical(texts: List[str]) -> Optional[Dict[str, Any]]:
    labels = [normalize_label(t) for t in texts]
    distinct = set(labels)
    if not (2 <= len(distinct) <= _CATEGORY_MAX_LABELS):
        return None
    if any(len(label) > _CATEGORY_MAX_CHARS or not label for label in distinct):
        return None
    counts: Dict[str, int] = {}
    for text in texts:
        key = label_hash(text)
        counts[key] = counts.get(key, 0) + 1
    return {"hash": "sha256-16 of the normalized label", "n": len(texts), "counts": counts}


def _lexical(texts: List[str]) -> Dict[str, Any]:
    normalized = [normalize_label(t) for t in texts]
    floor = None
    if len(normalized) >= 4:
        floor = _stats.jsd(_stats.ngram_buckets(normalized[0::2]),
                           _stats.ngram_buckets(normalized[1::2]))
    return {"n": len(texts), "ngram": 3, "buckets": 4096,
            "counts": _stats.ngram_buckets(normalized), "noise_floor": floor}


def _centroid(vectors: Sequence[Sequence[float]]) -> List[float]:
    dims = len(vectors[0])
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(dims)]


def build_profile(records: Iterable[Dict[str, Any]], name: str = "baseline", *,
                  embed: Optional[Embed] = None, categories: bool = True,
                  source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Aggregate ``records`` into a content-free profile."""
    records = list(records)
    feats = [_features(r) for r in records]
    times = [t for t in (_started(r) for r in records) if t]
    profile: Dict[str, Any] = {
        "profile_version": PROFILE_VERSION,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n": len(feats),
        "source": dict(source or {}),
        "time_range": [min(times).isoformat(), max(times).isoformat()] if times else None,
        "numeric": {},
        "means": {},
        "rates": {
            "error": {"x": sum(f["error"] for f in feats), "n": len(feats)},
            "refusal": {"x": sum(f["refusal"] for f in feats), "n": len(feats)},
        },
        "finish_reasons": {},
        "models": {},
        "categories": None,
        "lexical": None,
        "embedding": None,
    }
    for key in _NUMERIC:
        values = [f[key] for f in feats if f[key] is not None]
        if values:
            profile["numeric"][key] = _numeric_profile(values)
    for key in _MEANS:
        values = [f[key] for f in feats if f[key] is not None]
        if values:
            profile["means"][key] = {"n": len(values), "mean": statistics.fmean(values)}
    finish = [f["finish_reason"] for f in feats if f["finish_reason"]]
    for reason in finish:
        profile["finish_reasons"][reason] = profile["finish_reasons"].get(reason, 0) + 1
    profile["finish_reasons_n"] = len(finish)
    for f in feats:
        if f["model"]:
            model = str(f["model"])
            profile["models"][model] = profile["models"].get(model, 0) + 1
    texts = [f["text"] for f in feats if f["text"]]
    if texts:
        if categories:
            profile["categories"] = _categorical(texts)
        profile["lexical"] = _lexical(texts)
        if embed is not None:
            vectors = list(embed(texts))
            if vectors:
                profile["embedding"] = {"n": len(vectors), "centroid": _centroid(vectors)}
    return profile


# -- comparison -----------------------------------------------------------------

def _fmt(key: str, value: float) -> str:
    if key == "latency_ms":
        return f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.0f} ms"
    if key == "cost_usd":
        return f"${value:.6f}"
    return f"{value:,.0f}" if abs(value) >= 10 else f"{value:.2f}"


_LABELS = {
    "latency_ms": "latency", "output_tokens": "output tokens", "output_chars": "output length",
    "prompt_tokens": "prompt tokens per call", "total_tokens": "total tokens per call",
    "cost_usd": "cost per call",
}


def _signal(name: str, kind: str, state: str, reading: str, *, value: Any = None,
            threshold: Any = None, n_baseline: Optional[int] = None,
            n_window: Optional[int] = None, **details: Any) -> Dict[str, Any]:
    out = {"signal": name, "kind": kind, "state": state, "value": value,
           "threshold": threshold, "n_baseline": n_baseline, "n_window": n_window,
           "reading": reading}
    if details:
        out["details"] = details
    return out


def _too_few(name: str, kind: str, nb: int, nw: int, min_n: int) -> Dict[str, Any]:
    return _signal(name, kind, "insufficient_data",
                   f"not scored: needs at least {min_n} records on each side, has {nb} in the "
                   f"baseline and {nw} in the window",
                   threshold=min_n, n_baseline=nb, n_window=nw)


def _direction(old: float, new: float) -> str:
    return "rose" if new > old else "fell" if new < old else "stayed"


def _change(label: str, key: str, old: float, new: float) -> str:
    a, b = _fmt(key, old), _fmt(key, new)
    if a == b:
        return f"{label} stayed at {a}"
    return f"{label} {_direction(old, new)} from {a} to {b}"


def _distribution_signals(key: str, base: Dict[str, Any], values: List[float],
                          t: Dict[str, float]) -> List[Dict[str, Any]]:
    label = _LABELS[key]
    nb, nw = base["n"], len(values)
    min_n = int(t["min_n"])
    if nb < min_n or nw < min_n:
        return [_too_few(f"{key}.psi", "psi", nb, nw, min_n),
                _too_few(f"{key}.ks", "ks", nb, nw, min_n)]
    ordered = sorted(values)
    p50, p95 = _stats.quantile(ordered, 0.5), _stats.quantile(ordered, 0.95)
    shift = (_change(f"p95 {label}", key, base["p95"], p95) + "; "
             + _change("p50", key, base["p50"], p50))
    actual = _stats.proportions(values, base["psi_edges"])
    psi = _stats.psi(base["psi_props"], actual)
    d = _stats.ks_against_quantiles(base["quantiles"], values)
    critical = _stats.ks_critical(nb, nw, t["ks_alpha"])
    ks_flag = d > t["ks"] and d > critical
    return [
        _signal(f"{key}.psi", "psi", "attention" if psi > t["psi"] else "ok",
                f"{shift} (PSI {psi:.3f})", value=round(psi, 6), threshold=t["psi"],
                n_baseline=nb, n_window=nw, bins=len(actual)),
        _signal(f"{key}.ks", "ks", "attention" if ks_flag else "ok",
                f"{shift} (KS D {d:.3f}; asymptotic {t['ks_alpha']:.0%} critical value "
                f"{critical:.3f})", value=round(d, 6), threshold=t["ks"],
                n_baseline=nb, n_window=nw, critical_value=round(critical, 6)),
    ]


def _mean_signal(key: str, base: Dict[str, Any], values: List[float],
                 t: Dict[str, float]) -> Dict[str, Any]:
    nb, nw = base["n"], len(values)
    if nb < t["min_n"] or nw < t["min_n"]:
        return _too_few(f"{key}.mean", "mean", nb, nw, int(t["min_n"]))
    old, new = base["mean"], statistics.fmean(values)
    rel = (new - old) / old if old else (0.0 if new == old else float("inf"))
    return _signal(
        f"{key}.mean", "mean", "attention" if abs(rel) > t["mean_rel_change"] else "ok",
        (_change(f"mean {_LABELS[key]}", key, old, new)
         + (f" ({rel:+.1%})" if _fmt(key, old) != _fmt(key, new) else "")),
        value=round(rel, 6), threshold=t["mean_rel_change"], n_baseline=nb, n_window=nw,
        baseline_mean=old, window_mean=new)


def _rate_signal(name: str, label: str, x1: int, n1: int, x2: int, n2: int,
                 t: Dict[str, float], alpha: float) -> Dict[str, Any]:
    if n1 < t["min_n"] or n2 < t["min_n"]:
        return _too_few(name, "two_proportion", n1, n2, int(t["min_n"]))
    z, p = _stats.two_proportion_z(x1, n1, x2, n2)
    r1, r2 = x1 / n1, x2 / n2
    flag = p < alpha and abs(r2 - r1) >= t["rate_min_abs"]
    small = min(x1 + x2, (n1 - x1) + (n2 - x2)) < 10
    note = "; few events, the normal approximation is rough" if small else ""
    return _signal(
        name, "two_proportion", "attention" if flag else "ok",
        (f"{label} stayed at {r1:.1%} " if f"{r1:.1%}" == f"{r2:.1%}" else
         f"{label} {_direction(r1, r2)} from {r1:.1%} to {r2:.1%} ")
        + f"(two-proportion z = {z:.2f}, p = {p:.3g}{note})",
        value=round(r2 - r1, 6), threshold=t["rate_min_abs"], n_baseline=n1, n_window=n2,
        z=round(z, 6), p_value=p, alpha=alpha)


def check(profile: Dict[str, Any], records: Iterable[Dict[str, Any]], *,
          thresholds: Optional[Dict[str, float]] = None,
          embed: Optional[Embed] = None) -> Dict[str, Any]:
    """Compare a window of records against a profile."""
    t = dict(DEFAULT_THRESHOLDS)
    t.update(thresholds or {})
    feats = [_features(r) for r in records]
    nw = len(feats)
    signals: List[Dict[str, Any]] = []

    for key in _NUMERIC:
        base = profile["numeric"].get(key)
        values = [f[key] for f in feats if f[key] is not None]
        if base is None or not values:
            signals.append(_signal(f"{key}.psi", "psi", "unavailable",
                                   f"{_LABELS[key]} is not recorded on "
                                   f"{'the baseline' if base is None else 'the window'} records",
                                   n_baseline=(base or {}).get("n", 0), n_window=len(values)))
            continue
        signals.extend(_distribution_signals(key, base, values, t))

    for key in _MEANS:
        base = profile["means"].get(key)
        values = [f[key] for f in feats if f[key] is not None]
        if base is None or not values:
            continue
        signals.append(_mean_signal(key, base, values, t))

    for name, label in (("error", "error rate"), ("refusal", "refusal rate")):
        base = profile["rates"][name]
        signals.append(_rate_signal(f"{name}.rate", label, base["x"], base["n"],
                                    sum(f[name] for f in feats), nw, t, t["rate_alpha"]))

    window_finish: Dict[str, int] = {}
    for f in feats:
        if f["finish_reason"]:
            window_finish[f["finish_reason"]] = window_finish.get(f["finish_reason"], 0) + 1
    reasons = sorted(set(profile["finish_reasons"]) | set(window_finish))
    if reasons:
        n1, n2 = profile.get("finish_reasons_n", 0), sum(window_finish.values())
        alpha = t["rate_alpha"] / len(reasons)  # Bonferroni across the reasons tested
        for reason in reasons:
            signals.append(_rate_signal(
                f"finish_reason.{reason}", f"share of finish_reason={reason}",
                profile["finish_reasons"].get(reason, 0), n1, window_finish.get(reason, 0), n2,
                t, alpha))

    window_models: Dict[str, int] = {}
    for f in feats:
        if f["model"]:
            window_models[str(f["model"])] = window_models.get(str(f["model"]), 0) + 1
    if profile["models"] or window_models:
        nb_m, nw_m = sum(profile["models"].values()), sum(window_models.values())
        if nb_m < t["min_n"] or nw_m < t["min_n"]:
            signals.append(_too_few("model_mix", "jsd", nb_m, nw_m, int(t["min_n"])))
        else:
            value = _stats.jsd(profile["models"], window_models)
            new = {m: c / nw_m for m, c in window_models.items() if m not in profile["models"]}
            gone = sorted(m for m in profile["models"] if m not in window_models)
            big_new = sorted(m for m, share in new.items() if share > t["model_new_share"])
            parts = [f"model mix JSD {value:.3f}"]
            if new:
                parts.append("new: " + ", ".join(f"{m} ({s:.0%})" for m, s in sorted(new.items())))
            if gone:
                parts.append("no longer seen: " + ", ".join(gone))
            signals.append(_signal(
                "model_mix", "jsd",
                "attention" if value > t["model_mix_jsd"] or big_new else "ok",
                "; ".join(parts), value=round(value, 6), threshold=t["model_mix_jsd"],
                n_baseline=nb_m, n_window=nw_m, new_models=sorted(new), missing_models=gone))

    texts = [f["text"] for f in feats if f["text"]]
    signals.extend(_content_signals(profile, texts, t, embed))

    computed = [s for s in signals if s["state"] in ("ok", "attention")]
    attention = [s for s in signals if s["state"] == "attention"]
    state = "attention" if attention else "ok" if computed else "insufficient_data"
    return {
        "baseline": profile.get("name"),
        "state": state,
        "n_baseline": profile.get("n"),
        "n_window": nw,
        "thresholds": t,
        "signals": signals,
        "attention": [s["signal"] for s in attention],
    }


def _content_signals(profile: Dict[str, Any], texts: List[str], t: Dict[str, float],
                     embed: Optional[Embed]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    min_n = int(t["min_n"])
    cats = profile.get("categories")
    if cats:
        nb, nw = cats["n"], len(texts)
        if nb < min_n or nw < min_n:
            out.append(_too_few("output_categories", "jsd", nb, nw, min_n))
        else:
            window: Dict[str, int] = {}
            for text in texts:
                key = label_hash(text)
                window[key] = window.get(key, 0) + 1
            value = _stats.jsd(cats["counts"], window)
            stat, df, p, min_expected = _stats.chi_square_homogeneity(cats["counts"], window)
            unseen = sum(c for k, c in window.items() if k not in cats["counts"]) / nw
            note = "; some expected counts are under 5, so the chi-square p-value is rough" \
                if min_expected < 5 else ""
            out.append(_signal(
                "output_categories", "jsd", "attention" if value > t["category_jsd"] else "ok",
                f"label distribution JSD {value:.3f} over {len(cats['counts'])} baseline labels; "
                f"{unseen:.0%} of window outputs are labels never seen in the baseline "
                f"(chi-square {stat:.2f}, df {df}, p = {p:.3g}{note})",
                value=round(value, 6), threshold=t["category_jsd"], n_baseline=nb, n_window=nw,
                chi_square=round(stat, 6), df=df, p_value=p))
    lex = profile.get("lexical")
    if lex:
        nb, nw = lex["n"], len(texts)
        if nb < min_n or nw < min_n:
            out.append(_too_few("output_lexical", "jsd", nb, nw, min_n))
        else:
            window_counts = _stats.ngram_buckets(normalize_label(x) for x in texts)
            value = _stats.jsd(lex["counts"], window_counts)
            floor = lex.get("noise_floor") or 0.0
            threshold = max(t["lexical_jsd"], 1.5 * floor)
            out.append(_signal(
                "output_lexical", "jsd", "attention" if value > threshold else "ok",
                f"character 3-gram JSD {value:.3f} between baseline and window outputs "
                f"(baseline split-half noise floor {floor:.3f})",
                value=round(value, 6), threshold=round(threshold, 6), n_baseline=nb,
                n_window=nw, noise_floor=floor))
    emb = profile.get("embedding")
    if emb and embed is not None and texts:
        vectors = list(embed(texts))
        if emb["n"] < min_n or len(vectors) < min_n:
            out.append(_too_few("output_embedding", "cosine", emb["n"], len(vectors), min_n))
        else:
            cos = _stats.cosine(emb["centroid"], _centroid(vectors))
            distance = 1 - cos if cos is not None else None
            out.append(_signal(
                "output_embedding", "cosine",
                "attention" if distance is not None and distance > t["embedding_distance"]
                else "ok",
                f"embedding centroid moved by cosine distance {distance:.4f}"
                if distance is not None else "embedding centroids could not be compared",
                value=distance, threshold=t["embedding_distance"], n_baseline=emb["n"],
                n_window=len(vectors)))
    elif emb and embed is None:
        out.append(_signal("output_embedding", "cosine", "unavailable",
                           "the baseline has an embedding centroid; pass embed= to compare it"))
    return out


# -- storage --------------------------------------------------------------------

def baselines_dir() -> Path:
    env = os.environ.get("BRIEFCASE_HOME")
    root = Path(env).expanduser() if env else Path.home() / ".briefcase"
    return root / "baselines"


def check_name(name: str) -> str:
    if not _NAME.match(name):
        raise ValueError(
            f"baseline name {name!r} must be letters, digits, '.', '_' or '-' (max 100)")
    return name


def profile_path(name: str) -> Path:
    return baselines_dir() / f"{check_name(name)}.json"


def save_profile(profile: Dict[str, Any], name: Optional[str] = None) -> Path:
    path = profile_path(name or profile["name"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(profile, indent=2, sort_keys=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data + "\n")
    os.replace(tmp, path)
    return path


def load_profile(name_or_path: Union[str, Path]) -> Dict[str, Any]:
    candidate = Path(name_or_path)
    path = candidate if candidate.suffix == ".json" and candidate.exists() else profile_path(
        str(name_or_path))
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("profile_version") != PROFILE_VERSION:
        raise ValueError(f"{path} is not a version {PROFILE_VERSION} baseline profile")
    return data


def list_profiles() -> List[Dict[str, Any]]:
    out = []
    directory = baselines_dir()
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("profile_version") != PROFILE_VERSION:
            continue
        out.append({"name": data.get("name"), "n": data.get("n"),
                    "created_at": data.get("created_at"), "time_range": data.get("time_range"),
                    "models": sorted(data.get("models") or {}), "path": path.name})
    return out


__all__ = [
    "DEFAULT_THRESHOLDS",
    "PROFILE_VERSION",
    "REFUSAL_REASONS",
    "baselines_dir",
    "build_profile",
    "check",
    "list_profiles",
    "load_profile",
    "parse_when",
    "profile_path",
    "save_profile",
    "select_records",
]
