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

Provider SDKs import lazily inside each client; a provider whose SDK is
absent runs in mock mode rather than failing at import time.

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
