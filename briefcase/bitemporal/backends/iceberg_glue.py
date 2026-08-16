"""AWS Glue catalog Iceberg backend.

A thin authentication-convenience wrapper around
:class:`briefcase.bitemporal.backends.iceberg.IcebergBitemporalBackend`,
which already accepts ``catalog_type="glue"`` against an ambient AWS
credential chain. This subclass adds:

* Cross-account role assumption via ``boto3.client("sts").assume_role``.
* Pre-flight validation that the Glue database exists (it refuses to
  auto-create; Glue databases should be managed explicitly).
* The parent's ``close()`` hook releases the pyiceberg catalog
  reference deterministically for long-running services.

The class surface is intentionally small; the parent adapter does the
heavy lifting.

Install
-------
Requires ``boto3`` in addition to the parent's pyiceberg dependency:
``pip install briefcase-ai[bitemporal-glue]`` or ``pip install boto3``.
The boto3 import is lazy, so this module loads without it installed and
fails with a clear error at construction.
"""

from __future__ import annotations

from typing import Any, Optional

from briefcase.bitemporal.backends.iceberg import IcebergBitemporalBackend


_BOTO3_INSTALL_HINT = (
    "boto3 is required. Install with "
    "'pip install briefcase-ai[bitemporal-glue]' "
    "or 'pip install boto3'."
)


def _require_boto3() -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise ImportError(_BOTO3_INSTALL_HINT) from exc
    return boto3


class GlueIcebergBackend(IcebergBitemporalBackend):
    """Glue-catalog-backed Iceberg bitemporal store.

    Parameters
    ----------
    database
        Existing Glue database (namespace). Must exist; it is not
        auto-created.
    table
        Iceberg table name within ``database``.
    s3_warehouse
        S3 URI where Iceberg table data lives (e.g. ``s3://my-lake/warehouse``).
    region
        AWS region. Forwarded to boto3/pyiceberg.
    role_arn
        Optional IAM role to assume before opening the catalog. Uses STS
        to fetch temporary credentials; they are passed explicitly to
        pyiceberg via ``s3.access-key-id`` / ``s3.secret-access-key`` /
        ``s3.session-token`` so the pyiceberg-issued requests authenticate
        as the assumed role.
    **kwargs
        Additional kwargs forwarded to
        :class:`IcebergBitemporalBackend` (e.g. ``catalog_name``).
    """

    def __init__(
        self,
        database: str,
        table: str,
        *,
        s3_warehouse: str,
        region: Optional[str] = None,
        role_arn: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        self.database = database
        self.region = region
        self.role_arn = role_arn
        self._s3_warehouse = s3_warehouse

        catalog_kwargs: dict = dict(kwargs.pop("catalog_kwargs", {}))
        if region is not None:
            catalog_kwargs.setdefault("glue.region", region)
            catalog_kwargs.setdefault("s3.region", region)

        session = self._build_session(region=region, role_arn=role_arn)
        self._boto_session = session

        if role_arn is not None:
            creds = session.get_credentials()
            if creds is None:  # pragma: no cover - sanity
                raise RuntimeError(
                    "Unable to resolve credentials from assumed-role session"
                )
            frozen = creds.get_frozen_credentials()
            catalog_kwargs.setdefault("s3.access-key-id", frozen.access_key)
            catalog_kwargs.setdefault("s3.secret-access-key", frozen.secret_key)
            if frozen.token:
                catalog_kwargs.setdefault("s3.session-token", frozen.token)

        # Existence check. Tables auto-create via the parent, but Glue
        # databases should be created out-of-band by a platform team.
        self._assert_database_exists()

        super().__init__(
            namespace=database,
            table=table,
            warehouse=s3_warehouse,
            catalog_type="glue",
            **catalog_kwargs,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # AWS auth plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def _build_session(*, region: Optional[str], role_arn: Optional[str]) -> Any:
        boto3 = _require_boto3()

        if role_arn is None:
            return boto3.Session(region_name=region)

        sts = boto3.client("sts", region_name=region)
        resp = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="briefcase-glue",
        )
        creds = resp["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )

    def _assert_database_exists(self) -> None:
        glue = self._boto_session.client("glue", region_name=self.region)
        try:
            glue.get_database(Name=self.database)
        except glue.exceptions.EntityNotFoundException as exc:
            raise ValueError(
                f"Glue database {self.database!r} does not exist; create it first"
            ) from exc


__all__ = ["GlueIcebergBackend"]
