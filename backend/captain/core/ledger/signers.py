"""Ledger signers.

| Signer               | Key                                   | Use                    |
|----------------------|---------------------------------------|------------------------|
| PemFileSigner        | Ed25519 / EC P-256 PEM on local disk  | development, pilot dev |
| AwsKmsSigner         | AWS KMS ECC_NIST_P256, SIGN_VERIFY    | production on AWS      |
| AzureKeyVaultSigner  | Azure Key Vault EC P-256              | production on Azure    |

Cloud signers never see the private key: Captain sends a SHA-256 digest and gets a
signature back, so a database administrator cannot forge ledger entries (CTO F4).
All produce signatures the KeyRingVerifier checks with the public key alone.

The cloud SDK clients are injected (boto3 `kms` client / azure-keyvault-keys
`CryptographyClient`), so Captain does not hard-depend on either SDK; which one is
used follows decision D3 (AWS or Azure).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import (
    ECDSA,
    SECP256R1,
    EllipticCurvePrivateKey,
    generate_private_key,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.hashes import SHA256

from captain.core.ledger.chain import PublicKey


class PemFileSigner:
    """Loads a private key from a PEM file so signatures survive restarts.
    Development only: the key sits on disk. Use a KMS signer in production."""

    def __init__(self, key_id: str, path: Path) -> None:
        self.key_id = key_id
        k = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(k, Ed25519PrivateKey | EllipticCurvePrivateKey):
            raise ValueError("ledger signing key must be Ed25519 or EC P-256")
        self._k = k

    @staticmethod
    def generate(path: Path, kind: str = "ed25519") -> None:
        if path.exists():
            raise FileExistsError(f"{path} exists; refusing to overwrite a signing key")
        k: Ed25519PrivateKey | EllipticCurvePrivateKey = (
            Ed25519PrivateKey.generate() if kind == "ed25519" else generate_private_key(SECP256R1())
        )
        pem = k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)

    def sign(self, message: bytes) -> bytes:
        if isinstance(self._k, EllipticCurvePrivateKey):
            return self._k.sign(message, ECDSA(SHA256()))
        return self._k.sign(message)

    def public_key(self) -> PublicKey:
        return self._k.public_key()


class AwsKmsSigner:
    """AWS KMS asymmetric key (KeySpec ECC_NIST_P256, KeyUsage SIGN_VERIFY).
    Uses MessageType=DIGEST so messages of any size (e.g. anchor documents) can be
    signed; KMS returns a DER-encoded ECDSA signature."""

    def __init__(self, kms_client: Any, kms_key_id: str, key_id: str | None = None) -> None:
        self._kms = kms_client
        self._kms_key = kms_key_id
        self.key_id = key_id or kms_key_id

    def sign(self, message: bytes) -> bytes:
        resp = self._kms.sign(
            KeyId=self._kms_key,
            Message=hashlib.sha256(message).digest(),
            MessageType="DIGEST",
            SigningAlgorithm="ECDSA_SHA_256",
        )
        return bytes(resp["Signature"])


class AzureKeyVaultSigner:
    """Azure Key Vault EC P-256 key via azure-keyvault-keys CryptographyClient.
    Key Vault returns a raw r||s signature (64 bytes); it is converted to DER so the
    same verifier works for every signer."""

    def __init__(self, crypto_client: Any, key_id: str) -> None:
        self._c = crypto_client
        self.key_id = key_id

    @staticmethod
    def from_key_url(key_url: str) -> tuple[AzureKeyVaultSigner, PublicKey]:
        """`https://<vault>.vault.azure.net/keys/<name>/<version>` -> a signer and its public
        key (for the verifier). Signs as the pod's managed identity (workload identity);
        needs the `azure` extra. The ledger key id is `azurekv-<name>-<version>` (usable as
        a file name), so a rotated key version gets its own id; keep each retired version's
        public key as `<key id>.pem` in CAPTAIN_LEDGER_PUBLIC_KEYS_DIR."""
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.keys import KeyClient
        from azure.keyvault.keys.crypto import CryptographyClient

        vault, name, version = parse_key_url(key_url)
        cred = DefaultAzureCredential()
        jwk = KeyClient(vault, cred).get_key(name, version).key
        signer = AzureKeyVaultSigner(CryptographyClient(key_url, cred), f"azurekv-{name}-{version}")
        return signer, ec_public_key_from_jwk(str(jwk.crv), bytes(jwk.x), bytes(jwk.y))

    def sign(self, message: bytes) -> bytes:
        result = self._c.sign("ES256", hashlib.sha256(message).digest())
        raw = bytes(result.signature)
        if len(raw) != 64:
            raise ValueError("unexpected ES256 signature length from Key Vault")
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        return encode_dss_signature(r, s)


def parse_key_url(key_url: str) -> tuple[str, str, str]:
    """`https://v.vault.azure.net/keys/<name>/<version>` -> (vault URL, name, version).
    The version is required: an unpinned key would change under the verifier."""
    from urllib.parse import urlparse

    u = urlparse(key_url)
    parts = [x for x in u.path.split("/") if x]
    if u.scheme != "https" or len(parts) != 3 or parts[0] != "keys":
        raise ValueError(
            "Key Vault key URL must be https://<vault>.vault.azure.net/keys/<name>/<version>"
        )
    return f"https://{u.netloc}", parts[1], parts[2]


def ec_public_key_from_jwk(crv: str, x: bytes, y: bytes) -> PublicKey:
    """A Key Vault JWK (EC P-256) as the public key the verifier needs."""
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicNumbers

    if crv not in ("P-256", "KeyCurveName.p_256") or len(x) != 32 or len(y) != 32:
        raise ValueError("the ledger key must be an EC P-256 key")
    return EllipticCurvePublicNumbers(
        int.from_bytes(x, "big"), int.from_bytes(y, "big"), SECP256R1()
    ).public_key()
