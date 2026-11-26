"""Evidence ledger primitives: canonical hashing, signing, entries, Merkle anchors.

What this gives (and does not give) — stated honestly:
- Hash chain: altering, deleting or reordering any entry breaks every later link.
- Per-entry signature: someone with database access but without the signing key
  cannot forge a valid replacement chain. (Local Ed25519 key in dev; KMS-held,
  non-exportable key in production.)
- Hourly Merkle anchor in WORM storage: once anchored, even the signing-key holder
  cannot rewrite history without the anchor check failing.
- NOT given: protection for events that were never written (that is the outbox's
  job, and Tier 1–2 refuse calls when evidence can't be queued), and protection for
  the last < 1 hour against a holder of the signing key.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA, EllipticCurvePublicKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.hashes import SHA256

GENESIS = "0" * 64


def canonical(obj: Any) -> bytes:
    """RFC 8785 JSON Canonicalization Scheme."""
    return rfc8785.dumps(obj)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Signer(Protocol):
    key_id: str

    def sign(self, message: bytes) -> bytes: ...


class Verifier(Protocol):
    def verify(self, key_id: str, message: bytes, signature: bytes) -> bool: ...


class Ed25519Signer:
    """Local development signer. Production uses a KMS asymmetric key (same protocol)."""

    def __init__(self, key_id: str, private_key: Ed25519PrivateKey | None = None) -> None:
        self.key_id = key_id
        self._k = private_key or Ed25519PrivateKey.generate()

    def sign(self, message: bytes) -> bytes:
        return self._k.sign(message)

    def public_key(self) -> Ed25519PublicKey:
        return self._k.public_key()


PublicKey = Ed25519PublicKey | EllipticCurvePublicKey


class KeyRingVerifier:
    """Verifies Ed25519 (local) and ECDSA P-256 / SHA-256 DER signatures (cloud KMS).
    Keep retired keys in the ring: old entries stay verifiable after rotation."""

    def __init__(self, keys: dict[str, PublicKey]) -> None:
        self._keys = keys

    def verify(self, key_id: str, message: bytes, signature: bytes) -> bool:
        k = self._keys.get(key_id)
        if k is None:
            return False
        try:
            if isinstance(k, EllipticCurvePublicKey):
                k.verify(signature, message, ECDSA(SHA256()))
            else:
                k.verify(signature, message)
            return True
        except InvalidSignature:
            return False


def load_public_key_pem(pem: bytes) -> PublicKey:
    k = serialization.load_pem_public_key(pem)
    if not isinstance(k, Ed25519PublicKey | EllipticCurvePublicKey):
        raise ValueError("ledger keys must be Ed25519 or EC P-256")
    return k


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    partition: str
    seq: int
    event_id: str
    event_type: str
    recorded_at: datetime
    body: dict[str, Any]  # the event, JSON-safe, never raw content
    prev_hash: str
    entry_hash: str
    signature: str  # base64
    key_id: str
    anchor_id: str | None = None

    def hashed_part(self) -> dict[str, Any]:
        return {
            "partition": self.partition,
            "seq": self.seq,
            "event_id": self.event_id,
            "event_type": self.event_type,
            # Always hash UTC with fixed microsecond precision: databases return the same
            # instant in their session timezone, which must not change the hash.
            "recorded_at": self.recorded_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "body": self.body,
        }


def compute_entry_hash(prev_hash: str, hashed_part: dict[str, Any]) -> str:
    return sha256_hex(prev_hash.encode() + canonical(hashed_part))


def seal(
    partition: str,
    seq: int,
    event_id: str,
    event_type: str,
    recorded_at: datetime,
    body: dict[str, Any],
    prev_hash: str,
    signer: Signer,
) -> LedgerEntry:
    draft = LedgerEntry(
        partition, seq, event_id, event_type, recorded_at, body, prev_hash, "", "", ""
    )
    h = compute_entry_hash(prev_hash, draft.hashed_part())
    sig = base64.b64encode(signer.sign(bytes.fromhex(h))).decode()
    return LedgerEntry(
        partition, seq, event_id, event_type, recorded_at, body, prev_hash, h, sig, signer.key_id
    )


# --------------------------------------------------------------------- Merkle


def merkle_root(leaves: list[str]) -> str:
    """Binary Merkle root over hex leaf hashes (odd node is promoted). Leaf and node
    hashes are domain-separated to prevent second-preimage tricks."""
    if not leaves:
        return sha256_hex(b"\x00empty")
    level = [sha256_hex(b"\x00" + bytes.fromhex(x)) for x in leaves]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(
                    sha256_hex(b"\x01" + bytes.fromhex(level[i]) + bytes.fromhex(level[i + 1]))
                )
            else:
                nxt.append(level[i])
        level = nxt
    return level[0]


@dataclass(frozen=True, slots=True)
class Anchor:
    anchor_id: str
    created_at: datetime
    ranges: dict[str, tuple[int, int]]  # partition -> (first_seq, last_seq) inclusive
    root: str
    signature: str
    key_id: str

    def document(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "created_at": self.created_at.isoformat(),
            "ranges": {p: list(r) for p, r in sorted(self.ranges.items())},
            "root": self.root,
        }
