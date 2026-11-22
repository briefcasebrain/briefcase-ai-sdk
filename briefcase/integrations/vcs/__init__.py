"""
Multi-provider version control and data versioning integrations.

This package provides a unified client interface over data versioning
providers, all implementing :class:`VcsClientBase`:

- DVC (Data Version Control)
- Nessie (Apache Iceberg metadata service)
- Pachyderm (container-native data versioning)
- ArtiVC (Git-like VCS for artifacts)
- DuckLake (DuckDB plus lakeFS)
- Iceberg (Apache Iceberg table format)
- Git LFS (Git Large File Storage)

Provider SDKs import lazily inside each client, so importing this package
never needs one. Constructing a client whose SDK is absent raises
``ImportError`` naming the pip extra, and an operation with no live
implementation raises ``NotImplementedError``. Placeholder data and version
ids are returned only by a client built with ``mock=True``.

Usage:
    from briefcase.integrations.vcs import DvcClient, NessieClient

    dvc_client = DvcClient(
        repository="my-repo",
        branch="main",
        briefcase_client=client
    )
    data = dvc_client.read_object("data/training.csv")

    nessie_client = NessieClient(
        repository="my-iceberg-catalog",
        branch="main",
        endpoint="https://nessie.example.com"
    )
    with nessie_client:
        nessie_client.create_version("Updated training data")
"""

from typing import Any, Dict, List

from briefcase.integrations.vcs.base import VcsClientBase

# Each provider import degrades gracefully so a broken or partially
# installed provider module never takes the whole package down; __all__
# lists only the clients that imported.
__all__ = ["VcsClientBase"]

try:
    from briefcase.integrations.vcs.dvc import DvcClient  # noqa: F401
    __all__.append("DvcClient")
except ImportError:
    pass

try:
    from briefcase.integrations.vcs.nessie import NessieClient  # noqa: F401
    __all__.append("NessieClient")
except ImportError:
    pass

try:
    from briefcase.integrations.vcs.pachyderm import PachydermClient  # noqa: F401
    __all__.append("PachydermClient")
except ImportError:
    pass

try:
    from briefcase.integrations.vcs.artivc import ArtiVCClient  # noqa: F401
    __all__.append("ArtiVCClient")
except ImportError:
    pass

try:
    from briefcase.integrations.vcs.ducklake import DuckLakeClient  # noqa: F401
    __all__.append("DuckLakeClient")
except ImportError:
    pass

try:
    from briefcase.integrations.vcs.iceberg import IcebergClient  # noqa: F401
    __all__.append("IcebergClient")
except ImportError:
    pass

try:
    from briefcase.integrations.vcs.gitlfs import GitLFSClient  # noqa: F401
    __all__.append("GitLFSClient")
except ImportError:
    pass


#: Built-in providers by name (module path of the client class). A plugin
#: registered under the ``briefcase.vcs_providers`` entry point group cannot
#: take one of these names.
BUILTIN_PROVIDERS: Dict[str, str] = {
    "dvc": "briefcase.integrations.vcs.dvc:DvcClient",
    "nessie": "briefcase.integrations.vcs.nessie:NessieClient",
    "pachyderm": "briefcase.integrations.vcs.pachyderm:PachydermClient",
    "artivc": "briefcase.integrations.vcs.artivc:ArtiVCClient",
    "ducklake": "briefcase.integrations.vcs.ducklake:DuckLakeClient",
    "iceberg": "briefcase.integrations.vcs.iceberg:IcebergClient",
    "gitlfs": "briefcase.integrations.vcs.gitlfs:GitLFSClient",
}
_GROUP = "briefcase.vcs_providers"


def get_provider(name: str) -> Any:
    """The client class for provider ``name``: a built-in, else a plugin.

    Raises KeyError naming the available providers."""
    import importlib

    if name in BUILTIN_PROVIDERS:
        module_name, _, attr = BUILTIN_PROVIDERS[name].partition(":")
        return getattr(importlib.import_module(module_name), attr)
    from briefcase import plugins

    cls = plugins.get(_GROUP, name, BUILTIN_PROVIDERS)
    if cls is None:
        raise KeyError(f"no VCS provider {name!r} ({plugins.explain(_GROUP, name)}); available: "
                       f"{', '.join(available_providers())}")
    return cls


def available_providers() -> List[str]:
    """Built-in names first, then installed plugin names (not imported)."""
    from briefcase import plugins

    return list(BUILTIN_PROVIDERS) + [n for n in plugins.names(_GROUP) if n not in BUILTIN_PROVIDERS]
