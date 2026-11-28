"""Managed engine client settings and legacy migration state."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

EXIT_NOT_INSTALLED = 3
MANAGED_ACCESS = "https://observe.briefcasebrain.io/engine"
NOT_INSTALLED = f"Managed engine access is not configured. Visit {MANAGED_ACCESS}"
HELP_SUFFIX = " (requires managed engine access)"

ENV_ADDR = "BRIEFCASE_ENGINE_ADDR"
# oci-jj's own client settings (ojj-proto auth.rs / go grpcauth), honoured as before.
ENV_SERVER = "OCI_JJ_SERVER"
ENV_TOKEN = "OCI_JJ_AUTH_TOKEN"
ENV_TLS_CA = "OCI_JJ_TLS_CA"
DEFAULT_ADDR = "http://127.0.0.1:50051"
DEFAULT_GATEWAY = "127.0.0.1:8080"
COMPOSE_PROJECT = "briefcase-engine"


def briefcase_home(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("BRIEFCASE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".briefcase"


def engine_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    return briefcase_home(env) / "engine"


def installed_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return engine_dir(env) / "installed.json"


def load_state(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The whole ``installed.json`` (``{}`` when absent or unreadable). Never raises."""
    try:
        data = json.loads(installed_path(env).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class StateUnreadable(RuntimeError):
    """``installed.json`` or ``engine.env`` exists but cannot be read (permissions, corruption). Not the same as
    missing: treating it as empty would generate new passwords for volumes that still hold the old ones, or record
    that no volumes ever existed."""

    fix = "briefcase-ai engine doctor"


def load_state_strict(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Like :func:`load_state`, but only a missing file is ``{}``; any other failure raises."""
    path = installed_path(env)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise StateUnreadable(f"{path} exists but cannot be read ({exc.__class__.__name__}); nothing was "
                              "changed. Fix its permissions or restore it (it records which volumes exist)") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise StateUnreadable(f"{path} is not valid JSON; nothing was changed. Restore it from a backup or remove "
                              "it by hand if you are sure no engine volumes exist") from exc
    if not isinstance(data, dict):
        raise StateUnreadable(f"{path} does not hold a JSON object; nothing was changed")
    return data


def compose_history(state: Dict[str, Any]) -> bool:
    """Did a Compose stack ever run for this state, so that data volumes may exist? A record written before the marker
    existed does not say: absent history is unknown, and unknown counts as yes whenever an install has a stack."""
    marker = state.get("compose_used")
    if marker is None:
        return any(isinstance(r, dict) and r.get("compose") for r in (state.get("installs") or {}).values())
    return bool(marker)


def current_install(env: Optional[Mapping[str, str]] = None) -> Optional[Dict[str, Any]]:
    """The record of the current install, or ``None``."""
    state = load_state(env)
    record = (state.get("installs") or {}).get(state.get("current") or "")
    return record if isinstance(record, dict) else None


def installed(env: Optional[Mapping[str, str]] = None) -> bool:
    return current_install(env) is not None


def configured_address(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """An engine address the user set explicitly (the managed endpoint their administrator supplied)."""
    env = os.environ if env is None else env
    return env.get(ENV_ADDR) or env.get(ENV_SERVER) or None


def available(env: Optional[Mapping[str, str]] = None, server: Optional[str] = None) -> bool:
    """Whether engine commands may run: installed here, or pointed at an engine elsewhere."""
    return bool(server) or installed(env) or configured_address(env) is not None


def read_env_file(path: Path, strict: bool = False) -> Dict[str, str]:
    """The ``KEY=value`` lines of ``path``. With ``strict`` only a missing file is empty; an unreadable one raises
    :class:`StateUnreadable` (so no replacement passwords are generated for volumes that hold the old ones)."""
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    except (OSError, UnicodeDecodeError) as exc:
        if strict:
            raise StateUnreadable(f"{path} exists but cannot be read ({exc.__class__.__name__}); its passwords are "
                                  "not replaced. Fix its permissions or restore it") from exc
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def secrets_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return engine_dir(env) / "engine.env"


def client_settings(env: Optional[Mapping[str, str]] = None, server: Optional[str] = None) -> Dict[str, Any]:
    """Where and how to reach the engine: ``{"addr", "token", "ca", "source"}``.

    Precedence: ``--server`` > ``BRIEFCASE_ENGINE_ADDR`` > ``OCI_JJ_SERVER`` > the local
    install > ``http://127.0.0.1:50051``. The bearer token is ``OCI_JJ_AUTH_TOKEN`` when set,
    else the tenant token generated for the local install; the CA is ``OCI_JJ_TLS_CA``, else
    the local install's self-signed certificate when it was installed with ``--tls``.
    """
    env = os.environ if env is None else env
    record = current_install(env)
    local = {}
    if record:
        local = read_env_file(secrets_path(env))
    if server:
        addr, source = server, "--server"
    elif env.get(ENV_ADDR):
        addr, source = env[ENV_ADDR], ENV_ADDR
    elif env.get(ENV_SERVER):
        addr, source = env[ENV_SERVER], ENV_SERVER
    elif record:
        addr, source = record.get("grpc_addr") or DEFAULT_ADDR, "local install"
    else:
        addr, source = DEFAULT_ADDR, "default"
    local_token = None
    if source == "local install":
        from briefcase.engine.compose import tenant_token

        local_token = tenant_token(local)
    token = env.get(ENV_TOKEN) or local_token
    ca = env.get(ENV_TLS_CA) or ((record or {}).get("tls_ca") if source == "local install" else None)
    return {"addr": addr, "token": token or None, "ca": ca or None, "source": source}


def gateway_address(env: Optional[Mapping[str, str]] = None, server: Optional[str] = None) -> Optional[str]:
    env = os.environ if env is None else env
    gateway = env.get("BRIEFCASE_ENGINE_GATEWAY")
    if gateway or server or configured_address(env):
        return gateway or None
    record = current_install(env) or {}
    return record.get("gateway") or DEFAULT_GATEWAY


def help_text(text: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Command help, marked when the engine is not installed (shown in ``briefcase-ai --help``)."""
    try:
        return text if available(env) else text + HELP_SUFFIX
    except Exception:  # noqa: BLE001 - help must always render
        return text


def not_installed(as_json: bool = False) -> int:
    """Print the not-installed line and return :data:`EXIT_NOT_INSTALLED`."""
    if as_json:
        print(json.dumps({"error": "engine_not_installed", "message": NOT_INSTALLED,
                          "fix": MANAGED_ACCESS}))
    else:
        print(f"error: {NOT_INSTALLED}", file=sys.stderr)
    return EXIT_NOT_INSTALLED


def gate(args: Any = None, engine: Any = None) -> Optional[int]:
    """``None`` when the command may run, else the exit code after printing why not.

    An engine object injected by a caller (tests, embedding code) counts as available; the
    CLI's own lazy placeholder does not.
    """
    if engine is not None and type(engine).__name__ != "_LazyEngine":
        return None
    if available(server=getattr(args, "server", None)):
        return None
    return not_installed(bool(getattr(args, "json", False)))


def status_row(feature_id: str, title: str, detail_on: str,
               next_off: str = MANAGED_ACCESS) -> Dict[str, Any]:
    """A ``briefcase-ai status`` row for a feature that needs the engine."""
    if available():
        return {"id": feature_id, "title": title, "state": "on", "detail": detail_on, "next": None}
    return {"id": feature_id, "title": title, "state": "off", "detail": NOT_INSTALLED, "next": next_off}
