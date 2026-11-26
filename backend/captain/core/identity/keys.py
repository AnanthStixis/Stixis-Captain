"""Captain virtual key format and hashing.

Format: ``cpt_<env>_<keyid>_<secret>_<crc>``
- prefix lets secret scanners (gitleaks, GitHub) detect leaked keys
- keyid (16 hex) gives an O(1) lookup without scanning hashes
- crc (CRC32 of the rest) rejects typos before any database lookup
Only HMAC-SHA-256(pepper, secret) is stored. The raw key is shown once at issue time.
"""

from __future__ import annotations

import hmac
import re
import secrets
import zlib
from dataclasses import dataclass
from hashlib import sha256

_KEY_RE = re.compile(r"^cpt_(live|test)_([0-9a-f]{16})_([0-9a-f]{48})_([0-9a-f]{8})$")


class MalformedKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedKey:
    env: str
    key_id: str
    secret: str


def _crc(body: str) -> str:
    return f"{zlib.crc32(body.encode()) & 0xFFFFFFFF:08x}"


def generate_key(env: str = "live") -> tuple[str, ParsedKey]:
    """Return (raw_key, parsed). Show raw_key to the user once; never store it."""
    if env not in ("live", "test"):
        raise ValueError("env must be live or test")
    key_id = secrets.token_hex(8)
    secret = secrets.token_hex(24)
    body = f"cpt_{env}_{key_id}_{secret}"
    return f"{body}_{_crc(body)}", ParsedKey(env, key_id, secret)


def parse_key(raw: str) -> ParsedKey:
    m = _KEY_RE.match(raw)
    if not m:
        raise MalformedKeyError("not a Captain key")
    env, key_id, secret, crc = m.groups()
    if not hmac.compare_digest(crc, _crc(raw.rsplit("_", 1)[0])):
        raise MalformedKeyError("checksum mismatch")
    return ParsedKey(env, key_id, secret)


def hash_secret(secret: str, pepper: bytes) -> str:
    if len(pepper) < 32:
        raise ValueError("pepper must be at least 32 bytes")
    return hmac.new(pepper, secret.encode(), sha256).hexdigest()


def verify_secret(secret: str, stored_hash: str, pepper: bytes) -> bool:
    return hmac.compare_digest(hash_secret(secret, pepper), stored_hash)
