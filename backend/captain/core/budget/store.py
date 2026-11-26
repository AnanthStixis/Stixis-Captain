"""Budget counter stores: in-memory (tests, single process); PostgreSQL in production
(captain.db.stores.PgBudgetStore, row locks in a fixed order).

Both implement the same atomic semantics (CTO F6):
- reserve: if any HARD level would exceed its limit, nothing is reserved anywhere.
- settle/release: exactly once per reservation; a second call fails.
- expired reservations are released by `sweep()` so crashed calls don't hold budget.
A Valkey/Redis store can be added behind the same protocol if measured load needs it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

from captain.core.budget.model import BucketState, BudgetLevel


class ReservationRejected(Exception):
    def __init__(self, bucket: str) -> None:
        super().__init__(f"budget bucket {bucket} would be exceeded")
        self.bucket = bucket


class UnknownReservation(Exception):
    pass


class BudgetStoreUnavailable(Exception):
    pass


@dataclass(frozen=True, slots=True)
class StoredReservation:
    reservation_id: str
    amount: int
    buckets: tuple[str, ...]


class BudgetStore(Protocol):
    def reserve(
        self, reservation_id: str, levels: list[BudgetLevel], amount: int, ttl_s: int
    ) -> None: ...
    def settle(self, reservation_id: str, charged: int) -> StoredReservation: ...
    def state(self, bucket: str) -> BucketState: ...
    def add_spend(self, bucket: str, amount: int) -> None: ...
    def add_extra(self, bucket: str, amount: int) -> None: ...
    def mark_alerted(self, bucket: str, threshold: float) -> bool: ...
    def sweep(self) -> int: ...
    def hit_rate(
        self, bucket: str, requests: int, tokens: int, rpm: int | None, tpm: int | None
    ) -> bool: ...


class InMemoryBudgetStore:
    def __init__(self) -> None:
        self._b: dict[str, dict[str, int]] = {}
        self._res: dict[str, tuple[float, StoredReservation]] = {}
        self._rate: dict[str, tuple[int, int, int]] = {}  # bucket -> (minute, req, tok)
        self._alerted: set[str] = set()
        self._lock = threading.Lock()

    def _bucket(self, name: str, limit: int = 0) -> dict[str, int]:
        b = self._b.setdefault(name, {"limit": limit, "spent": 0, "reserved": 0, "extra": 0})
        if limit:
            b["limit"] = limit
        return b

    def reserve(
        self, reservation_id: str, levels: list[BudgetLevel], amount: int, ttl_s: int
    ) -> None:
        with self._lock:
            for lv in levels:
                b = self._bucket(lv.bucket, lv.limit)
                # Overrides live with the counters, so every instance sees them.
                if lv.hard and b["spent"] + b["reserved"] + amount > lv.limit + b["extra"]:
                    raise ReservationRejected(lv.bucket)
            for lv in levels:
                self._b[lv.bucket]["reserved"] += amount
            self._res[reservation_id] = (
                time.monotonic() + ttl_s,
                StoredReservation(reservation_id, amount, tuple(lv.bucket for lv in levels)),
            )

    def settle(self, reservation_id: str, charged: int) -> StoredReservation:
        with self._lock:
            hit = self._res.pop(reservation_id, None)
            if hit is None:
                raise UnknownReservation(reservation_id)
            r = hit[1]
            for name in r.buckets:
                self._b[name]["reserved"] -= r.amount
                self._b[name]["spent"] += charged
            return r

    def state(self, bucket: str) -> BucketState:
        with self._lock:
            b = self._bucket(bucket)
            return BucketState(b["limit"] + b["extra"], b["spent"], b["reserved"])

    def mark_alerted(self, bucket: str, threshold: float) -> bool:
        """True only for the first caller for this bucket and threshold."""
        with self._lock:
            key = f"{bucket}|{threshold}"
            if key in self._alerted:
                return False
            self._alerted.add(key)
            return True

    def add_extra(self, bucket: str, amount: int) -> None:
        with self._lock:
            self._bucket(bucket)["extra"] += amount

    def add_spend(self, bucket: str, amount: int) -> None:
        with self._lock:
            self._bucket(bucket)["spent"] += amount

    def sweep(self) -> int:
        now = time.monotonic()
        expired = [rid for rid, (exp, _) in self._res.items() if exp <= now]
        for rid in expired:
            try:
                self.settle(rid, 0)
            except UnknownReservation:
                pass
        return len(expired)

    def hit_rate(
        self, bucket: str, requests: int, tokens: int, rpm: int | None, tpm: int | None
    ) -> bool:
        minute = int(time.time() // 60)
        with self._lock:
            m, req, tok = self._rate.get(bucket, (minute, 0, 0))
            if m != minute:
                req, tok = 0, 0
            if (rpm is not None and req + requests > rpm) or (
                tpm is not None and tok + tokens > tpm
            ):
                return False
            self._rate[bucket] = (minute, req + requests, tok + tokens)
            return True
