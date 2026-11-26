"""Erasure by crypto-shredding (ADR-005 §1).

Personal identifiers never enter the signed evidence ledger in the clear. Each person gets
a random key; their id is written as `subj_<HMAC(key, id)>`. The key and the
pseudonym -> person mapping live in `subject_keys`, outside the ledger. Erasure deletes
that row: the person's events stay in the chain (verification still passes) but can no
longer be linked to them, by anyone, including from a database backup taken afterwards.

What is personal (decided 2026-11-26): person principal ids (anything that is not a
known service account, application or agent) and client IP addresses. Client IPs are
hashed with a key per UTC day (`ipday:<date>`); the worker deletes day keys older than
the retention period, after which those hashes cannot be recomputed or linked.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

SUBJECT_PREFIX = "subj_"
IP_PREFIX = "ip_"
ERASED = "erased subject"
IP_DAY = "ipday:"

# Ledger body detail keys that may carry a person id, a list of them, or budget scopes.
_PERSON_KEYS = frozenset(
    {"actor", "actor_id", "approver", "principal", "principal_id", "requester", "by",
     "user", "owner_id", "decided_by", "requested_by"}
)  # fmt: skip
_PERSON_LIST_KEYS = frozenset({"approvers", "recipients"})
_SCOPE_KEYS = frozenset({"scope", "scopes"})
_IP_KEYS = frozenset({"client_ip", "ip"})
# Ids that are Captain itself, not people.
SYSTEM_ACTORS = frozenset({"captain", "system", "worker", "unattributed", "invalid"})


class SubjectStore(Protocol):
    def get_or_create(self, subject: str, make: Callable[[], tuple[bytes, str]]) -> str:
        """Return the subject's pseudonym, creating its key with `make(key)` if needed."""
        ...

    def key(self, subject: str) -> bytes | None: ...
    def subject_for(self, pseudonym: str) -> str | None: ...
    def pseudonym_of(self, subject: str) -> str | None: ...
    def delete(self, subject: str) -> str | None:
        """Delete the key; returns the pseudonym it had (None if there was none)."""
        ...

    def delete_prefix_before(self, prefix: str, before: datetime) -> int: ...


class InMemorySubjectStore:
    def __init__(self) -> None:
        self._rows: dict[str, tuple[bytes, str, datetime]] = {}
        self._lock = threading.Lock()

    def get_or_create(self, subject: str, make: Callable[[], tuple[bytes, str]]) -> str:
        with self._lock:
            if subject not in self._rows:
                key, pseudo = make()
                self._rows[subject] = (key, pseudo, datetime.now(UTC))
            return self._rows[subject][1]

    def key(self, subject: str) -> bytes | None:
        r = self._rows.get(subject)
        return r[0] if r else None

    def subject_for(self, pseudonym: str) -> str | None:
        return next((s for s, r in self._rows.items() if r[1] == pseudonym), None)

    def pseudonym_of(self, subject: str) -> str | None:
        r = self._rows.get(subject)
        return r[1] if r else None

    def delete(self, subject: str) -> str | None:
        with self._lock:
            r = self._rows.pop(subject, None)
        return r[1] if r else None

    def delete_prefix_before(self, prefix: str, before: datetime) -> int:
        with self._lock:
            gone = [s for s, r in self._rows.items() if s.startswith(prefix) and r[2] < before]
            for s in gone:
                del self._rows[s]
        return len(gone)


def _mac(key: bytes, value: str, n: int) -> str:
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:n]


class Pseudonymiser:
    def __init__(
        self,
        store: SubjectStore,
        is_person: Callable[[str], bool],
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self._store = store
        self._is_person = is_person
        self._today = today

    @property
    def store(self) -> SubjectStore:
        return self._store

    # --------------------------------------------------------------- write side
    def person(self, pid: Any) -> Any:
        if not isinstance(pid, str) or not pid or pid in SYSTEM_ACTORS:
            return pid
        if pid.startswith((SUBJECT_PREFIX, IP_PREFIX)) or not self._is_person(pid):
            return pid

        def make() -> tuple[bytes, str]:
            k = secrets.token_bytes(32)
            return k, SUBJECT_PREFIX + _mac(k, pid, 24)

        return self._store.get_or_create(pid, make)

    def ip(self, value: Any) -> Any:
        if not isinstance(value, str) or not value or value.startswith(IP_PREFIX):
            return value
        if value == "invalid":
            return value
        day = IP_DAY + self._today().isoformat()

        def make() -> tuple[bytes, str]:
            return secrets.token_bytes(32), day  # the day key has no pseudonym of its own

        self._store.get_or_create(day, make)
        key = self._store.key(day)
        if key is None:  # never hash with an empty key
            raise RuntimeError("client-IP day key missing")
        return IP_PREFIX + _mac(key, value, 16)

    def _scope(self, s: Any) -> Any:
        if not isinstance(s, str):
            return s
        parts = []
        for p in s.split(","):
            kind, sep, rest = p.partition(":")
            if sep and kind in ("principal", "ptool"):
                pid, sep2, tail = rest.partition(":") if kind == "ptool" else (rest, "", "")
                p = f"{kind}:{self.person(pid)}{sep2}{tail}"
            parts.append(p)
        return ",".join(parts)

    def event(self, body: dict[str, Any]) -> dict[str, Any]:
        """The ledger body with every personal identifier replaced."""
        out = dict(body)
        out["principal_id"] = self.person(body.get("principal_id"))
        d = body.get("details")
        if isinstance(d, dict):
            nd: dict[str, Any] = {}
            for k, v in d.items():
                if k in _PERSON_KEYS:
                    v = self.person(v)
                elif k in _PERSON_LIST_KEYS and isinstance(v, str):
                    v = ",".join(str(self.person(x)) for x in v.split(",") if x)
                elif k in _SCOPE_KEYS:
                    v = self._scope(v)
                elif k in _IP_KEYS:
                    v = self.ip(v)
                nd[k] = v
            if nd.get("scope") == "principal" and "target" in nd:  # kill switch on a person
                nd["target"] = self.person(nd["target"])
            out["details"] = nd
        return out

    # ---------------------------------------------------------------- read side
    def display(self, value: Any) -> Any:
        """A pseudonym back to the person, or "erased subject" once their key is gone."""
        if isinstance(value, str) and value.startswith(SUBJECT_PREFIX):
            return self._store.subject_for(value) or ERASED
        return value

    def lookup(self, pid: str) -> str:
        """For filters: the pseudonym a person's events were written under."""
        return self._store.pseudonym_of(pid) or pid

    def _display_scope(self, s: Any) -> Any:
        if not isinstance(s, str) or SUBJECT_PREFIX not in s:
            return s
        parts = []
        for p in s.split(","):
            kind, sep, rest = p.partition(":")
            if sep and kind in ("principal", "ptool"):
                pid, sep2, tail = rest.partition(":") if kind == "ptool" else (rest, "", "")
                p = f"{kind}:{self.display(pid)}{sep2}{tail}"
            parts.append(p)
        return ",".join(parts)

    def resolve(self, body: dict[str, Any]) -> dict[str, Any]:
        out = dict(body)
        out["principal_id"] = self.display(body.get("principal_id"))
        d = body.get("details")
        if isinstance(d, dict):
            nd: dict[str, Any] = {}
            for k, v in d.items():
                if k in _PERSON_KEYS:
                    v = self.display(v)
                elif k in _PERSON_LIST_KEYS and isinstance(v, str):
                    v = ",".join(str(self.display(x)) for x in v.split(",") if x)
                elif k in _SCOPE_KEYS:
                    v = self._display_scope(v)
                nd[k] = v
            if nd.get("scope") == "principal" and "target" in nd:
                nd["target"] = self.display(nd["target"])
            out["details"] = nd
        return out

    def prune_ip_days(self, keep_days: int, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=keep_days)
        return self._store.delete_prefix_before(IP_DAY, cutoff)
