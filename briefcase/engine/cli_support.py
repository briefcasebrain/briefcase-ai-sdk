"""Shared plumbing for the engine-backed commands (``graph``, ``env``, ``verify --graph``,
``run attach``): the install gate, one client per invocation, and honest errors."""
from __future__ import annotations

import argparse
from typing import Any, Callable, Optional

from briefcase.cli import _ux

RAW = argparse.RawDescriptionHelpFormatter
FOOTER = ("environment: BRIEFCASE_ENGINE_ADDR or OCI_JJ_SERVER (engine address), OCI_JJ_AUTH_TOKEN, "
          "OCI_JJ_TLS_CA, OCI_JJ_REPO\nexit codes: 0 ok, 1 failed, 2 usage, 3 engine not installed")

# Tests replace this with a factory that returns an EngineClient over a fake transport.
CLIENT_FACTORY: Optional[Callable[..., Any]] = None


def sub(sp, name: str, help_text: str, example: str, rpc: str):
    """A subcommand parser with the one-line purpose, one example and the RPC it uses."""
    p = sp.add_parser(name, help=help_text,
                      description=f"{help_text}\nEngine call: {rpc}.",
                      epilog=f"example:\n  {example}\n\n{FOOTER}", formatter_class=RAW)
    p.add_argument("--repo", dest="engine_repo", metavar="REPO",
                   help="Engine repository (image family); default $OCI_JJ_REPO or rl-gym-env.")
    _ux.add_json_flag(p)
    return p


def make_client(args: Any):
    from briefcase.engine.client import EngineClient

    repo = getattr(args, "engine_repo", None) or getattr(args, "repo", None)
    if CLIENT_FACTORY is not None:
        return CLIENT_FACTORY(server=getattr(args, "server", None), repo=repo)
    return EngineClient(server=getattr(args, "server", None), repo=repo)


def run(args: Any, engine: Any, body: Callable[[Any], int]) -> int:
    """Gate on the install, build the client, run ``body(client)``, turn engine errors into
    one ``error:`` line with a fix."""
    from briefcase import engine as _engine
    from briefcase.engine.client import EngineError

    rc = _engine.gate(args, engine)
    if rc is not None:
        return rc
    try:
        client = make_client(args)
    except EngineError as exc:
        return _ux.fail(str(exc), fix="pip install 'briefcase-ai[engine]'")
    try:
        return body(client)
    except EngineError as exc:
        fix = {"unavailable": "briefcase-ai engine status",
               "UNIMPLEMENTED": "Contact your managed workspace administrator",
               "UNAUTHENTICATED": "briefcase-ai engine status"}.get(exc.code)
        return _ux.fail(str(exc), fix=fix)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def needs_newer_engine(args: Any, engine: Any, what: str, missing: str) -> int:
    """A command the vendored contract cannot serve yet. Says so plainly; exit 1."""
    from briefcase import engine as _engine

    rc = _engine.gate(args, engine)
    if rc is not None:
        return rc
    message = f"{what} requires a newer engine: oci_jj.v1 has no {missing} yet"
    if getattr(args, "json", False):
        _ux.print_json({"error": "requires_newer_engine", "message": message})
        return 1
    return _ux.fail(message, fix="Contact your managed workspace administrator")


def short(value: Any, n: int = 12) -> str:
    text = str(value or "")
    return text[:n] if len(text) > n else text
