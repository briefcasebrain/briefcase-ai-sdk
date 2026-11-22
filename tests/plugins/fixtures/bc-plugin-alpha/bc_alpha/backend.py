"""briefcase.bitemporal_backends: get_backend("alphadb")."""
from briefcase.bitemporal import InMemoryBitemporalStore

BRIEFCASE_PLUGIN_API = 1


class AlphaBackend(InMemoryBitemporalStore):
    """The reference in-memory store under another name. A real backend implements
    append, append_many, history, latest, as_of, keys and __len__."""

    label = "alphadb"
