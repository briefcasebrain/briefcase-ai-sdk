"""``briefcase dataset publish``: put a dataset where the engine's verdict worker may read it.

oci-jj's dataset policy (``rust/crates/oci-jj-server/src/dataset.rs``, commit e89ddbf) accepts
only ``synthetic://<name>`` and ``s3://<OCI_JJ_DATASET_S3_BUCKET>/tenants/<tenant>/<key>`` (key
charset ``[A-Za-z0-9._/-]``, no empty, ``.`` or ``..`` segments; the worker reads ``.parquet`` or
``.csv``). The server presigns a GET URL per job; the worker holds no object-store credentials.
There is no ``cas://`` form: dataset bytes are not stored in the engine's CAS.

So a dataset is published into the local install's MinIO, under the tenant prefix and a
content-addressed key (its sha256), by piping the file into ``mc`` inside the MinIO container
(``docker compose exec -T minio mc pipe``): the MinIO credentials never leave the container and
no extra port is opened. For an engine run elsewhere, upload to that engine's dataset bucket
yourself and use ``briefcase dataset register``.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import List, Mapping

from briefcase.engine import compose as _compose

SUFFIXES = (".parquet", ".csv")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def object_key(tenant: str, digest: str, filename: str) -> str:
    """``tenants/<tenant>/datasets/<sha256>/<name>``, every segment in the engine's allowed charset."""
    name = _UNSAFE.sub("-", filename).strip(".-") or "dataset"
    if not name.endswith(SUFFIXES):
        raise ValueError(f"{filename!r}: the engine reads .parquet or .csv datasets only")
    return f"tenants/{tenant}/datasets/{digest}/{name}"


def uri(key: str, bucket: str = _compose.DATASET_BUCKET) -> str:
    return f"s3://{bucket}/{key}"


def upload_argv(record: Mapping[str, str], key: str, bucket: str = _compose.DATASET_BUCKET) -> List[str]:
    """``docker compose exec -T minio ... mc pipe local/<bucket>/<key>``; the file goes on stdin."""
    script = ('MC_HOST_local="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@127.0.0.1:9000" '
              'exec mc --config-dir /tmp/.mc --quiet pipe "local/$0"')
    return _compose.compose_argv(Path(record["compose"]), Path(record["env_file"]),
                                 "exec", "-T", "minio", "sh", "-c", script, f"{bucket}/{key}")
