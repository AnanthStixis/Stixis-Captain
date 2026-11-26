"""PostgreSQL advisory-lock lease: exactly one worker across all replicas.

The lock is session-scoped, so it is held on a dedicated connection; if the holder
process dies, PostgreSQL drops the session and the lock, and a standby takes over.
"""

from __future__ import annotations

import hashlib

import psycopg

from captain.db.bus import plain_dsn


def lease_name(database_url: str) -> str:
    """One writer per install. Advisory locks are per database, so installs that share a
    database through separate schemas (search_path in the URL options) get separate locks."""
    from sqlalchemy.engine import make_url

    opts = make_url(database_url).query.get("options") or ""
    opts = opts if isinstance(opts, str) else ",".join(opts)
    return "captain-core-worker" + (f":{opts}" if opts else "")


class PgAdvisoryLease:
    def __init__(self, dsn: str, name: str) -> None:
        self._dsn = plain_dsn(dsn)
        self._key = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
        self._conn: psycopg.Connection[tuple[object, ...]] | None = None

    def acquire(self) -> bool:
        if self._conn is not None and not self._conn.closed:
            # Prove the session (and so the lock) is still alive; a silently dropped
            # connection would otherwise let this worker believe it still holds the lease.
            try:
                self._conn.execute("SELECT 1")
                return True
            except psycopg.Error:
                self._conn.close()
                self._conn = None
        conn = psycopg.connect(self._dsn, autocommit=True)
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (self._key,)).fetchone()
        if row and row[0]:
            self._conn = conn
            return True
        conn.close()
        return False

    def release(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.execute("SELECT pg_advisory_unlock(%s)", (self._key,))
            self._conn.close()
        self._conn = None
