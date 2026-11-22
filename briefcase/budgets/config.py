"""``budgets.toml``: read and write budget definitions.

Location: ``$BRIEFCASE_BUDGETS_FILE`` if set, else ``~/.briefcase/budgets.toml``
(``$BRIEFCASE_HOME`` moves ``~/.briefcase``). Format::

    store = "memory"            # or redis://..., sqlite:///..., postgresql://..., mysql://...
    unpriced = "warn"           # block | allow | warn
    default_max_tokens = 4096

    [budgets.daily]
    usd = 50.0
    soft_usd = 40.0
    window = "day"              # hour | day | month | total  (calendar windows, UTC)
    on_exceed = "block"         # block | warn | degrade

    [budgets.per-tenant]
    usd = 5.0
    per = "tenant"
    window = "month"
    on_exceed = "degrade"
    degrade_to = "gpt-4o-mini"
    models = ["gpt-4o*"]
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from briefcase.budgets._core import EFFECTS, UNPRICED_POLICIES, WINDOWS, Budget

ENV_FILE = "BRIEFCASE_BUDGETS_FILE"
TOP_KEYS = ("store", "unpriced", "default_max_tokens", "on_error")
BUDGET_KEYS = ("usd", "soft_usd", "tokens", "soft_tokens", "window", "per", "models", "where",
               "on_exceed", "degrade_to")


class ConfigError(ValueError):
    pass


def default_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(ENV_FILE):
        return Path(env[ENV_FILE]).expanduser()
    home = env.get("BRIEFCASE_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".briefcase") / "budgets.toml"


def read(path: Optional[Union[str, os.PathLike]] = None) -> Dict[str, Any]:
    """The parsed file (``{}`` when it does not exist). Raises
    :class:`ConfigError` naming the file and the problem."""
    import tomllib

    target = Path(path) if path is not None else default_path()
    if not target.exists():
        return {}
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{target}: {exc}") from exc
    validate(data, str(target))
    return data


def validate(data: Mapping[str, Any], source: str = "budgets.toml") -> None:
    problems: List[str] = []
    for key in data:
        if key not in TOP_KEYS + ("budgets",):
            problems.append(f"unknown key {key!r} (expected one of {TOP_KEYS + ('budgets',)})")
    if "on_error" in data and data["on_error"] not in ("allow", "block"):
        problems.append("on_error must be 'allow' or 'block'")
    if "unpriced" in data and data["unpriced"] not in UNPRICED_POLICIES:
        problems.append(f"unpriced must be one of {UNPRICED_POLICIES}")
    budgets = data.get("budgets", {})
    if not isinstance(budgets, dict):
        problems.append("[budgets] must be a table of named budgets")
        budgets = {}
    for name, spec in budgets.items():
        if not isinstance(spec, dict):
            problems.append(f"budgets.{name} must be a table")
            continue
        for key in spec:
            if key not in BUDGET_KEYS:
                problems.append(f"budgets.{name}: unknown key {key!r}")
        try:
            to_budget(name, spec)
        except (TypeError, ValueError) as exc:
            problems.append(f"budgets.{name}: {exc}")
    if problems:
        raise ConfigError(f"{source}: " + "; ".join(problems))


def to_budget(name: str, spec: Mapping[str, Any]) -> Budget:
    return Budget(name=name, **{k: v for k, v in spec.items() if k in BUDGET_KEYS})


def budgets_from(data: Mapping[str, Any]) -> List[Budget]:
    return [to_budget(name, spec) for name, spec in (data.get("budgets") or {}).items()]


# -- writing (a small TOML subset: strings, numbers, booleans, string lists and tables) --

def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot write {type(value).__name__} to budgets.toml")


def _key(key: str) -> str:
    return key if key.replace("-", "").replace("_", "").isalnum() else json.dumps(key)


def dumps(data: Mapping[str, Any]) -> str:
    lines = ["# briefcase budgets (written by `briefcase budget set`; edit freely).",
             "# Enforced in apps that call briefcase.budgets.enable(). Docs: docs/features/budgets.md"]
    for key in TOP_KEYS:
        if key in data:
            lines.append(f"{key} = {_value(data[key])}")
    for name, spec in (data.get("budgets") or {}).items():
        lines.append("")
        lines.append(f"[budgets.{_key(name)}]")
        for key in BUDGET_KEYS:
            if spec.get(key) not in (None, [], {}):
                lines.append(f"{key} = {_value(spec[key])}")
    return "\n".join(lines) + "\n"


def write(data: Mapping[str, Any], path: Optional[Union[str, os.PathLike]] = None) -> Path:
    validate(data)
    target = Path(path) if path is not None else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(dumps(data), encoding="utf-8")
    os.replace(tmp, target)
    return target


__all__ = ["BUDGET_KEYS", "ConfigError", "EFFECTS", "ENV_FILE", "WINDOWS", "budgets_from",
           "default_path", "dumps", "read", "to_budget", "validate", "write"]
