"""briefcase.vcs_providers: get_provider("alphavcs")."""
from typing import Dict

from briefcase.integrations.vcs import VcsClientBase

BRIEFCASE_PLUGIN_API = 1


class AlphaVcsClient(VcsClientBase):
    """Keeps objects in a dict; versions are counters."""

    def __init__(self, repository: str, branch: str = "main", **kwargs) -> None:
        super().__init__("alphavcs", repository, branch, **kwargs)
        self._objects: Dict[str, bytes] = {}
        self._has_provider = True

    def _read_object_impl(self, path: str) -> bytes:
        return self._objects[path]

    def _write_object_impl(self, path: str, data: bytes, content_type=None) -> None:
        self._objects[path] = data

    def _create_version_impl(self, message: str, metadata=None) -> str:
        return f"v{len(self._objects)}"
