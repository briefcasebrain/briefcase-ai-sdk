"""Inspect an existing engine project for migration and safe cleanup."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

SECRET_KEYS = (
    "PG_SUPERUSER_PASSWORD",
    "OCI_JJ_APP_PASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "OCI_JJ_WORKER_TOKEN",
    "OCI_JJ_TENANT_TOKENS",
    "OCIBAI_DEDUP_HMAC_KEY",
)
# The credentials a data volume is created with: a replacement leaves the volume unreadable by the server.
VOLUME_BOUND_KEYS = ("PG_SUPERUSER_PASSWORD", "OCI_JJ_APP_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD")
TENANT = "local"
BLOB_BUCKET = "oci-jj-blobs"
DATASET_BUCKET = "oci-jj-datasets"
MINIO_IMAGE = ("bitnamilegacy/minio:2025.7.23"
               "@sha256:8935e75fa5d11295c17171e4aa49efe390a1193cd7f12e4d21b92af9ffef09d7")
PROJECT = "briefcase-engine"
ENGINE_LABEL = "ai.briefcase.engine"  # marks a volume this engine created
VOLUME_KEYS = ("pgdata", "miniodata", "regdata")
REGISTRY_IMAGE = ("registry:2.8.3"
                  "@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373")


def compose_argv(compose_file: Path, env_file: Path, *args: str, docker: Optional[str] = None) -> list:
    return [docker or "docker", "compose", "--project-name", PROJECT,
            "-f", str(compose_file), "--env-file", str(env_file), *args]


def tenant_token(values: Mapping[str, str], role: str = "tenant") -> Optional[str]:
    """The first ``OCI_JJ_TENANT_TOKENS`` entry with that role (``tenant`` or ``reader``)."""
    for entry in (values.get("OCI_JJ_TENANT_TOKENS") or "").split(","):
        parts = entry.strip().split(":")
        if len(parts) >= 2 and parts[0] and (parts[2] if len(parts) > 2 else "tenant") == role:
            return parts[0]
    return None
