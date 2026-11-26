"""Budget definitions and period bucketing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class Period(StrEnum):
    DAILY = "DAILY"
    MONTHLY = "MONTHLY"


@dataclass(frozen=True, slots=True)
class BudgetDefinition:
    """`scope` is a Core scope string, e.g. org:o1, cost_centre:CC-1, team:t1,
    project:p1, app:a1, principal:u1, key:k1, model:claude-sonnet."""

    scope: str
    period: Period
    limit: int  # micro-units
    hard: bool = True
    alert_thresholds: tuple[float, ...] = (0.8, 1.0)

    def bucket(self, at: datetime | None = None) -> str:
        at = (at or datetime.now(UTC)).astimezone(UTC)
        stamp = at.strftime("%Y-%m-%d") if self.period is Period.DAILY else at.strftime("%Y-%m")
        return f"budget:{self.scope}:{self.period.value}:{stamp}"


@dataclass(frozen=True, slots=True)
class RateLimitDefinition:
    scope: str
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None


@dataclass(frozen=True, slots=True)
class BudgetLevel:
    """One budget bucket checked in a reservation."""

    bucket: str
    limit: int
    hard: bool


@dataclass(frozen=True, slots=True)
class BucketState:
    limit: int
    spent: int
    reserved: int
