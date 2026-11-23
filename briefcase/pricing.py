"""Versioned model pricing: packaged data, user layers, validation and a pure-Python estimator.

Layers, lowest to highest precedence (a higher layer replaces a model's whole entry):

1. the packaged ``briefcase/data/pricing/v1.json`` (copied from the Rust core's built-in table,
   unverified; see its ``notice``),
2. ``~/.briefcase/pricing.json`` (``$BRIEFCASE_HOME/pricing.json``), written by ``briefcase pricing set``,
3. the file named by ``$BRIEFCASE_PRICING_FILE``,
4. an explicit ``path`` argument.

Files use the schema the native ``CostCalculator.load_pricing_json`` accepts (``{"models": [...]}``,
USD per 1,000 tokens) plus optional provenance and ``aliases``; unknown fields are tolerated.

``apply`` pushes the table into a native calculator when the extension is importable.
``estimate`` is a pure-Python mirror of the native arithmetic for the simple per-token case only;
it raises :class:`NeedsNative` instead of approximating anything else.
"""
from __future__ import annotations

import difflib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

SCHEMA_VERSION = 1
ENV_FILE = "BRIEFCASE_PRICING_FILE"
LAYER_ORDER = ("packaged", "user", "env", "explicit")


class PricingError(ValueError):
    """A pricing file is invalid. ``errors`` holds one ``path:line: message`` per problem."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


class UnknownModel(KeyError):
    pass


class NeedsNative(RuntimeError):
    """The pure-Python estimator cannot reproduce this calculation exactly."""


def briefcase_home(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get("BRIEFCASE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".briefcase"


def user_file(environ: Optional[Mapping[str, str]] = None) -> Path:
    return briefcase_home(environ) / "pricing.json"


# ---------------------------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------------------------

def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _line_index(text: str) -> List[Tuple[int, str]]:
    """(line number, model_name) for each ``"model_name": "..."`` occurrence, in order."""
    return [(text.count("\n", 0, m.start()) + 1, m.group(1))
            for m in re.finditer(r'"model_name"\s*:\s*"((?:[^"\\]|\\.)*)"', text)]


def _field_line(text: str, start_line: int, end_line: int, key: str, default: int) -> int:
    lines = text.splitlines()
    for n in range(start_line, min(end_line, len(lines)) + 1):
        if f'"{key}"' in lines[n - 1]:
            return n
    return default


def parse_document(text: str, source: str) -> Dict[str, Any]:
    """Validate a pricing document; return it with ``models`` as a list of dict entries.

    Raises :class:`PricingError` with every problem, each anchored ``source:line:``.
    """
    errors: List[str] = []
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PricingError([f"{source}:{exc.lineno}: invalid JSON: {exc.msg} (column {exc.colno})"])
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), list):
        raise PricingError([f'{source}:1: expected an object with a "models" array'])
    version = doc.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        errors.append(f"{source}:{_field_line(text, 1, 10**6, 'schema_version', 1)}: "
                      f"unsupported schema_version {version!r} (this briefcase reads up to {SCHEMA_VERSION}); "
                      "upgrade briefcase-ai")
    index = _line_index(text)
    total_lines = text.count("\n") + 1
    seen: Dict[str, int] = {}
    claimed: Dict[str, Tuple[str, int]] = {}  # alias -> (owner, line)
    for i, entry in enumerate(doc["models"]):
        line = index[i][0] if i < len(index) else 1
        end = (index[i + 1][0] - 1) if i + 1 < len(index) else total_lines

        def err(key: str, msg: str, _line=line, _end=end) -> None:
            errors.append(f"{source}:{_field_line(text, _line, _end, key, _line)}: {msg}")

        if not isinstance(entry, dict):
            errors.append(f"{source}:{line}: models[{i}] must be an object")
            continue
        name = entry.get("model_name")
        if not isinstance(name, str) or not name.strip():
            err("model_name", f"models[{i}]: model_name must be a non-empty string")
            continue
        label = f"model '{name}'"
        if name in seen:
            err("model_name", f"{label}: duplicate model id (first defined on line {seen[name]})")
        seen.setdefault(name, line)
        if not isinstance(entry.get("provider"), str) or not entry["provider"]:
            err("provider", f"{label}: provider must be a non-empty string")
        for key in ("input_cost_per_1k_tokens", "output_cost_per_1k_tokens"):
            v = entry.get(key)
            if not _is_num(v):
                err(key, f"{label}: {key} is required and must be a number")
            elif not math.isfinite(v) or v < 0:  # type: ignore[arg-type, operator]  # _is_num holds here
                err(key, f"{label}: {key} must be finite and not negative (got {v!r})")
        cw = entry.get("context_window")
        if not isinstance(cw, int) or isinstance(cw, bool) or cw <= 0:
            err("context_window", f"{label}: context_window must be an integer greater than 0")
        mo = entry.get("max_output_tokens")
        if mo is not None and (not isinstance(mo, int) or isinstance(mo, bool) or mo <= 0):
            err("max_output_tokens", f"{label}: max_output_tokens must be null or an integer greater than 0")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) and a for a in aliases):
            err("aliases", f"{label}: aliases must be a list of non-empty strings")
            aliases = []
        for a in aliases:
            if a in claimed and claimed[a][0] != name:
                err("aliases", f"{label}: alias '{a}' is already an alias of '{claimed[a][0]}' "
                               f"(line {claimed[a][1]})")
            claimed.setdefault(a, (name, line))
    for a, (owner, line) in claimed.items():
        if a in seen and a != owner:
            errors.append(f"{source}:{line}: alias '{a}' of '{owner}' collides with model id '{a}' "
                          f"(line {seen[a]})")
        elif a == owner:
            errors.append(f"{source}:{line}: alias '{a}' repeats its own model id")
    if errors:
        raise PricingError(errors)
    doc["models"] = [dict(m) for m in doc["models"]]
    return doc


def packaged_text() -> str:
    return resources.files("briefcase.data.pricing").joinpath("v1.json").read_text(encoding="utf-8")


def unpriced_ids() -> List[Dict[str, str]]:
    text = resources.files("briefcase.data.pricing").joinpath("unpriced.json").read_text(encoding="utf-8")
    return list(json.loads(text)["models"])


# ---------------------------------------------------------------------------------------------
# Model id normalisation: a port of CostCalculator::normalize_model_id (cost.rs)
# ---------------------------------------------------------------------------------------------

def normalize_model_id(model_id: str) -> str:
    id_ = model_id.rsplit("/", 1)[-1]
    for region in ("us.", "eu.", "apac.", "global."):
        if id_.startswith(region):
            id_ = id_[len(region):]
            break
    for vendor in ("anthropic.", "amazon.", "cohere.", "mistral.", "meta."):
        if id_.startswith(vendor):
            id_ = id_[len(vendor):]
            break
    pos = id_.find("@")
    if pos > 0:
        id_ = id_[:pos]
    out = id_
    pos = out.rfind("-v")
    if pos >= 0:
        tail = out[pos + 2:]
        if tail and all(c.isascii() and (c.isdigit() or c == ":") for c in tail):
            out = out[:pos]
    pos = out.rfind(":")
    if pos >= 0:
        tail = out[pos + 1:]
        if tail and tail.isascii() and tail.isdigit():
            out = out[:pos]
    if out.endswith("-latest"):
        out = out[:-len("-latest")]
    for pattern in (r"-\d{4}-\d{2}-\d{2}$", r"-\d{8}$"):
        m = re.search(pattern, out)
        if m and len(out) > len(m.group(0)):
            out = out[:m.start()]
            break
    return "claude-haiku-3-5" if out == "claude-3-5-haiku" else out


# ---------------------------------------------------------------------------------------------
# The merged table
# ---------------------------------------------------------------------------------------------

@dataclass
class Layer:
    name: str
    path: Optional[Path]
    models: List[str]
    generated_at: Optional[str] = None


@dataclass
class PricingTable:
    models: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # name -> entry (+ "layer")
    layers: List[Layer] = field(default_factory=list)
    generated_at: Optional[str] = None
    notice: Optional[str] = None

    # -- lookup ---------------------------------------------------------------------------
    def _alias_map(self) -> Dict[str, str]:
        return {a: n for n, e in self.models.items() for a in e.get("aliases") or []}

    def resolve(self, model: str) -> Optional[Tuple[Dict[str, Any], str]]:
        """``(entry, how)`` where how is ``exact``, ``alias`` or ``normalized``; None if unpriced."""
        aliases = self._alias_map()
        if model in self.models:
            return self.models[model], "exact"
        if model in aliases:
            return self.models[aliases[model]], "alias"
        norm = normalize_model_id(model)
        if norm in self.models:
            return self.models[norm], "normalized"
        if norm in aliases:
            return self.models[aliases[norm]], "normalized"
        return None

    def closest(self, model: str, n: int = 3) -> List[str]:
        pool = list(self.models) + list(self._alias_map())
        norm = normalize_model_id(model)
        hits = difflib.get_close_matches(norm, pool, n=n, cutoff=0.5)
        if len(hits) < n:
            for name in pool:
                if (norm in name or name in norm) and name not in hits:
                    hits.append(name)
        return hits[:n]

    def unpriced_hint(self, model: str) -> Optional[Dict[str, str]]:
        names = {model, normalize_model_id(model)}
        return next((u for u in unpriced_ids() if u["id"] in names), None)

    # -- staleness ------------------------------------------------------------------------
    def age_days(self, today: Optional[date] = None) -> Optional[int]:
        return _age(self.generated_at, today)

    def model_age_days(self, model: str, today: Optional[date] = None) -> Optional[int]:
        r = self.resolve(model)
        return _age(r[0].get("verified_on"), today) if r else None

    def unverified(self) -> List[str]:
        return [n for n, e in self.models.items() if not e.get("verified_on")]

    # -- native ---------------------------------------------------------------------------
    def apply(self, calculator: Any = None) -> Optional[int]:
        """Push the merged table into a native ``CostCalculator``; return models loaded.

        Returns None when the native extension is not importable and no calculator was given.
        Only entries above the packaged layer, plus packaged models the calculator does not know,
        are pushed: the native loader replaces a model outright and drops its cache and
        long-context data, so re-pushing identical built-ins would make pricing worse.
        """
        if calculator is None:
            try:
                from briefcase._native import CostCalculator
            except ImportError:
                return None
            calculator = CostCalculator()
        known = set(calculator.get_available_models())
        push = []
        for name, e in self.models.items():
            if e["layer"] == "packaged" and name in known:
                continue
            push.append(_native_entry(e, name))
            push.extend(_native_entry(e, a) for a in e.get("aliases") or [] if e["layer"] != "packaged")
        if not push:
            return 0
        return calculator.load_pricing_json(json.dumps({"models": push}))

    # -- pure-Python estimate -------------------------------------------------------------
    def estimate(self, model: str, input_tokens: int, output_tokens: int, *,
                 cache_read_tokens: int = 0, cache_write_5m_tokens: int = 0,
                 cache_write_1h_tokens: int = 0, rate_card: Optional[str] = None) -> Dict[str, Any]:
        """Per-token cost for the simple case, with the native engine's arithmetic and checks.

        Raises :class:`NeedsNative` for anything it cannot mirror exactly: a non-standard rate
        card, prompt-cache tokens, or an input large enough to hit a long-context tier.
        Raises :class:`UnknownModel` for an unpriced model and ``ValueError`` for token counts the
        native engine rejects (none, or beyond the context window / output cap).
        """
        resolved = self.resolve(model)
        if resolved is None:
            raise UnknownModel(model)
        entry = resolved[0]
        if rate_card is not None and rate_card.strip().lower() not in ("", "standard"):
            raise NeedsNative(f"rate card {rate_card!r} needs native extension (pip install briefcase-ai)")
        if cache_read_tokens or cache_write_5m_tokens or cache_write_1h_tokens:
            raise NeedsNative("prompt-cache pricing needs native extension (pip install briefcase-ai)")
        for v in (input_tokens, output_tokens):
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError("token counts must be non-negative integers")
        if input_tokens + output_tokens == 0:
            raise ValueError("invalid token count: at least one token is required")
        if input_tokens + output_tokens > entry["context_window"]:
            raise ValueError("invalid token count: exceeds the model's context window")
        cap = entry.get("max_output_tokens")
        if cap is not None and output_tokens > cap:
            raise ValueError("invalid token count: exceeds the model's maximum output tokens")
        tiers = (entry.get("rate_card_extras") or {}).get("long_context_tiers") or []
        if any(input_tokens > t["threshold_tokens"] for t in tiers):
            raise NeedsNative("long-context tier pricing needs native extension (pip install briefcase-ai)")
        input_cost = (input_tokens / 1000.0) * entry["input_cost_per_1k_tokens"]
        output_cost = (output_tokens / 1000.0) * entry["output_cost_per_1k_tokens"]
        return {"model_name": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
                "input_cost": input_cost, "output_cost": output_cost, "cache_cost": 0.0,
                "total_cost": input_cost + output_cost + 0.0, "currency": "USD"}


def _native_entry(e: Mapping[str, Any], name: str) -> Dict[str, Any]:
    return {"model_name": name, "provider": e["provider"],
            "input_cost_per_1k_tokens": e["input_cost_per_1k_tokens"],
            "output_cost_per_1k_tokens": e["output_cost_per_1k_tokens"],
            "context_window": e["context_window"], "max_output_tokens": e.get("max_output_tokens")}


def _age(value: Optional[str], today: Optional[date]) -> Optional[int]:
    if not value:
        return None
    try:
        then = datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None
    return ((today or date.today()) - then).days


# ---------------------------------------------------------------------------------------------
# Loading and merging
# ---------------------------------------------------------------------------------------------

def layer_files(path: Optional[os.PathLike] = None,
                environ: Optional[Mapping[str, str]] = None) -> List[Tuple[str, Path]]:
    """The optional file layers that apply, in precedence order (existence not checked here)."""
    env = os.environ if environ is None else environ
    out = [("user", user_file(env))]
    if env.get(ENV_FILE):
        out.append(("env", Path(env[ENV_FILE]).expanduser()))
    if path is not None:
        out.append(("explicit", Path(path)))
    return out


def load_layer(name: str, path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PricingError([f"{path}:1: cannot read pricing file: {exc.strerror or exc}"])
    return parse_document(text, str(path))


def merge(docs: Iterable[Tuple[str, Optional[Path], Dict[str, Any]]]) -> PricingTable:
    """Merge validated documents in order; later layers replace a model's whole entry."""
    table = PricingTable()
    for name, path, doc in docs:
        errors: List[str] = []
        names = [m["model_name"] for m in doc["models"]]
        for m in doc["models"]:
            for a in m.get("aliases") or []:
                owner = table._alias_map().get(a)
                if a in table.models and a not in names:
                    errors.append(f"{path or name}: alias '{a}' of '{m['model_name']}' collides with "
                                  f"model id '{a}' from an earlier layer")
                elif owner and owner != m["model_name"] and owner not in names:
                    errors.append(f"{path or name}: alias '{a}' of '{m['model_name']}' is already an "
                                  f"alias of '{owner}' from an earlier layer")
            if m["model_name"] in table._alias_map() and table._alias_map()[m["model_name"]] != m["model_name"]:
                errors.append(f"{path or name}: model id '{m['model_name']}' is an alias of "
                              f"'{table._alias_map()[m['model_name']]}' in an earlier layer")
        if errors:
            raise PricingError(errors)
        for m in doc["models"]:
            table.models[m["model_name"]] = dict(m, layer=name)
        table.layers.append(Layer(name, path, names, doc.get("generated_at")))
        if name == "packaged":
            table.generated_at = doc.get("generated_at")
            table.notice = doc.get("notice")
    return table


def load(path: Optional[os.PathLike] = None, environ: Optional[Mapping[str, str]] = None) -> PricingTable:
    """Packaged < user < ``$BRIEFCASE_PRICING_FILE`` < explicit ``path``."""
    env = os.environ if environ is None else environ
    docs: List[Tuple[str, Optional[Path], Dict[str, Any]]] = [
        ("packaged", None, parse_document(packaged_text(), "briefcase/data/pricing/v1.json"))]
    for name, p in layer_files(path, env):
        if name == "user" and not p.exists():
            continue
        docs.append((name, p, load_layer(name, p)))
    return merge(docs)


def mtok(per_1k: float) -> str:
    """USD per million tokens for display, without float noise (0.003 -> '3', 5.9999999999999995e-05 -> '0.06')."""
    return f"{per_1k * 1000:.10g}"


# ---------------------------------------------------------------------------------------------
# Which models do my records use?
# ---------------------------------------------------------------------------------------------

_MODEL_KEYS = ("model", "model_name", "gen_ai.request.model", "gen_ai.response.model")
_NESTED = ("inputs", "outputs", "metadata", "attributes", "attrs")


def record_models(path: os.PathLike) -> Tuple[Dict[str, int], int, int]:
    """``({model: record count}, records read, lines skipped)`` from a records JSONL file.

    Only model-id fields are read (top level and one level into inputs/outputs/metadata);
    prompt and response content is never looked at or returned.
    """
    counts: Dict[str, int] = {}
    records = skipped = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(rec, dict):
                skipped += 1
                continue
            records += 1
            found = set()
            for scope in [rec] + [rec[k] for k in _NESTED if isinstance(rec.get(k), dict)]:
                for key in _MODEL_KEYS:
                    v = scope.get(key)
                    if isinstance(v, str) and v:
                        found.add(v)
            for model in found:
                counts[model] = counts.get(model, 0) + 1
    return counts, records, skipped
