"""The Briefcase engine (oci-jj): local install state, client settings and the install gate.

The engine is proprietary and is NOT part of this package. What ships here is the open part:
the gRPC contract (``briefcase/engine/_proto``, Apache-2.0, vendored from oci-jj ``proto/``),
the client, the release keys this SDK trusts (``briefcase/engine/keys``) and the installer that
gets the engine from the gated Briefcase portal and verifies it (``briefcase engine install``).
Nothing in this package contains engine binaries, images, source or credentials.

Install state lives under ``~/.briefcase/engine`` (``$BRIEFCASE_HOME/engine``)::

    installed.json          which versions are installed and which one is current
    engine.env              per-install secrets (DB passwords, gRPC tokens), mode 0600
    <version>/engine-manifest.json(.sig)   the verified, signed release manifest
    <version>/bin/oci-bai   the client binary (heavy local work: cohort push), when the release
                            has a build for this OS; otherwise it runs from the server image
    <version>/docker-compose.yml   the single-box stack on the verified images
    tls/                    optional self-signed certificate (``engine install --tls``)

Every engine command calls :func:`gate` first. Without an install (and without an explicit
``BRIEFCASE_ENGINE_ADDR`` / ``OCI_JJ_SERVER`` pointing at an engine someone else runs), the
command prints one line and exits with :data:`EXIT_NOT_INSTALLED` (3) instead of failing
somewhere deep in a gRPC call.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

EXIT_NOT_INSTALLED = 3
NOT_INSTALLED = "engine not installed — `briefcase engine install`"
HELP_SUFFIX = " (engine not installed — `briefcase engine install`)"

ENV_ADDR = "BRIEFCASE_ENGINE_ADDR"
ENV_LICENSE = "BRIEFCASE_LICENSE_TOKEN"
ENV_PLATFORM = "BRIEFCASE_PLATFORM_URL"
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


def current_install(env: Optional[Mapping[str, str]] = None) -> Optional[Dict[str, Any]]:
    """The record of the current install, or ``None``."""
    state = load_state(env)
    record = (state.get("installs") or {}).get(state.get("current") or "")
    return record if isinstance(record, dict) else None


def installed(env: Optional[Mapping[str, str]] = None) -> bool:
    return current_install(env) is not None


def configured_address(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """An engine address the user set explicitly (an engine they run themselves)."""
    env = os.environ if env is None else env
    return env.get(ENV_ADDR) or env.get(ENV_SERVER) or None


def available(env: Optional[Mapping[str, str]] = None, server: Optional[str] = None) -> bool:
    """Whether engine commands may run: installed here, or pointed at an engine elsewhere."""
    return bool(server) or installed(env) or configured_address(env) is not None


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
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


def gateway_address(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    record = current_install(env) or {}
    return env.get("BRIEFCASE_ENGINE_GATEWAY") or record.get("gateway") or DEFAULT_GATEWAY


def help_text(text: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Command help, marked when the engine is not installed (shown in ``briefcase --help``)."""
    try:
        return text if available(env) else text + HELP_SUFFIX
    except Exception:  # noqa: BLE001 - help must always render
        return text


def not_installed(as_json: bool = False) -> int:
    """Print the not-installed line and return :data:`EXIT_NOT_INSTALLED`."""
    if as_json:
        print(json.dumps({"error": "engine_not_installed", "message": NOT_INSTALLED,
                          "fix": "briefcase engine install"}))
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
               next_off: str = "briefcase engine install") -> Dict[str, Any]:
    """A ``briefcase status`` row for a feature that needs the engine."""
    if available():
        return {"id": feature_id, "title": title, "state": "on", "detail": detail_on, "next": None}
    return {"id": feature_id, "title": title, "state": "off", "detail": NOT_INSTALLED, "next": next_off}
