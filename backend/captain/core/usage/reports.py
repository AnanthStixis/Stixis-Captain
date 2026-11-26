"""Chargeback and provider reconciliation (FR-13, UC-07), computed from the evidence
ledger so reports and audit evidence always agree.

- Chargeback: settled cost per cost centre / team / project / model for a period.
- Reconciliation: Captain's settled cost per day × model vs the provider's figures;
  any difference above the threshold (default 2%) or a row missing on either side is
  an exception with its breakdown, so the cause can be found (missing traffic,
  pricing change, discount).

Provider figures are imported in a normalised CSV (day, provider, model, cost_micros).
Per-provider adapters for usage/cost APIs convert into that shape; they need the
client's provider admin credentials and are added per client.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from captain.core.ledger.chain import LedgerEntry

SETTLED = "usage.settled"


@dataclass(frozen=True, slots=True)
class SettledCost:
    day: date
    model: str
    provider: str
    cost: int
    source: str  # PROVIDER_REPORTED | ESTIMATED | …
    scopes: dict[str, str]  # kind -> id, e.g. {"cost_centre": "CC1", "team": "t1"}


def settled_costs(entries: Iterable[LedgerEntry]) -> list[SettledCost]:
    out = []
    for e in entries:
        if e.event_type != SETTLED:
            continue
        b = e.body
        usage = b.get("usage") or {}
        raw = (b.get("details") or {}).get("scopes") or ""
        scopes = dict(s.split(":", 1) for s in str(raw).split(",") if ":" in s)
        out.append(
            SettledCost(
                day=date.fromisoformat(str(b["occurred_at"])[:10]),
                model=str(b.get("model") or "unknown"),
                provider=str(usage.get("provider") or "unknown"),
                cost=int(usage.get("cost") or 0),
                source=str(usage.get("source") or "UNAVAILABLE"),
                scopes=scopes,
            )
        )
    return out


CHARGEBACK_DIMENSIONS = ("cost_centre", "team", "project", "model")


def chargeback(costs: Iterable[SettledCost], start: date, end: date) -> list[dict[str, str | int]]:
    """Rows grouped by cost centre, team, project and model for start ≤ day ≤ end."""
    totals: dict[tuple[str, ...], list[int]] = defaultdict(lambda: [0, 0, 0])
    for c in costs:
        if not (start <= c.day <= end):
            continue
        key = (
            c.scopes.get("cost_centre", "unattributed"),
            c.scopes.get("team", "unattributed"),
            c.scopes.get("project", "unattributed"),
            c.model,
        )
        t = totals[key]
        t[0] += c.cost
        t[1] += 1
        t[2] += c.cost if c.source != "PROVIDER_REPORTED" else 0
    return [
        {"cost_centre": k[0], "team": k[1], "project": k[2], "model": k[3],
         "cost_micros": v[0], "calls": v[1], "estimated_cost_micros": v[2]}
        for k, v in sorted(totals.items())
    ]  # fmt: skip


def to_csv(rows: list[dict[str, str | int]]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


@dataclass(frozen=True, slots=True)
class ProviderCost:
    day: date
    provider: str
    model: str
    cost: int


def parse_provider_csv(text: str) -> list[ProviderCost]:
    rows = csv.DictReader(io.StringIO(text))
    need = {"day", "provider", "model", "cost_micros"}
    if not rows.fieldnames or not need <= set(rows.fieldnames):
        raise ValueError(f"provider CSV needs columns {sorted(need)}")
    return [
        ProviderCost(date.fromisoformat(r["day"]), r["provider"], r["model"], int(r["cost_micros"]))
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class ReconciliationLine:
    day: date
    model: str
    captain: int
    provider: int
    variance_pct: float | None  # None when provider figure is 0
    exception: str | None  # None | "VARIANCE" | "MISSING_IN_PROVIDER" | "MISSING_IN_CAPTAIN"


def reconcile(
    captain: Iterable[SettledCost], provider: Iterable[ProviderCost], threshold_pct: float = 2.0
) -> list[ReconciliationLine]:
    ours: dict[tuple[date, str], int] = defaultdict(int)
    theirs: dict[tuple[date, str], int] = defaultdict(int)
    for c in captain:
        ours[(c.day, c.model)] += c.cost
    for p in provider:
        theirs[(p.day, p.model)] += p.cost
    out = []
    for key in sorted(set(ours) | set(theirs)):
        a, b = ours.get(key, 0), theirs.get(key, 0)
        exc: str | None
        pct: float | None
        if key not in theirs:
            exc, pct = "MISSING_IN_PROVIDER", None
        elif key not in ours:
            exc, pct = "MISSING_IN_CAPTAIN", None
        else:
            pct = (a - b) * 100.0 / b if b else None
            exc = (
                "VARIANCE"
                if (pct is None and a != b) or (pct is not None and abs(pct) > threshold_pct)
                else None
            )
        out.append(ReconciliationLine(key[0], key[1], a, b, pct, exc))
    return out
