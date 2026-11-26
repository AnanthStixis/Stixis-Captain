"""Cloud WORM anchor stores with stand-in clients that follow the real API shapes:
S3 put_object with Object Lock + IfNoneMatch, paged list_objects_v2; Azure
upload_blob(overwrite=False), list_blobs, download_blob(...).readall()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from captain.config import ConfigError, build_worm
from captain.core.ledger.chain import Ed25519Signer, KeyRingVerifier
from captain.core.ledger.service import DirectoryWormStore, InMemoryLedgerStore, LedgerService
from captain.core.ledger.worm_cloud import (
    AzureImmutableBlobWormStore,
    S3ObjectLockWormStore,
    WormConfigurationError,
)
from captain.core.models import Channel, CoverageClass, EventType, UsageEvent


class PreconditionFailed(Exception):
    pass


class FakeS3:
    def __init__(self, lock_enabled: bool = True, page: int = 2) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.lock_enabled, self.page = lock_enabled, page

    def get_object_lock_configuration(self, Bucket: str) -> dict[str, Any]:  # noqa: N803
        if not self.lock_enabled:
            raise RuntimeError("ObjectLockConfigurationNotFoundError")
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}

    def put_object(self, **kw: Any) -> dict[str, Any]:
        assert kw["ObjectLockMode"] == "COMPLIANCE" and kw["IfNoneMatch"] == "*"
        assert kw["ObjectLockRetainUntilDate"] > datetime.now(UTC) + timedelta(days=182)
        if kw["Key"] in self.objects:
            raise PreconditionFailed("412 PreconditionFailed")
        self.objects[kw["Key"]] = kw
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": SimpleNamespace(read=lambda: self.objects[Key]["Body"])}

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        keys = sorted(k for k in self.objects if k.startswith(kw["Prefix"]))
        start = int(kw.get("ContinuationToken", 0))
        chunk = keys[start : start + self.page]
        more = start + self.page < len(keys)
        return {
            "Contents": [{"Key": k} for k in chunk],
            "IsTruncated": more,
            "NextContinuationToken": str(start + self.page) if more else None,
        }


class FakeContainer:
    def __init__(self, immutable: bool = True) -> None:
        self.blobs: dict[str, bytes] = {}
        self.immutable = immutable

    def get_container_properties(self) -> Any:  # the SDK returns an object with attributes
        return SimpleNamespace(
            has_immutability_policy=self.immutable, immutable_storage_with_versioning_enabled=False
        )

    def upload_blob(self, name: str, data: bytes, overwrite: bool, **kw: Any) -> None:
        if name in self.blobs and not overwrite:
            raise FileExistsError("BlobAlreadyExists")
        self.blobs[name] = data

    def download_blob(self, name: str) -> Any:
        return SimpleNamespace(readall=lambda: self.blobs[name])

    def list_blobs(self, name_starts_with: str) -> list[Any]:
        return [SimpleNamespace(name=n) for n in self.blobs if n.startswith(name_starts_with)]


@pytest.mark.parametrize(
    "make",
    [
        lambda: S3ObjectLockWormStore(FakeS3(), "anchors-bucket"),
        lambda: AzureImmutableBlobWormStore(FakeContainer()),
    ],
    ids=["s3", "azure"],
)
def test_write_once_and_listing(make: Any) -> None:
    w = make()
    for i in range(5):  # > one S3 page
        w.put_once(f"a{i}.json", f"{i}".encode())
    with pytest.raises((PreconditionFailed, FileExistsError)):
        w.put_once("a1.json", b"tampered")
    assert w.names() == [f"a{i}.json" for i in range(5)]
    assert w.get("a3.json") == b"3"


@pytest.mark.parametrize("kind", ["s3", "azure"])
def test_ledger_anchors_and_verifies_on_cloud_worm(kind: str) -> None:
    worm: Any = (
        S3ObjectLockWormStore(FakeS3(), "b")
        if kind == "s3"
        else AzureImmutableBlobWormStore(FakeContainer())
    )
    s = Ed25519Signer("k")
    svc = LedgerService(InMemoryLedgerStore(), worm, s, KeyRingVerifier({"k": s.public_key()}))
    svc.enqueue(
        UsageEvent(
            trace_id="t",
            event_type=EventType.CALL_ALLOWED,
            channel=Channel.API,
            coverage=CoverageClass.GOVERNED,
            principal_id="u",
            register_id="AI-1",
        )
    )
    svc.write_pending()
    assert svc.anchor() is not None and len(worm.names()) == 1
    rep = svc.verify()
    assert rep.ok and rep.anchors_checked == 1


def test_s3_refuses_bucket_without_object_lock() -> None:
    with pytest.raises(WormConfigurationError):
        S3ObjectLockWormStore(FakeS3(lock_enabled=False), "plain-bucket").verify_configuration()


def test_retention_minimum() -> None:
    with pytest.raises(ValueError):
        S3ObjectLockWormStore(FakeS3(), "b", retention_days=30)


def test_build_worm_from_settings(tmp_path: Any) -> None:
    assert isinstance(build_worm(f"dir:{tmp_path / 'a'}", 400), DirectoryWormStore)
    with pytest.raises(ConfigError):
        build_worm("ftp:somewhere", 400)


def test_azure_container_without_immutability_is_refused() -> None:
    from captain.core.ledger.worm_cloud import WormConfigurationError

    AzureImmutableBlobWormStore(FakeContainer()).verify_configuration()  # write-once: fine
    with pytest.raises(WormConfigurationError, match="immutability"):
        AzureImmutableBlobWormStore(FakeContainer(immutable=False)).verify_configuration()
