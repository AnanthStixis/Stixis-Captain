"""Company AI keys (provider API keys), entered on screen and stored encrypted (plan step 3).

Envelope encryption: every key gets its own random 256-bit data key; the AI key is
encrypted with it (AES-256-GCM, the provider id bound in as associated data, so a
ciphertext cannot be moved to another provider); the data key is then WRAPPED by a master
key that never sits in the database:
- production on Azure: an RSA key in Key Vault (`CAPTAIN_SECRETS_KEY=azurekv:<key URL>`),
  wrapped and unwrapped by Key Vault itself as the pod's workload identity;
- development: a local 256-bit key (`CAPTAIN_SECRETS_KEY=hex:<64 hex chars>`).
A database dump alone therefore holds no usable key. Deleting a row destroys the key.

What is ever shown or recorded: the provider, the last 4 characters, who stored it and
when. The key itself is only ever decrypted inside the gateway, at call time.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REF_PREFIX = "captain:"


class SecretsUnavailableError(Exception):
    """The master key could not wrap/unwrap (Key Vault down, wrong key): fail closed."""


class KeyWrapper(Protocol):
    key_id: str

    def wrap(self, dek: bytes) -> bytes: ...
    def unwrap(self, wrapped: bytes) -> bytes: ...


class LocalKeyWrapper:
    """Development only: the master key comes from the environment."""

    def __init__(self, master: bytes, key_id: str = "local-1") -> None:
        if len(master) != 32:
            raise ValueError("the local secrets key must be 32 bytes (64 hex characters)")
        self._aes = AESGCM(master)
        self.key_id = key_id

    def wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, dek, b"captain-dek")

    def unwrap(self, wrapped: bytes) -> bytes:
        try:
            return self._aes.decrypt(wrapped[:12], wrapped[12:], b"captain-dek")
        except Exception as e:  # InvalidTag: wrong master key
            raise SecretsUnavailableError("the stored key cannot be unwrapped") from e


class AzureKeyVaultWrapper:
    """RSA key in Key Vault (wrapKey / unwrapKey, RSA-OAEP-256). The private half never
    leaves Key Vault. `crypto_client` is an azure-keyvault-keys CryptographyClient."""

    ALG = "RSA-OAEP-256"

    def __init__(self, crypto_client: Any, key_id: str) -> None:
        self._c = crypto_client
        self.key_id = key_id

    @staticmethod
    def from_key_url(key_url: str) -> AzureKeyVaultWrapper:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.keys.crypto import CryptographyClient

        from captain.core.ledger.signers import parse_key_url

        _, name, version = parse_key_url(key_url)
        return AzureKeyVaultWrapper(
            CryptographyClient(key_url, DefaultAzureCredential()), f"azurekv-{name}-{version}"
        )

    def wrap(self, dek: bytes) -> bytes:
        try:
            return bytes(self._c.wrap_key(self.ALG, dek).encrypted_key)
        except Exception as e:
            raise SecretsUnavailableError(f"Key Vault wrap failed: {type(e).__name__}") from e

    def unwrap(self, wrapped: bytes) -> bytes:
        try:
            return bytes(self._c.unwrap_key(self.ALG, wrapped).key)
        except Exception as e:
            raise SecretsUnavailableError(f"Key Vault unwrap failed: {type(e).__name__}") from e


def build_wrapper(spec: str) -> KeyWrapper:
    """`hex:<64 hex>` (development) or `azurekv:<Key Vault RSA key URL>`."""
    kind, _, rest = spec.partition(":")
    if kind == "hex":
        return LocalKeyWrapper(bytes.fromhex(rest))
    if kind == "azurekv":
        return AzureKeyVaultWrapper.from_key_url(rest)
    raise ValueError("CAPTAIN_SECRETS_KEY must be hex:<64 hex> or azurekv:<key URL>")


@dataclass(frozen=True, slots=True)
class StoredSecret:
    id: str
    provider_id: str
    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    wrap_key_id: str
    last4: str
    created_by: str
    created_at: datetime


class SecretStore(Protocol):
    def put(self, s: StoredSecret) -> None: ...
    def get(self, secret_id: str) -> StoredSecret | None: ...
    def all(self) -> list[StoredSecret]: ...
    def delete(self, secret_id: str) -> bool: ...


class InMemorySecretStore:
    def __init__(self) -> None:
        self._d: dict[str, StoredSecret] = {}

    def put(self, s: StoredSecret) -> None:
        self._d[s.id] = s

    def get(self, secret_id: str) -> StoredSecret | None:
        return self._d.get(secret_id)

    def all(self) -> list[StoredSecret]:
        return sorted(self._d.values(), key=lambda s: s.created_at)

    def delete(self, secret_id: str) -> bool:
        return self._d.pop(secret_id, None) is not None


def secret_view(s: StoredSecret) -> dict[str, Any]:
    """What the dashboard may show: never the key, never ciphertext."""
    return {
        "id": s.id,
        "ref": REF_PREFIX + s.id,
        "provider_id": s.provider_id,
        "last4": s.last4,
        "created_by": s.created_by,
        "created_at": s.created_at,
        "master_key": s.wrap_key_id,
    }


class SecretsService:
    def __init__(self, store: SecretStore, wrapper: KeyWrapper, cache_s: float = 300) -> None:
        self._store = store
        self._wrap = wrapper
        self._cache: dict[str, tuple[float, str]] = {}
        self._cache_s = cache_s
        self._lock = threading.Lock()

    @property
    def store(self) -> SecretStore:
        return self._store

    def put(self, provider_id: str, plaintext: str, created_by: str) -> StoredSecret:
        value = plaintext.strip()
        if len(value) < 8:
            raise ValueError("that does not look like an API key (too short)")
        dek = AESGCM.generate_key(256)
        nonce = os.urandom(12)
        ct = AESGCM(dek).encrypt(nonce, value.encode(), provider_id.encode())
        s = StoredSecret(
            id=uuid4().hex,
            provider_id=provider_id,
            ciphertext=ct,
            nonce=nonce,
            wrapped_dek=self._wrap.wrap(dek),
            wrap_key_id=self._wrap.key_id,
            last4=value[-4:],
            created_by=created_by,
            created_at=datetime.now(UTC),
        )
        self._store.put(s)
        return s

    def reveal(self, secret_id: str) -> str:
        """Decrypt for a provider call (gateway only). Cached briefly in memory so Key
        Vault is not called on every request; a deleted key stops working within
        `cache_s` seconds on every gateway, immediately on this one."""
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(secret_id)
            if hit and hit[0] > now:
                return hit[1]
        s = self._store.get(secret_id)
        if s is None:
            raise KeyError(secret_id)
        dek = self._wrap.unwrap(s.wrapped_dek)
        try:
            value = AESGCM(dek).decrypt(s.nonce, s.ciphertext, s.provider_id.encode()).decode()
        except Exception as e:
            raise SecretsUnavailableError("the stored key failed its integrity check") from e
        with self._lock:
            self._cache[secret_id] = (now + self._cache_s, value)
        return value

    def delete(self, secret_id: str) -> bool:
        with self._lock:
            self._cache.pop(secret_id, None)
        return self._store.delete(secret_id)
