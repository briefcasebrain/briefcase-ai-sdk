"""Nessie client for Apache Iceberg catalog versioning.

Stdlib only: the client talks to Nessie's REST API (v1) over HTTP with
``urllib``; no pip extra is needed.

- ``read_object(key)`` fetches the content object stored under a content
  key (``GET /contents/{key}?ref={branch}``) and returns its JSON bytes.
- ``write_object(key, data)`` stages a ``PUT`` of a JSON content object
  (for example ``{"type": "ICEBERG_TABLE", "metadataLocation": ...}``).
  Nessie has no uncommitted writes, so nothing reaches the server yet.
- ``create_version(message)`` commits the staged operations
  (``POST /trees/branch/{branch}/commit``) and returns the new commit hash.

``mock=True`` opts into an offline stub (placeholder reads and version
ids, tagged ``"mock": True`` in every metadata dict).
"""

from typing import Any, Dict, List, Optional
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from briefcase._logging import get_logger
from briefcase.integrations.vcs.base import (
    DEFAULT_COMMAND_TIMEOUT,
    NothingToCommitError,
    VcsClientBase,
)

logger = get_logger(__name__)


class NessieClient(VcsClientBase):
    """
    Nessie client for Apache Iceberg metadata versioning.

    Nessie is a metadata versioning system for Apache Iceberg tables,
    enabling Git-like semantics for data lake operations.

    Configuration priority (highest to lowest):
        1. Explicit parameters
        2. Environment variables (NESSIE_ENDPOINT)
        3. Default Nessie server endpoint

    Usage:
        client = NessieClient(
            repository="my-iceberg-catalog",
            branch="main",
            briefcase_client=briefcase_client,
            endpoint="https://nessie.example.com/api/v1"
        )
        client.write_object(
            "db.events",
            b'{"type": "ICEBERG_TABLE", "metadataLocation": "s3://b/m.json", '
            b'"snapshotId": 1, "schemaId": 0, "specId": 0, "sortOrderId": 0}',
        )
        client.create_version("Register events table")
        table = client.read_object("db.events")
    """

    def __init__(
        self,
        repository: str,
        branch: str = "main",
        briefcase_client=None,
        endpoint: Optional[str] = None,
        token: Optional[str] = None,
        api_version: str = "v1",
        mock: bool = False,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        **extra
    ):
        """
        Initialize Nessie client.

        Args:
            repository: Nessie catalog/warehouse name
            branch: Nessie branch name (default: "main")
            briefcase_client: Optional BriefcaseClient for instrumentation
            endpoint: Nessie API endpoint
            token: Authentication token
            api_version: Nessie API version. Only "v1" is implemented.
            mock: Opt into the offline stub (placeholder reads and version ids).
            timeout: Timeout in seconds for each HTTP request.
            **extra: Additional Nessie configuration
        """
        resolved_endpoint = (
            endpoint or
            os.getenv("NESSIE_ENDPOINT") or
            "http://localhost:19120/api/v1"
        )

        super().__init__(
            provider_type="nessie",
            repository=repository,
            branch=branch,
            briefcase_client=briefcase_client,
            endpoint=resolved_endpoint,
            token=token,
            mock=mock,
            **extra
        )

        self.api_version = api_version

        self.timeout = timeout
        self._staged: List[Dict[str, Any]] = []
        if self.endpoint.split(":", 1)[0].lower() not in ("http", "https"):
            raise ValueError(f"Nessie endpoint must be http(s): {self.endpoint!r}")
        self._has_provider = not self.mock

    # -- REST plumbing ---------------------------------------------------

    def _require_v1(self, operation: str) -> None:
        if self.api_version != "v1":
            raise NotImplementedError(
                f"NessieClient.{operation} is implemented for the Nessie v1 "
                f"REST API only (api_version={self.api_version!r})."
            )

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, str]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = self.endpoint.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        # The scheme is validated as http(s) in __init__.
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310
            raw = response.read()
        return json.loads(raw) if raw else None

    def _branch_hash(self) -> str:
        ref = self._request(
            "GET", "/trees/tree/" + urllib.parse.quote(self.branch, safe="")
        )
        return ref["hash"]

    # -- Provider hooks --------------------------------------------------

    def _read_object_impl(self, path: str) -> bytes:
        """Read the content object stored under a content key."""
        if self.mock:
            return b"Mock Nessie catalog: " + path.encode()
        self._require_v1("read_object")
        try:
            content = self._request(
                "GET",
                "/contents/" + urllib.parse.quote(path, safe=""),
                params={"ref": self.branch},
            )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise FileNotFoundError(
                    f"Nessie content not found: {path} on {self.branch}"
                ) from e
            raise
        return json.dumps(content).encode("utf-8")

    def _write_object_impl(
        self,
        path: str,
        data: bytes,
        content_type: str
    ) -> None:
        """Stage a PUT of a JSON content object; ``create_version`` commits it."""
        if self.mock:
            logger.info(f"Mock Nessie: Would update {path} with {len(data)} bytes")
            return
        self._require_v1("write_object")
        try:
            content = json.loads(data)
        except ValueError as e:
            raise ValueError(
                "NessieClient.write_object expects a JSON Nessie content object, "
                'e.g. {"type": "ICEBERG_TABLE", "metadataLocation": "..."}'
            ) from e
        if not isinstance(content, dict) or "type" not in content:
            raise ValueError(
                "NessieClient.write_object expects a JSON object with a 'type' field"
            )
        self._staged.append({
            "type": "PUT",
            "key": {"elements": path.split(".")},
            "content": content,
        })

    def _create_version_impl(
        self,
        message: str,
        metadata: Optional[Dict[str, str]]
    ) -> str:
        """Commit the staged operations to the branch; returns the commit hash."""
        if self.mock:
            return f"nessie-{self.branch}-mock-sha"
        self._require_v1("create_version")
        if not self._staged:
            raise NothingToCommitError(
                "NessieClient.create_version: no staged operations. Stage "
                "content with write_object first."
            )
        expected_hash = self._branch_hash()
        result = self._request(
            "POST",
            "/trees/branch/" + urllib.parse.quote(self.branch, safe="") + "/commit",
            params={"expectedHash": expected_hash},
            body={
                "commitMeta": {
                    "message": message,
                    "properties": dict(metadata or {}),
                },
                "operations": list(self._staged),
            },
        )
        new_hash = (result or {}).get("hash")
        if not new_hash or new_hash == expected_hash:
            raise RuntimeError(
                "Nessie commit did not return a new hash for branch "
                f"{self.branch!r}"
            )
        self._staged = []
        return new_hash
