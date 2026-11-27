"""Budget and Rate Limit service: reserve → settle/release, alerts, audited overrides."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from captain.core.budget.model import (
    BucketState,
    BudgetDefinition,
    BudgetLevel,
    RateLimitDefinition,
)
from captain.core.budget.store import BudgetStore, ReservationRejected
from captain.core.decision_api import BudgetExceededError
from captain.core.models import ReasonCode, Reservation


class BudgetNotConfiguredError(BudgetExceededError):
    reason_code = ReasonCode.BUDGET_NOT_CONFIGURED


class RateLimitedError(BudgetExceededError):
    reason_code = ReasonCode.RATE_LIMITED


class BudgetCatalog(Protocol):
    def budgets_for(self, scopes: tuple[str, ...]) -> list[BudgetDefinition]: ...
    def rate_limits_for(self, scopes: tuple[str, ...]) -> list[RateLimitDefinition]: ...


class InMemoryBudgetCatalog:
    def __init__(self) -> None:
        self.budgets: dict[str, list[BudgetDefinition]] = {}
        self.rates: dict[str, RateLimitDefinition] = {}

    def add(self, d: BudgetDefinition) -> None:
        self.budgets[d.scope] = [d]  # one budget per scope: saving again edits it

    def add_rate(self, r: RateLimitDefinition) -> None:
        self.rates[r.scope] = r

    def remove_rate(self, scope: str) -> bool:
        return self.rates.pop(scope, None) is not None

    def remove(self, scope: str) -> bool:
        return self.budgets.pop(scope, None) is not None

    def budgets_for(self, scopes: tuple[str, ...]) -> list[BudgetDefinition]:
        return [d for s in scopes for d in self.budgets.get(s, [])]

    def rate_limits_for(self, scopes: tuple[str, ...]) -> list[RateLimitDefinition]:
        return [self.rates[s] for s in scopes if s in self.rates]


@dataclass(frozen=True, slots=True)
class BudgetAlert:
    scope: str
    bucket: str
    threshold: float
    spent: int
    limit: int


@dataclass(frozen=True, slots=True)
class BudgetOverride:
    scope: str
    bucket: str
    extra: int
    actor_id: str
    reason: str
    at: datetime


class BudgetService:
    def __init__(
        self,
        catalog: BudgetCatalog,
        store: BudgetStore,
        on_alert: Callable[[BudgetAlert], None],
        on_override: Callable[[BudgetOverride], None],
        reservation_ttl: timedelta = timedelta(minutes=10),
        require_budget: bool = False,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._on_alert = on_alert
        self._on_override = on_override
        self._ttl = reservation_ttl
        # Production: a call with no applicable budget is refused, so nothing can spend
        # without a cap. Tests and sandboxes may switch this off.
        self._require = require_budget
        self._defs: dict[str, BudgetDefinition] = {}  # bucket -> definition (for alerts)
        self._lock = threading.Lock()

    def _levels(
        self, scopes: tuple[str, ...], at: datetime
    ) -> list[tuple[BudgetDefinition, BudgetLevel]]:
        out = []
        for d in self._catalog.budgets_for(scopes):
            bucket = d.bucket(at)
            self._defs[bucket] = d
            out.append((d, BudgetLevel(bucket, d.limit, d.hard)))
        return out

    def check_rate(self, scopes: tuple[str, ...], tokens: int) -> None:
        for r in self._catalog.rate_limits_for(scopes):
            if not self._store.hit_rate(
                r.scope, 1, tokens, r.requests_per_minute, r.tokens_per_minute
            ):
                raise RateLimitedError(f"rate limit exceeded for {r.scope}", r.scope)

    def reserve(self, request_id: UUID, scopes: tuple[str, ...], amount: int) -> Reservation:
        now = datetime.now(UTC)
        levels = self._levels(scopes, now)
        if self._require and not any(lv.hard for _, lv in levels):
            raise BudgetNotConfiguredError(
                "no hard budget applies to this call (organisation, cost centre, team, "
                "project, application, principal, key or model); refusing to spend uncapped",
                "none",
            )
        res = Reservation(
            request_id=request_id,
            amount=amount,
            budget_keys=tuple(lv.bucket for _, lv in levels),
            expires_at=now + self._ttl,
        )
        try:
            self._store.reserve(
                str(res.reservation_id),
                [lv for _, lv in levels],
                amount,
                int(self._ttl.total_seconds()),
            )
        except ReservationRejected as e:
            raise BudgetExceededError(f"budget {e.bucket} exhausted", e.bucket) from e
        return res

    def settle(self, reservation: Reservation, charged: int) -> None:
        self._store.settle(str(reservation.reservation_id), charged)
        self._check_alerts(reservation.budget_keys)

    def sweep_expired(self) -> int:
        """Release reservations whose call never settled (crash, lost connection)."""
        return self._store.sweep()

    def usage_for(self, scopes: tuple[str, ...]) -> list[tuple[BudgetDefinition, BucketState]]:
        """Current-period state of every budget that applies to these scopes (read only)."""
        now = datetime.now(UTC)
        return [(d, self._store.state(d.bucket(now))) for d in self._catalog.budgets_for(scopes)]

    def state(self, bucket: str) -> BucketState:
        return self._store.state(bucket)

    def override(self, scope: str, extra: int, actor_id: str, reason: str) -> BudgetOverride:
        """Emergency raise for the current period. Always produces an audit record."""
        if extra <= 0 or not reason.strip():
            raise ValueError("override needs a positive amount and a reason")
        defs = self._catalog.budgets_for((scope,))
        if not defs:
            raise KeyError(scope)
        bucket = defs[0].bucket()
        with self._lock:
            # Persisted in the budget store (shared by all instances, survives restarts).
            self._store.add_extra(bucket, extra)
        o = BudgetOverride(scope, bucket, extra, actor_id, reason, datetime.now(UTC))
        self._on_override(o)
        return o

    def _check_alerts(self, buckets: tuple[str, ...]) -> None:
        for b in buckets:
            s = self._store.state(b)
            if s.limit <= 0:
                continue
            d = self._defs.get(b)
            if d is None:
                continue
            for t in sorted(d.alert_thresholds):
                # The store records "already alerted" atomically, so each threshold alerts
                # once per bucket (= per period) across all instances and restarts.
                if s.spent >= s.limit * t and self._store.mark_alerted(b, t):
                    self._on_alert(BudgetAlert(d.scope, b, t, s.spent, s.limit))
