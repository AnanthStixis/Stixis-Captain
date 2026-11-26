"""Versioned model pricing and cost calculation.

- Prices are integer micro-units per 1,000,000 tokens; never floats.
- Every price row has a version and an effective_from. A call record stores the
  version used, so historic costs stay reproducible after price changes.
- Rounding is always up (ceil) so estimates and charges never under-count.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from captain.core.models import CostEstimate, TokenUsage

PER_MILLION = 1_000_000


class PriceNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class ModelPrice:
    model: str  # Captain model alias, e.g. "claude-sonnet"
    provider: str
    version: str
    effective_from: datetime
    input_per_m: int  # micro-units per 1M input tokens
    output_per_m: int
    cached_input_per_m: int | None = None  # None → billed as normal input
    cache_write_per_m: int | None = None
    currency: str = "USD"


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


class PriceCatalog:
    """`persist` stores a new price (Postgres); `reload` re-reads all prices, used when a
    model has no price here yet, so a price set on the console reaches every gateway
    without a restart (at most once per `reload_every_s`)."""

    def __init__(
        self,
        persist: Callable[[ModelPrice], None] | None = None,
        reload: Callable[[], list[ModelPrice]] | None = None,
        reload_every_s: float = 5.0,
    ) -> None:
        self._rows: dict[str, list[ModelPrice]] = {}
        self._lock = threading.Lock()
        self._persist, self._reload = persist, reload
        self._reload_every, self._last_reload = reload_every_s, 0.0

    def publish(self, price: ModelPrice) -> None:
        """A new price version from the console: stored, then in force here at once."""
        if self._persist is not None:
            self._persist(price)
        self.add(price)

    def latest(self) -> list[ModelPrice]:
        with self._lock:
            return [rows[-1] for rows in self._rows.values() if rows]

    def _refresh(self) -> bool:
        now = time.monotonic()
        if self._reload is None or now - self._last_reload < self._reload_every:
            return False
        self._last_reload = now
        fresh = self._reload()
        with self._lock:
            for p in fresh:
                rows = self._rows.setdefault(p.model, [])
                if not any(r.version == p.version for r in rows):
                    rows.append(p)
                    rows.sort(key=lambda r: r.effective_from)
        return True

    def add(self, price: ModelPrice) -> None:
        with self._lock:
            rows = self._rows.setdefault(price.model, [])
            if any(r.version == price.version for r in rows):
                raise ValueError(f"price version {price.version} already exists for {price.model}")
            rows.append(price)
            rows.sort(key=lambda r: r.effective_from)

    def price_at(self, model: str, at: datetime | None = None) -> ModelPrice:
        at = at or datetime.now(UTC)
        rows = [r for r in self._rows.get(model, []) if r.effective_from <= at]
        if not rows and self._refresh():
            rows = [r for r in self._rows.get(model, []) if r.effective_from <= at]
        if not rows:
            raise PriceNotFoundError(f"no price for {model} at {at.isoformat()}")
        return rows[-1]

    def by_version(self, model: str, version: str) -> ModelPrice:
        for r in self._rows.get(model, []):
            if r.version == version:
                return r
        raise PriceNotFoundError(f"{model}@{version}")


def cost_of(usage: TokenUsage, price: ModelPrice, cache_write_tokens: int = 0) -> int:
    cached = usage.cached_input_tokens if price.cached_input_per_m is not None else 0
    uncached = max(usage.input_tokens - cached, 0)
    total = (
        uncached * price.input_per_m
        + cached * (price.cached_input_per_m or 0)
        + cache_write_tokens * (price.cache_write_per_m or price.input_per_m)
        + usage.output_tokens * price.output_per_m  # reasoning tokens are billed as output
    )
    return _ceil_div(total, PER_MILLION)


def estimate_input_tokens(text_chars: int) -> int:
    """Conservative pre-call estimate when no provider tokenizer is available:
    ~1 token per 3 characters (real tokenizers average ~4). Over-estimating is the
    safe direction for budget reservation."""
    return math.ceil(text_chars / 3) + 8  # + message framing overhead


def estimate_cost(price: ModelPrice, input_tokens: int, max_output_tokens: int) -> CostEstimate:
    worst = TokenUsage(input_tokens=input_tokens, output_tokens=max_output_tokens)
    return CostEstimate(
        input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
        amount=cost_of(worst, price),
        price_version=price.version,
    )
