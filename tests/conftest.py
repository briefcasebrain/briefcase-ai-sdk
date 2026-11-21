"""Collection policy for the facade suite.

A test module is skipped at collection only when its import fails because an
*optional third-party package is not installed*. The allow-list of such
packages is derived from the extras in ``pyproject.toml``. Every other import
failure -- an ``ImportError`` raised by ``briefcase`` itself, a missing
``briefcase`` submodule, an installed-but-incompatible dependency, a
``TypeError`` at import time -- fails collection, so breakage cannot hide
behind a quiet skip. Each skip is listed with its reason in the terminal
summary, whatever ``-r`` flags are in use.
"""

import importlib.util
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

# Imported for its side effect: installs the mocked compiled _core into
# sys.modules before any briefcase import resolves it. Not an unused import.
from . import mock_core  # noqa: F401

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# Distribution name -> importable module root(s), for the distributions whose
# import name is not simply the distribution name with "-" turned into "_".
# Namespace packages list the full dotted root, because the bare top-level
# name ("opentelemetry", "google") says nothing about whether the
# distribution is installed.
_IMPORT_ROOTS: Dict[str, Tuple[str, ...]] = {
    "opentelemetry-api": ("opentelemetry.trace",),
    "opentelemetry-sdk": ("opentelemetry.sdk",),
    "opentelemetry-exporter-otlp-proto-grpc": ("opentelemetry.exporter.otlp.proto.grpc",),
    "opentelemetry-exporter-otlp-proto-http": ("opentelemetry.exporter.otlp.proto.http",),
    "opentelemetry-proto": ("opentelemetry.proto",),
    "weaviate-client": ("weaviate",),
    "google-cloud-logging": ("google.cloud.logging",),
    "pynacl": ("nacl",),
    "llama-index-core": ("llama_index",),
    "ag2": ("autogen",),
    "openai-agents": ("agents",),
    "grpcio": ("grpc",),
    "grpcio-reflection": ("grpc_reflection",),
    "protobuf": ("google.protobuf",),
    # gymnasium brings numpy with it; the gym tests import both.
    "gymnasium": ("gymnasium", "numpy"),
    # moto brings botocore/boto3 with it.
    "moto": ("moto", "botocore", "boto3"),
    "boto3": ("boto3", "botocore"),
}

# The `dev` extra is the test tooling itself. A missing pytest plugin or linter
# is a broken environment, not an optional feature, so only moto (documented
# in pyproject as "suites skip cleanly without it") is skippable from it.
_DEV_SKIPPABLE = {"moto"}

_SELF = "briefcase-ai"


def _parse_extras(pyproject: Path = _PYPROJECT) -> Dict[str, List[str]]:
    """Return ``{extra: [distribution, ...]}`` from ``[project.optional-dependencies]``.

    A small line parser rather than ``tomllib``, which Python 3.9 and 3.10 lack.
    """
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(
        r"^\[project\.optional-dependencies\]\s*$(.*?)(?=^\[)", text, re.M | re.S
    )
    if not match:
        raise RuntimeError(f"no [project.optional-dependencies] table in {pyproject}")
    extras: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for raw in match.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head = re.match(r"^([A-Za-z0-9_.-]+)\s*=\s*\[(.*)$", line)
        if head:
            current = head.group(1)
            extras[current] = []
            line = head.group(2)
        if current is None:
            continue
        for requirement in re.findall(r'"([^"]+)"', line):
            name = re.match(r"[A-Za-z0-9_.-]+", requirement)
            if name:
                extras[current].append(name.group(0).lower())
    return extras


def optional_import_roots(pyproject: Path = _PYPROJECT) -> Dict[str, str]:
    """Return ``{import root: extra}`` for every optional third-party package."""
    roots: Dict[str, str] = {}
    for extra, distributions in _parse_extras(pyproject).items():
        for dist in distributions:
            if dist == _SELF:
                continue
            if extra == "dev" and dist not in _DEV_SKIPPABLE:
                continue
            for root in _IMPORT_ROOTS.get(dist, (dist.replace("-", "_"),)):
                roots.setdefault(root, extra)
    return roots


_OPTIONAL_ROOTS = optional_import_roots()


def _is_installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        # find_spec imports the parents of a dotted name; a missing parent
        # means the module is not installed either.
        return False


def missing_optional_dependency(
    exc: Optional[BaseException], roots: Optional[Dict[str, str]] = None
) -> Optional[Tuple[str, str]]:
    """Classify an import failure.

    Returns ``(module, extra)`` when ``exc`` (or an exception it was raised
    from) is a ``ModuleNotFoundError`` for an allow-listed optional package
    that is genuinely not installed; ``None`` for anything else.

    An allow-listed package that *is* installed never qualifies: its import
    failing means a version mismatch (``mcp`` 2.x dropping
    ``mcp.server.fastmcp``) or a broken install, and that must be seen.
    """
    roots = _OPTIONAL_ROOTS if roots is None else roots
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        name = getattr(exc, "name", None)
        if isinstance(exc, ModuleNotFoundError) and name:
            for root, extra in roots.items():
                related = (
                    name == root
                    or name.startswith(root + ".")
                    # A missing namespace parent ("google.cloud") of the root.
                    or root.startswith(name + ".")
                )
                if related and not _is_installed(root):
                    return name, extra
        exc = exc.__cause__ or exc.__context__
    return None


_SKIPS_KEY = pytest.StashKey[List[Tuple[str, str]]]()


class _FacadeModule(pytest.Module):
    """A test module that skips, loudly, when an optional package is absent."""

    def collect(self):
        try:
            self.obj  # triggers the import
        except self.CollectError as exc:
            missing = missing_optional_dependency(exc.__cause__)
            if missing is None:
                raise
            module, extra = missing
            reason = (
                f"optional dependency {module!r} is not installed "
                f"(pip install 'briefcase-ai[{extra}]')"
            )
            self.config.stash.setdefault(_SKIPS_KEY, []).append(
                (self.nodeid, reason)
            )
            raise pytest.skip.Exception(reason, allow_module_level=True) from exc
        return super().collect()


def pytest_pycollect_makemodule(module_path, parent):
    return _FacadeModule.from_parent(parent, path=module_path)


def pytest_terminal_summary(terminalreporter, config):
    skips = config.stash.get(_SKIPS_KEY, [])
    if not skips:
        return
    terminalreporter.section("test modules skipped: optional dependency missing")
    for nodeid, reason in skips:
        terminalreporter.line(f"SKIPPED {nodeid}: {reason}")
