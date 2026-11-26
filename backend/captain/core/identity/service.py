"""Identity service: key lifecycle and authentication."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from captain.core.bus import EventBus, Topic
from captain.core.decision_api import AuthenticationError
from captain.core.identity.keys import (
    MalformedKeyError,
    generate_key,
    hash_secret,
    parse_key,
    verify_secret,
)
from captain.core.identity.model import KeyRecord, KeyStatus, PrincipalRecord
from captain.core.models import Environment, ReasonCode

_TOUCH_INTERVAL = timedelta(seconds=60)


class IdentityStore(Protocol):
    def get_key(self, key_id: str) -> KeyRecord | None: ...
    def put_key(self, record: KeyRecord) -> None: ...
    def keys_for_principal(self, principal_id: str) -> list[KeyRecord]: ...
    def get_principal(self, principal_id: str) -> PrincipalRecord | None: ...
    def put_principal(self, record: PrincipalRecord) -> None: ...
    def touch_key(self, key_id: str, at: datetime) -> None: ...


class InMemoryIdentityStore:
    def __init__(self) -> None:
        self._keys: dict[str, KeyRecord] = {}
        self._principals: dict[str, PrincipalRecord] = {}
        self._lock = threading.Lock()

    def get_key(self, key_id: str) -> KeyRecord | None:
        return self._keys.get(key_id)

    def put_key(self, record: KeyRecord) -> None:
        with self._lock:
            self._keys[record.key_id] = record

    def keys_for_principal(self, principal_id: str) -> list[KeyRecord]:
        return [k for k in self._keys.values() if k.principal_id == principal_id]

    def get_principal(self, principal_id: str) -> PrincipalRecord | None:
        return self._principals.get(principal_id)

    def put_principal(self, record: PrincipalRecord) -> None:
        with self._lock:
            self._principals[record.id] = record

    def touch_key(self, key_id: str, at: datetime) -> None:
        with self._lock:
            if k := self._keys.get(key_id):
                self._keys[key_id] = replace(k, last_used_at=at)


class IssuedKey:
    """Returned once at creation. `raw_key` must never be stored or logged."""

    def __init__(self, raw_key: str, record: KeyRecord) -> None:
        self.raw_key = raw_key
        self.record = record

    def __repr__(self) -> str:
        return f"IssuedKey(key_id={self.record.key_id!r})"


class IdentityService:
    def __init__(
        self,
        store: IdentityStore,
        pepper: bytes,
        bus: EventBus,
        cache_ttl_s: float = 30.0,
        key_env: str = "live",
    ) -> None:
        self._store = store
        self._pepper = pepper
        self._bus = bus
        self._ttl = cache_ttl_s
        self._env = key_env
        self._cache: dict[str, tuple[float, KeyRecord]] = {}
        self._lock = threading.Lock()
        bus.subscribe(Topic.KEY_REVOKED, self._evict)

    # ------------------------------------------------------------- lifecycle
    def issue_key(
        self,
        principal_id: str,
        register_id: str,
        project_id: str,
        application_id: str,
        environment: Environment,
        ttl: timedelta | None,
        label: str = "",
        allowed_cidrs: tuple[str, ...] = (),
        rotated_from: str | None = None,
    ) -> IssuedKey:
        principal = self._store.get_principal(principal_id)
        if principal is None or not principal.active:
            raise ValueError("principal does not exist or is inactive")
        raw, parsed = generate_key(self._env)
        now = datetime.now(UTC)
        record = KeyRecord(
            key_id=parsed.key_id,
            secret_hash=hash_secret(parsed.secret, self._pepper),
            principal_id=principal_id,
            register_id=register_id,
            project_id=project_id,
            application_id=application_id,
            environment=environment,
            created_at=now,
            expires_at=now + ttl if ttl else None,
            label=label,
            allowed_cidrs=allowed_cidrs,
            rotated_from=rotated_from,
        )
        self._store.put_key(record)
        return IssuedKey(raw, record)

    def revoke_key(self, key_id: str, reason: str) -> KeyRecord:
        rec = self._store.get_key(key_id)
        if rec is None:
            raise KeyError(key_id)
        rec = rec.revoked(reason)
        self._store.put_key(rec)
        self._bus.publish(Topic.KEY_REVOKED, key_id)  # every instance evicts its cache
        return rec

    def rotate_key(self, key_id: str, overlap: timedelta = timedelta(0)) -> IssuedKey:
        """Issue a replacement bound to the same context; revoke the old key.
        A non-zero overlap keeps the old key valid until then (expiry instead of revoke)."""
        old = self._store.get_key(key_id)
        if old is None or old.status is not KeyStatus.ACTIVE:
            raise ValueError("only active keys can be rotated")
        lifetime = (old.expires_at - old.created_at) if old.expires_at else None
        new = self.issue_key(
            old.principal_id,
            old.register_id,
            old.project_id,
            old.application_id,
            old.environment,
            lifetime,
            old.label,
            old.allowed_cidrs,
            rotated_from=old.key_id,
        )
        if overlap > timedelta(0):
            self._store.put_key(replace(old, expires_at=datetime.now(UTC) + overlap))
            self._evict(key_id)
        else:
            self.revoke_key(key_id, "rotated")
        return new

    # --------------------------------------------------------- authentication
    def authenticate(self, raw_key: str, client_ip: str | None = None) -> KeyRecord:
        try:
            parsed = parse_key(raw_key)
        except MalformedKeyError as e:
            raise AuthenticationError("malformed key") from e
        rec = self._cached(parsed.key_id)
        if rec is None or not verify_secret(parsed.secret, rec.secret_hash, self._pepper):
            raise AuthenticationError("unknown key")
        now = datetime.now(UTC)
        if rec.status is KeyStatus.REVOKED:
            raise AuthenticationError("key revoked", ReasonCode.KEY_REVOKED)
        if not rec.is_usable(now):
            raise AuthenticationError("key expired", ReasonCode.KEY_REVOKED)
        if not rec.ip_allowed(client_ip):
            raise AuthenticationError("source address not allowed for this key")
        principal = self._store.get_principal(rec.principal_id)
        if principal is None or not principal.active:
            raise AuthenticationError("principal inactive")
        # "Last used" is informational: write it at most once a minute per key so a busy key
        # does not cost a database write on every request.
        if rec.last_used_at is None or now - rec.last_used_at >= _TOUCH_INTERVAL:
            self._store.touch_key(rec.key_id, now)
        return rec

    def principal(self, principal_id: str) -> PrincipalRecord:
        p = self._store.get_principal(principal_id)
        if p is None:
            raise AuthenticationError("principal not found")
        return p

    # ----------------------------------------------------------------- cache
    def _cached(self, key_id: str) -> KeyRecord | None:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key_id)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
        rec = self._store.get_key(key_id)
        if rec is not None:
            with self._lock:
                self._cache[key_id] = (now, rec)
        return rec

    def _evict(self, key_id: str) -> None:
        with self._lock:
            self._cache.pop(key_id, None)
