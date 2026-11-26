"""Server-side state for the HTTP Core API (decisions, held requests, reservations).

Kept server-side so clients can only reference ids, never send state. PostgreSQL makes
it shared by every API replica (no sticky routing needed). Records expire after a TTL.

What is NEVER stored here: prompt/response content. Requests are saved without their
`contents`, and decisions without `redacted_contents`. Content was already inspected
during decide(); approval release re-checks kill switch, policy and registry facts.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine


class StateStore(Protocol):
    def put(self, kind: str, key: str, owner: str, doc: dict[str, Any]) -> None: ...
    def get(self, kind: str, key: str, owner: str) -> dict[str, Any] | None: ...
    def claim(self, kind: str, key: str, owner: str, doc: dict[str, Any]) -> bool: ...
    def delete(self, kind: str, key: str) -> bool: ...


class InMemoryStateStore:
    def __init__(self, ttl_s: float = 900) -> None:
        self._ttl = ttl_s
        self._d: dict[tuple[str, str], tuple[float, str, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def put(self, kind: str, key: str, owner: str, doc: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._lock:
            self._d = {k: v for k, v in self._d.items() if now - v[0] < self._ttl}
            self._d[(kind, key)] = (now, owner, doc)

    def delete(self, kind: str, key: str) -> bool:
        with self._lock:
            return self._d.pop((kind, key), None) is not None

    def claim(self, kind: str, key: str, owner: str, doc: dict[str, Any]) -> bool:
        """Atomically store only if no live record exists. True = this caller won."""
        now = time.monotonic()
        with self._lock:
            hit = self._d.get((kind, key))
            if hit is not None and now - hit[0] < self._ttl:
                return False
            self._d[(kind, key)] = (now, owner, doc)
            return True

    def get(self, kind: str, key: str, owner: str) -> dict[str, Any] | None:
        with self._lock:
            hit = self._d.get((kind, key))
        if hit is None or time.monotonic() - hit[0] >= self._ttl or hit[1] != owner:
            return None
        return hit[2]


class PgStateStore:
    def __init__(self, engine: Engine, ttl_s: float = 900) -> None:
        self._e = engine
        self._ttl = timedelta(seconds=ttl_s)

    def put(self, kind: str, key: str, owner: str, doc: dict[str, Any]) -> None:
        from captain.db.schema import api_state as t

        now = datetime.now(UTC)
        stmt = pg_insert(t).values(
            kind=kind, key=key, owner=owner, doc=doc, expires_at=now + self._ttl
        )
        with self._e.begin() as c:
            c.execute(
                stmt.on_conflict_do_update(
                    index_elements=["kind", "key"],
                    set_={
                        "owner": stmt.excluded.owner,
                        "doc": stmt.excluded.doc,
                        "expires_at": stmt.excluded.expires_at,
                    },
                )
            )
            c.execute(delete(t).where(t.c.expires_at < now))  # housekeeping

    def delete(self, kind: str, key: str) -> bool:
        from captain.db.schema import api_state as t

        with self._e.begin() as c:
            row = c.execute(
                delete(t).where(and_(t.c.kind == kind, t.c.key == key)).returning(t.c.key)
            ).first()
        return row is not None

    def claim(self, kind: str, key: str, owner: str, doc: dict[str, Any]) -> bool:
        from captain.db.schema import api_state as t

        now = datetime.now(UTC)
        with self._e.begin() as c:
            c.execute(delete(t).where(and_(t.c.kind == kind, t.c.key == key, t.c.expires_at < now)))
            row = c.execute(
                pg_insert(t)
                .values(kind=kind, key=key, owner=owner, doc=doc, expires_at=now + self._ttl)
                .on_conflict_do_nothing()
                .returning(t.c.key)
            ).first()
        return row is not None

    def get(self, kind: str, key: str, owner: str) -> dict[str, Any] | None:
        from captain.db.schema import api_state as t

        with self._e.connect() as c:
            row = c.execute(
                select(t.c.doc).where(
                    and_(
                        t.c.kind == kind,
                        t.c.key == key,
                        t.c.owner == owner,
                        t.c.expires_at > datetime.now(UTC),
                    )
                )
            ).first()
        doc: dict[str, Any] | None = row.doc if row else None
        return doc
