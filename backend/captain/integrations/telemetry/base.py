"""Vendor usage importers: bring usage that happened OUTSIDE Captain into the evidence
ledger as OBSERVED events.

Honest coverage (CTO review): an observed record says "the vendor reports this happened".
Captain did not see it first and could not have stopped it, so it is never GOVERNED, it
never touches a budget, and it is not mixed into governed spend. Importing the same
period twice is safe: event ids are derived from the vendor and the record's own id, and
the ledger ignores an event it has already sealed.

Privacy: vendor identities (emails, logins) are mapped to Captain principals through an
explicit map. An identity that is not in the map is recorded as a keyed-free short hash,
never as the raw email or login, so evidence does not become a list of people's emails.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from captain.core.decision_api import CoreDecisionAPI
from captain.core.models import (
    ActualUsage,
    Channel,
    CoverageClass,
    EventType,
    TokenUsage,
    UsageEvent,
    UsageSource,
)

Facts = dict[str, str | int | float | bool | None]


@dataclass(frozen=True, slots=True)
class ObservedRecord:
    vendor: str  # e.g. "github_copilot", "anthropic_claude_code"
    source_id: str  # stable and unique within the vendor
    day: date  # UTC day the usage belongs to (vendors report daily aggregates)
    actor: str | None = None  # vendor-side identity: email, login or key name
    model: str | None = None
    tokens: TokenUsage | None = None
    cost_micro: int | None = None  # Captain money unit (millionths); None if not reported
    facts: Facts = field(default_factory=dict)  # the vendor's own counters, as numbers
    coverage: CoverageClass = CoverageClass.OBSERVED  # BYPASS_DETECTED for discovery logs
    channel: Channel = Channel.IDE  # DESKTOP for the desktop agent


class RecordSource(Protocol):
    vendor: str

    def fetch(self, day: date) -> Iterable[ObservedRecord]: ...


class ImportFailure(Exception):
    """The vendor refused, was unreachable, or answered with something unusable."""


class ActorMap:
    """vendor identity (case-insensitive) -> Captain principal id."""

    def __init__(self, mapping: Mapping[str, str] | None = None) -> None:
        self._m = {k.strip().lower(): v for k, v in (mapping or {}).items()}

    def principal(self, vendor: str, actor: str | None) -> tuple[str | None, bool]:
        """(principal id, mapped?). Unmapped actors get a stable pseudonym."""
        if not actor:
            return None, False
        hit = self._m.get(actor.strip().lower())
        if hit:
            return hit, True
        digest = hashlib.sha256(f"{vendor}|{actor.strip().lower()}".encode()).hexdigest()[:12]
        return f"vendor-unmapped:{digest}", False


def event_id(rec: ObservedRecord) -> UUID:
    return uuid5(NAMESPACE_URL, f"captain-observed|{rec.vendor}|{rec.source_id}")


def to_event(rec: ObservedRecord, actors: ActorMap) -> UsageEvent:
    principal, mapped = actors.principal(rec.vendor, rec.actor)
    usage = None
    if rec.tokens is not None or rec.cost_micro is not None:
        usage = ActualUsage(
            usage=rec.tokens or TokenUsage(),
            source=UsageSource.OBSERVED,
            cost=rec.cost_micro or 0,
            price_version="vendor-reported",
            provider=rec.vendor,
        )
    details: Facts = {
        "vendor": rec.vendor,
        "period": rec.day.isoformat(),
        "granularity": "day",
        "principal_mapped": mapped,
        **{f"v_{k}": v for k, v in rec.facts.items()},
    }
    return UsageEvent(
        event_id=event_id(rec),
        trace_id=f"import-{rec.vendor}"[:128],
        occurred_at=datetime(rec.day.year, rec.day.month, rec.day.day, tzinfo=UTC),
        event_type=EventType.CALL_OBSERVED,
        channel=rec.channel,
        coverage=rec.coverage,
        principal_id=principal,
        register_id=(
            f"bypass:{rec.vendor}"
            if rec.coverage is CoverageClass.BYPASS_DETECTED
            else f"observed:{rec.vendor}"
        ),
        model=rec.model,
        usage=usage,
        details=details,
    )


@dataclass
class ImportResult:
    recorded: int = 0
    unmapped_actors: int = 0
    failed: int = 0


def import_records(
    core: CoreDecisionAPI, records: Iterable[ObservedRecord], actors: ActorMap
) -> ImportResult:
    """Write each record as an OBSERVED event. A record that cannot be queued is counted,
    not silently dropped; the caller decides whether to retry the day."""
    out = ImportResult()
    for rec in records:
        ev = to_event(rec, actors)
        try:
            core.record(ev)
        except Exception:  # evidence unavailable etc.: surface via the count
            out.failed += 1
            continue
        out.recorded += 1
        if ev.details and ev.details.get("principal_mapped") is False and rec.actor:
            out.unmapped_actors += 1
    return out
