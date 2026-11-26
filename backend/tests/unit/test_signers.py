"""Ledger signers. Cloud KMS clients are faked with local EC keys that follow the real
APIs' input/output shapes (digest in; DER from AWS, raw r||s from Azure)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA, SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature
from cryptography.hazmat.primitives.hashes import SHA256

from captain.core.ledger.chain import KeyRingVerifier, load_public_key_pem
from captain.core.ledger.service import InMemoryLedgerStore, InMemoryWormStore, LedgerService
from captain.core.ledger.signers import AwsKmsSigner, AzureKeyVaultSigner, PemFileSigner
from captain.core.models import Channel, CoverageClass, EventType, UsageEvent

EC = generate_private_key(SECP256R1())


class FakeKms:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def sign(self, **kw: Any) -> dict[str, bytes]:
        self.calls.append(kw)
        assert kw["MessageType"] == "DIGEST" and len(kw["Message"]) == 32
        return {"Signature": EC.sign(kw["Message"], ECDSA(Prehashed(SHA256())))}


class FakeAzure:
    def sign(self, alg: str, digest: bytes) -> SimpleNamespace:
        assert alg == "ES256" and len(digest) == 32
        r, s = decode_dss_signature(EC.sign(digest, ECDSA(Prehashed(SHA256()))))
        return SimpleNamespace(signature=r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def ring(key_id: str) -> KeyRingVerifier:
    return KeyRingVerifier({key_id: EC.public_key()})


@pytest.mark.parametrize(
    "signer",
    [AwsKmsSigner(FakeKms(), "arn:aws:kms:x", "kms-1"), AzureKeyVaultSigner(FakeAzure(), "kms-1")],
)
def test_cloud_signatures_verify(signer: Any) -> None:
    msg = b"x" * 10_000  # larger than KMS RAW limit: digest mode handles it
    sig = signer.sign(msg)
    assert ring("kms-1").verify("kms-1", msg, sig)
    assert not ring("kms-1").verify("kms-1", msg + b"!", sig)


@pytest.mark.parametrize("kind", ["ed25519", "ec"])
def test_pem_signer_persists_across_restarts(tmp_path: Path, kind: str) -> None:
    p = tmp_path / "keys" / "ledger.pem"
    PemFileSigner.generate(p, kind)
    with pytest.raises(FileExistsError):
        PemFileSigner.generate(p, kind)
    a, b = PemFileSigner("dev", p), PemFileSigner("dev", p)  # two "restarts"
    v = KeyRingVerifier({"dev": a.public_key()})
    assert v.verify("dev", b"m", b.sign(b"m"))


def test_ledger_with_kms_signer_and_rotation() -> None:
    kms = AwsKmsSigner(FakeKms(), "arn", "kms-2026")
    from captain.core.ledger.chain import Ed25519Signer

    legacy = Ed25519Signer("dev-2025")
    verifier = KeyRingVerifier({"dev-2025": legacy.public_key(), "kms-2026": EC.public_key()})
    store, worm = InMemoryLedgerStore(), InMemoryWormStore()
    ev = UsageEvent(
        trace_id="t",
        event_type=EventType.CALL_ALLOWED,
        channel=Channel.API,
        coverage=CoverageClass.GOVERNED,
        principal_id="u",
        register_id="AI-1",
    )
    LedgerService(store, worm, legacy, verifier).enqueue(ev)
    LedgerService(store, worm, legacy, verifier).write_pending()
    svc = LedgerService(store, worm, kms, verifier)  # key rotated to KMS
    svc.enqueue(ev.model_copy(update={"trace_id": "t2", "event_id": uuid4()}))
    svc.write_pending()
    assert [e.key_id for e in store.rows] == ["dev-2025", "kms-2026"]
    assert svc.anchor() is not None and svc.verify().ok


def test_public_key_pem_loader_rejects_rsa() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = (
        rsa.generate_private_key(65537, 2048)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    with pytest.raises(ValueError):
        load_public_key_pem(pem)


# ------------------------------------------------------- Azure Key Vault wiring (E2, Azure)


def test_key_vault_url_must_be_pinned_to_a_version() -> None:
    from captain.core.ledger.signers import parse_key_url

    url = "https://captain-kv.vault.azure.net/keys/ledger/0123abcd"
    assert parse_key_url(url) == ("https://captain-kv.vault.azure.net", "ledger", "0123abcd")
    for bad in (
        "https://captain-kv.vault.azure.net/keys/ledger",  # no version: could change silently
        "http://captain-kv.vault.azure.net/keys/ledger/1",
        "https://captain-kv.vault.azure.net/secrets/ledger/1",
    ):
        with pytest.raises(ValueError):
            parse_key_url(bad)


def test_public_key_from_key_vault_jwk_verifies_key_vault_signatures() -> None:
    """The verifier's key is rebuilt from the JWK Key Vault returns (x, y on P-256) and
    accepts what the Key Vault signer produced."""
    from captain.core.ledger.signers import ec_public_key_from_jwk

    n = EC.public_key().public_numbers()
    pub = ec_public_key_from_jwk("P-256", n.x.to_bytes(32, "big"), n.y.to_bytes(32, "big"))
    signer = AzureKeyVaultSigner(FakeAzure(), "azurekv-ledger-0123abcd")
    sig = signer.sign(b"entry")
    assert KeyRingVerifier({signer.key_id: pub}).verify(signer.key_id, b"entry", sig)
    with pytest.raises(ValueError):
        ec_public_key_from_jwk("P-384", b"\0" * 48, b"\0" * 48)


def test_config_accepts_a_key_vault_ledger_key_and_rejects_others() -> None:
    from captain.config import ConfigError, load_settings

    base = {
        "CAPTAIN_DATABASE_URL": "postgresql+psycopg://x",
        "CAPTAIN_KEY_PEPPER": "00" * 32,
        "CAPTAIN_WORM": "azure:https://a.blob.core.windows.net|anchors",
    }
    url = "azurekv:https://captain-kv.vault.azure.net/keys/ledger/0123abcd"
    assert load_settings({**base, "CAPTAIN_LEDGER_KEY": url}).ledger_key_spec == url
    for bad in ("kms:arn:aws:kms:x", "azurekv:https://captain-kv.vault.azure.net/keys/ledger"):
        with pytest.raises(ConfigError):
            load_settings({**base, "CAPTAIN_LEDGER_KEY": bad})
