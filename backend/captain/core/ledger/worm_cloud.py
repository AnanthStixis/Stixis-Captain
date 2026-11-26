"""Write-once (WORM) storage for ledger anchors in the client's cloud (CTO F4).

Both stores implement the WormStore protocol (put_once / get / names). The SDK clients
are injected, so Captain does not hard-depend on boto3 or the Azure SDK; which one is
used follows decision D3 (AWS or Azure).

AWS — S3 Object Lock, COMPLIANCE mode
  - Bucket must be created with Object Lock enabled (OpenTofu). `verify_configuration()`
    refuses to start if it is not.
  - Every anchor is written with ObjectLockMode=COMPLIANCE and a retain-until date:
    during retention nobody, including the root account, can delete or overwrite it.
  - `IfNoneMatch="*"` makes the write conditional, so an existing anchor name can never
    be replaced by a new version.

Azure — immutable blob storage
  - The container must have a LOCKED time-based retention policy (set by OpenTofu;
    it cannot be shortened or removed once locked). That is what makes blobs WORM.
  - Captain writes with overwrite=False so an existing anchor is never replaced.
  - The data-plane SDK cannot read container policies, so the deployment check for the
    locked policy belongs to the infrastructure pipeline; `verify_configuration()` here
    only confirms the container is reachable.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any


class WormConfigurationError(RuntimeError):
    pass


class S3ObjectLockWormStore:
    def __init__(
        self,
        s3_client: Any,
        bucket: str,
        prefix: str = "captain/anchors/",
        retention_days: int = 400,
    ) -> None:
        if retention_days < 183:  # EU AI Act Art. 26 is read as ≥ 6 months of logs
            raise ValueError("anchor retention must be at least 183 days")
        self._s3 = s3_client
        self._bucket = bucket
        self._prefix = prefix
        self._days = retention_days

    def verify_configuration(self) -> None:
        try:
            cfg = self._s3.get_object_lock_configuration(Bucket=self._bucket)
        except Exception as e:  # botocore ClientError: no Object Lock on this bucket
            raise WormConfigurationError(
                f"bucket {self._bucket}: Object Lock not configured ({e})"
            ) from e
        if cfg.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") != "Enabled":
            raise WormConfigurationError(f"bucket {self._bucket}: Object Lock is not enabled")

    def put_once(self, name: str, data: bytes) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._prefix + name,
            Body=data,
            ContentType="application/json",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(days=self._days),
            ChecksumAlgorithm="SHA256",
            IfNoneMatch="*",
        )

    def get(self, name: str) -> bytes:
        body: bytes = self._s3.get_object(Bucket=self._bucket, Key=self._prefix + name)[
            "Body"
        ].read()
        return body

    def names(self) -> list[str]:
        out: list[str] = []
        token: str | None = None
        while True:
            kw: dict[str, Any] = {"Bucket": self._bucket, "Prefix": self._prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kw)
            out += [o["Key"][len(self._prefix) :] for o in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                return sorted(out)
            token = resp.get("NextContinuationToken")


class AzureImmutableBlobWormStore:
    def __init__(self, container_client: Any, prefix: str = "captain/anchors/") -> None:
        self._c = container_client
        self._prefix = prefix

    def verify_configuration(self) -> None:
        """Refuses to run unless the container is write-once: a time-based immutability
        policy on the container, or version-level immutability (WORM) enabled on it."""
        try:
            props = self._c.get_container_properties()
        except Exception as e:
            raise WormConfigurationError(f"anchor container unreachable ({e})") from e

        def flag(name: str) -> bool:
            v = props.get(name) if isinstance(props, dict) else getattr(props, name, None)
            return bool(v)

        if not (
            flag("has_immutability_policy") or flag("immutable_storage_with_versioning_enabled")
        ):
            raise WormConfigurationError(
                "anchor container has no immutability policy: anchors could be overwritten "
                "or deleted (set a time-based retention policy on the container)"
            )

    def put_once(self, name: str, data: bytes) -> None:
        self._c.upload_blob(
            name=self._prefix + name,
            data=data,
            overwrite=False,  # never replace an existing anchor
            content_settings=None,
            metadata={"sha256": hashlib.sha256(data).hexdigest()},
        )

    def get(self, name: str) -> bytes:
        data: bytes = self._c.download_blob(self._prefix + name).readall()
        return data

    def names(self) -> list[str]:
        return sorted(
            b.name[len(self._prefix) :] for b in self._c.list_blobs(name_starts_with=self._prefix)
        )
