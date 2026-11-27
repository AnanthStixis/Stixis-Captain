"""Read-only queries for the Admin API and dashboard.

Writes always go through AdminService (permission-checked, recorded in the ledger).
Reads are kept separate so the Core store interfaces stay minimal. Two implementations
share one protocol: PostgreSQL (production) and in-memory (tests).

Nothing returned here contains secrets: keys are listed by id and metadata only,
never their hash; events never carry prompt content (the ledger never stores it).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from captain.api.describe import describe, describe_alert
from captain.core.alerts import Alert, InMemoryAlertStore
from captain.core.approvals.service import InMemoryApprovalStore
from captain.core.budget.model import BudgetDefinition, Period, RateLimitDefinition
from captain.core.budget.service import InMemoryBudgetCatalog
from captain.core.context.service import InMemoryRegisterStore
from captain.core.identity.model import KeyRecord, PrincipalRecord
from captain.core.identity.service import InMemoryIdentityStore
from captain.core.ledger.chain import LedgerEntry
from captain.core.models import ApprovalTicket, RegisterEntry
from captain.core.registry.service import InMemoryRegistryStore
from captain.core.subjects import Pseudonymiser


def key_view(k: KeyRecord) -> dict[str, Any]:
    return {
        "key_id": k.key_id,
        "principal_id": k.principal_id,
        "register_id": k.register_id,
        "project_id": k.project_id,
        "application_id": k.application_id,
        "environment": k.environment.value,
        "status": k.status.value,
        "label": k.label,
        "created_at": k.created_at,
        "expires_at": k.expires_at,
        "revoked_at": k.revoked_at,
        "revoked_reason": k.revoked_reason,
        "last_used_at": k.last_used_at,
        "rotated_from": k.rotated_from,
        "allowed_cidrs": list(k.allowed_cidrs),
    }  # secret_hash deliberately omitted


def principal_view(p: PrincipalRecord) -> dict[str, Any]:
    return {
        "id": p.id,
        "type": p.type,
        "org_id": p.org_id,
        "team_id": p.team_id,
        "display_name": p.display_name,
        "roles": sorted(p.roles),
        "active": p.active,
    }


def alert_view(a: Alert) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "kind": a.kind,
        "severity": a.severity.value,
        "subject": a.subject,
        "details": a.details,
        "register_id": a.register_id,
        "created_at": a.created_at,
        "delivered": a.delivered,
        "attempts": a.attempts,
        **describe_alert(a.kind),
    }


def event_view(e: LedgerEntry) -> dict[str, Any]:
    """An event as the dashboard shows it: the stored facts plus a plain-language
    description (headline, outcome, why) for people who are not engineers."""
    v = _raw_view(e)
    return {**v, **describe(v)}


def _raw_view(e: LedgerEntry) -> dict[str, Any]:
    b = e.body
    usage = b.get("usage") or {}
    return {
        "partition": e.partition,
        "seq": e.seq,
        "event_id": e.event_id,
        "event_type": e.event_type,
        "recorded_at": e.recorded_at,
        "request_id": b.get("request_id"),
        "trace_id": b.get("trace_id"),
        "channel": b.get("channel"),
        "coverage": b.get("coverage"),
        "principal_id": b.get("principal_id"),
        "register_id": b.get("register_id"),
        "decision": b.get("decision"),
        "reason_code": b.get("reason_code"),
        "policy_version": b.get("policy_version"),
        "model": b.get("model"),
        "tool": b.get("tool"),
        "risk_level": (b.get("details") or {}).get("risk_level"),
        "risk_score": (b.get("details") or {}).get("risk_score"),
        "cost_micros": usage.get("cost"),
        "usage_source": usage.get("source"),
        "input_tokens": (usage.get("usage") or {}).get("input_tokens"),
        "output_tokens": (usage.get("usage") or {}).get("output_tokens"),
        "latency_ms": usage.get("latency_ms"),
        "details": b.get("details"),
        "entry_hash": e.entry_hash,
        "anchor_id": e.anchor_id,
    }


# ----------------------------------------------------------------------------------------
# Summaries that the database computes, so a dashboard page never loads the whole ledger.
# The in-memory versions below compute the same answers from entries (tests, harness).

CoverageRow = tuple[str, str, str | None, int]  # (coverage, channel, principal, events)
PartitionRow = tuple[str, str, int, datetime]  # (partition, event_type, events, newest)


def _wanted(
    e: LedgerEntry,
    types: set[str] | None,
    decision: str | None,
    principal_id: str | None,
    register_id: str | None,
) -> bool:
    return (
        (types is None or e.event_type in types)
        and (decision is None or e.body.get("decision") == decision)
        and (principal_id is None or e.body.get("principal_id") == principal_id)
        and (register_id is None or e.partition == register_id)
    )


def page_from_entries(
    entries: list[LedgerEntry],
    types: set[str] | None,
    decision: str | None,
    principal_id: str | None,
    register_id: str | None,
    limit: int,
    offset: int,
) -> tuple[int, list[LedgerEntry]]:
    rows = [e for e in entries if _wanted(e, types, decision, principal_id, register_id)]
    # One total order shared with the SQL version (newest first, then partition, then newest
    # sequence), so a page is the same page whichever store answers it.
    rows.sort(key=lambda e: e.seq, reverse=True)
    rows.sort(key=lambda e: e.partition)
    rows.sort(key=lambda e: e.recorded_at, reverse=True)
    return len(rows), rows[offset : offset + limit]


def coverage_from_entries(
    entries: list[LedgerEntry], types: set[str], exclude: set[str]
) -> list[CoverageRow]:
    counts: dict[tuple[str, str, str | None], int] = {}
    for e in entries:
        if e.event_type not in types or e.partition in exclude:
            continue
        b = e.body
        k = (str(b.get("coverage") or "GOVERNED"), str(b.get("channel")), b.get("principal_id"))
        counts[k] = counts.get(k, 0) + 1
    return [(c, ch, p, n) for (c, ch, p), n in counts.items()]


def partitions_from_entries(entries: list[LedgerEntry]) -> list[PartitionRow]:
    stats: dict[tuple[str, str], tuple[int, datetime]] = {}
    for e in entries:
        n, last = stats.get((e.partition, e.event_type), (0, e.recorded_at))
        stats[(e.partition, e.event_type)] = (n + 1, max(last, e.recorded_at))
    return [(p, t, n, last) for (p, t), (n, last) in stats.items()]


# (day UTC, event_type, decision, reason_code, coverage, risk_level, model, usage_source,
#  events, cost, input_tokens, output_tokens)
OverviewRow = tuple[
    str, str, str | None, str | None, str | None, str | None, str | None, str | None,
    int, int, int, int,
]  # fmt: skip
# (key, calls, input_tokens, output_tokens, cost, estimated_cost, refused)
UsageRow = tuple[str, int, int, int, int, int, int]
OVERVIEW_TYPES = {
    "call.allowed", "call.masked", "call.refused", "call.observed", "tool.held", "usage.settled",
}  # fmt: skip


def _i(v: Any) -> int:
    return int(v or 0)


def percentile(sorted_values: list[int], p: float) -> float | None:
    """Linear interpolation between neighbours: the same definition as PostgreSQL's
    percentile_cont, so the SQL and in-Python answers agree."""
    if not sorted_values:
        return None
    pos = p * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def overview_from_entries(entries: list[LedgerEntry]) -> list[OverviewRow]:
    acc: dict[tuple[Any, ...], list[int]] = {}
    for e in entries:
        if e.event_type not in OVERVIEW_TYPES:
            continue
        b = e.body
        u = b.get("usage") or {}
        tok = u.get("usage") or {}
        key = (
            e.recorded_at.astimezone(UTC).date().isoformat(),
            e.event_type,
            b.get("decision"),
            b.get("reason_code"),
            b.get("coverage"),
            (b.get("details") or {}).get("risk_level"),
            b.get("model"),
            u.get("source"),
        )
        a = acc.setdefault(key, [0, 0, 0, 0])
        a[0] += 1
        a[1] += _i(u.get("cost"))
        a[2] += _i(tok.get("input_tokens"))
        a[3] += _i(tok.get("output_tokens"))
    return [(*k, *v) for k, v in acc.items()]


LATENCY_FIELDS = ("latency_ms", "time_to_first_token_ms")


def latencies_from_entries(entries: list[LedgerEntry], field: str = "latency_ms") -> list[int]:
    if field not in LATENCY_FIELDS:
        raise ValueError(f"unknown latency field: {field}")
    return sorted(
        _i((e.body.get("usage") or {}).get(field))
        for e in entries
        if e.event_type == "usage.settled" and (e.body.get("usage") or {}).get(field)
    )


def usage_from_entries(entries: list[LedgerEntry], by: str) -> list[UsageRow]:
    rows: dict[str, list[int]] = {}
    for e in entries:
        b = e.body
        if e.event_type == "call.refused" and by == "principal" and b.get("principal_id"):
            rows.setdefault(str(b["principal_id"]), [0, 0, 0, 0, 0, 0])[5] += 1
            continue
        if e.event_type != "usage.settled":
            continue
        scopes = str((b.get("details") or {}).get("scopes") or "")
        keys = [x.split(":", 1)[1] for x in scopes.split(",") if x.startswith(f"{by}:")]
        if by == "model" and not keys and b.get("model"):
            keys = [str(b["model"])]
        if not keys:
            continue
        u = b.get("usage") or {}
        tok = u.get("usage") or {}
        r = rows.setdefault(keys[0], [0, 0, 0, 0, 0, 0])
        r[0] += 1
        r[1] += _i(tok.get("input_tokens"))
        r[2] += _i(tok.get("output_tokens"))
        r[3] += _i(u.get("cost"))
        if u.get("source") != "PROVIDER_REPORTED":
            r[4] += _i(u.get("cost"))
    return [(k, *v) for k, v in rows.items()]  # type: ignore[misc]


class AdminQueries(Protocol):
    def principals(self) -> list[PrincipalRecord]: ...
    def keys(self, principal_id: str | None = None) -> list[KeyRecord]: ...
    def register_entries(self) -> list[RegisterEntry]: ...
    def budget_definitions(self) -> list[BudgetDefinition]: ...
    def rate_limit_definitions(self) -> list[RateLimitDefinition]: ...
    def approvals(self, state: str | None, limit: int) -> list[ApprovalTicket]: ...
    def alerts(self, limit: int) -> list[Alert]: ...
    def registry(self, kind: str) -> list[dict[str, Any]]: ...
    def events(self, since: datetime | None, until: datetime | None) -> list[LedgerEntry]: ...
    def events_page(
        self,
        since: datetime | None,
        until: datetime | None,
        types: set[str] | None,
        decision: str | None,
        principal_id: str | None,
        register_id: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[LedgerEntry]]: ...
    def coverage_counts(
        self, since: datetime | None, until: datetime | None, types: set[str], exclude: set[str]
    ) -> list[CoverageRow]: ...
    def partition_stats(
        self, since: datetime | None, until: datetime | None
    ) -> list[PartitionRow]: ...
    def overview_rows(
        self, since: datetime | None, until: datetime | None
    ) -> list[OverviewRow]: ...
    def latency_percentiles(
        self, since: datetime | None, until: datetime | None, field: str = "latency_ms"
    ) -> tuple[float | None, float | None]: ...
    def usage_by(
        self, by: str, since: datetime | None, until: datetime | None
    ) -> list[UsageRow]: ...


def team_names(queries: AdminQueries) -> list[str]:
    """Every team name in use: people's teams and the team/project on keys. One spelling
    per name (first seen wins), so "Payments" and "payments" are one team."""
    seen: dict[str, str] = {}
    for p in queries.principals():
        if p.team_id and p.team_id.strip():
            seen.setdefault(p.team_id.strip().lower(), p.team_id.strip())
    for k in queries.keys():
        if k.project_id and k.project_id.strip():
            seen.setdefault(k.project_id.strip().lower(), k.project_id.strip())
    return sorted(seen.values(), key=str.lower)


def same_team(queries: AdminQueries, name: str | None) -> str | None:
    """The existing spelling of a team name typed in any case; new names stay as typed."""
    n = (name or "").strip()
    if not n:
        return None
    return next((t for t in team_names(queries) if t.lower() == n.lower()), n)


class PgAdminQueries:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def principals(self) -> list[PrincipalRecord]:
        from captain.db import schema as s

        with self._e.connect() as c:
            return [
                PrincipalRecord(
                    r.id,
                    r.type,
                    r.org_id,
                    r.display_name,
                    r.team_id,
                    r.idp_subject,
                    frozenset(r.roles or ()),
                    r.active,
                    r.job_title,
                )
                for r in c.execute(select(s.principals).order_by(s.principals.c.id))
            ]

    def keys(self, principal_id: str | None = None) -> list[KeyRecord]:
        from captain.db import schema as s
        from captain.db.stores import PgIdentityStore

        q = select(s.virtual_keys).order_by(s.virtual_keys.c.created_at.desc())
        if principal_id:
            q = q.where(s.virtual_keys.c.principal_id == principal_id)
        with self._e.connect() as c:
            return [PgIdentityStore._key(r) for r in c.execute(q)]  # noqa: SLF001

    def register_entries(self) -> list[RegisterEntry]:
        from captain.db import schema as s

        with self._e.connect() as c:
            rows = c.execute(select(s.register_entries.c.doc).order_by(s.register_entries.c.id))
            return [RegisterEntry.model_validate(r.doc) for r in rows]

    def rate_limit_definitions(self) -> list[RateLimitDefinition]:
        from captain.db import schema as s

        with self._e.connect() as c:
            rows = c.execute(select(s.rate_limits).order_by(s.rate_limits.c.scope))
            return [
                RateLimitDefinition(r.scope, r.requests_per_minute, r.tokens_per_minute)
                for r in rows
            ]

    def budget_definitions(self) -> list[BudgetDefinition]:
        from captain.db import schema as s

        with self._e.connect() as c:
            return [
                BudgetDefinition(
                    r.scope,
                    Period(r.period),
                    r.limit_micros,
                    r.hard,
                    tuple(float(t) for t in r.alert_thresholds),
                )
                for r in c.execute(
                    select(s.budget_definitions).order_by(s.budget_definitions.c.scope)
                )
            ]

    def approvals(self, state: str | None, limit: int) -> list[ApprovalTicket]:
        from captain.db import schema as s

        t = s.approval_tickets
        q = select(t.c.doc).order_by(t.c.updated_at.desc()).limit(limit)
        if state:
            q = q.where(t.c.state == state)
        with self._e.connect() as c:
            return [ApprovalTicket.model_validate(r.doc) for r in c.execute(q)]

    def alerts(self, limit: int) -> list[Alert]:
        from captain.core.alerts import Severity
        from captain.db import schema as s

        a = s.alerts
        with self._e.connect() as c:
            rows = c.execute(select(a).order_by(a.c.created_at.desc()).limit(limit))
            return [
                Alert(
                    r.doc["kind"],
                    Severity(r.doc["severity"]),
                    r.doc["subject"],
                    r.doc["details"],
                    r.doc["register_id"],
                    tuple(r.doc["recipients"]),
                    UUID(r.id),
                    r.created_at,
                    r.delivered,
                    r.attempts,
                )
                for r in rows
            ]

    def registry(self, kind: str) -> list[dict[str, Any]]:
        from captain.db import schema as s

        r = s.registry_items
        with self._e.connect() as c:
            rows = c.execute(select(r.c.doc).where(r.c.kind == kind).order_by(r.c.key))
            return [dict(row.doc) for row in rows]

    @staticmethod
    def _with_anchors() -> Any:
        """The ledger joined to its anchor (ranges are disjoint, so at most one per entry)."""
        from sqlalchemy import and_

        from captain.db import schema as s

        L, A = s.evidence_ledger, s.ledger_anchor_ranges  # noqa: N806
        return L.outerjoin(
            A, and_(A.c.partition == L.c.partition, L.c.seq.between(A.c.first_seq, A.c.last_seq))
        )

    def events(self, since: datetime | None, until: datetime | None) -> list[LedgerEntry]:
        from captain.db import schema as s
        from captain.db.stores import PgLedgerStore

        L, A = s.evidence_ledger, s.ledger_anchor_ranges  # noqa: N806
        q = select(L, A.c.anchor_id).select_from(self._with_anchors()).order_by(L.c.recorded_at)
        if since:
            q = q.where(L.c.recorded_at >= since)
        if until:
            q = q.where(L.c.recorded_at < until)
        with self._e.connect() as c:
            return [PgLedgerStore._entry(r, r.anchor_id) for r in c.execute(q)]  # noqa: SLF001

    @staticmethod
    def _time_conds(since: datetime | None, until: datetime | None) -> list[Any]:
        from captain.db import schema as s

        L = s.evidence_ledger  # noqa: N806
        conds: list[Any] = []
        if since:
            conds.append(L.c.recorded_at >= since)
        if until:
            conds.append(L.c.recorded_at < until)
        return conds

    def events_page(
        self,
        since: datetime | None,
        until: datetime | None,
        types: set[str] | None,
        decision: str | None,
        principal_id: str | None,
        register_id: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[LedgerEntry]]:
        from captain.db import schema as s
        from captain.db.stores import PgLedgerStore

        L = s.evidence_ledger  # noqa: N806
        conds = self._time_conds(since, until)
        if types is not None:
            conds.append(L.c.event_type.in_(sorted(types)))
        if decision is not None:
            conds.append(L.c.body["decision"].astext == decision)
        if principal_id is not None:
            conds.append(L.c.body["principal_id"].astext == principal_id)
        if register_id is not None:
            conds.append(L.c.partition == register_id)
        A = s.ledger_anchor_ranges  # noqa: N806
        page = (
            select(L, A.c.anchor_id)
            .select_from(self._with_anchors())
            .where(*conds)
            .order_by(L.c.recorded_at.desc(), L.c.partition, L.c.seq.desc())
            .limit(limit)
            .offset(offset)
        )
        with self._e.connect() as c:
            total = c.execute(select(func.count()).select_from(L).where(*conds)).scalar_one()
            return int(total), [
                PgLedgerStore._entry(r, r.anchor_id)  # noqa: SLF001
                for r in c.execute(page)
            ]

    def coverage_counts(
        self, since: datetime | None, until: datetime | None, types: set[str], exclude: set[str]
    ) -> list[CoverageRow]:
        from captain.db import schema as s

        L = s.evidence_ledger  # noqa: N806
        cov = func.coalesce(L.c.body["coverage"].astext, "GOVERNED")
        chan = L.c.body["channel"].astext
        who = L.c.body["principal_id"].astext
        q = (
            select(cov, chan, who, func.count())
            .where(*self._time_conds(since, until), L.c.event_type.in_(sorted(types)))
            .where(L.c.partition.notin_(sorted(exclude)))
            .group_by(cov, chan, who)
        )
        with self._e.connect() as c:
            return [(str(a), str(b), p, int(n)) for a, b, p, n in c.execute(q)]

    def partition_stats(self, since: datetime | None, until: datetime | None) -> list[PartitionRow]:
        from captain.db import schema as s

        L = s.evidence_ledger  # noqa: N806
        q = (
            select(L.c.partition, L.c.event_type, func.count(), func.max(L.c.recorded_at))
            .where(*self._time_conds(since, until))
            .group_by(L.c.partition, L.c.event_type)
        )
        with self._e.connect() as c:
            return [(str(p), str(t), int(n), last) for p, t, n, last in c.execute(q)]

    def overview_rows(self, since: datetime | None, until: datetime | None) -> list[OverviewRow]:
        from sqlalchemy import BigInteger, Date, cast, literal_column

        from captain.db import schema as s

        L = s.evidence_ledger  # noqa: N806
        b = L.c.body

        def num(expr: Any) -> Any:
            return func.coalesce(cast(func.nullif(expr, ""), BigInteger), 0)

        day = cast(func.timezone("UTC", L.c.recorded_at), Date)
        dims = [
            day,
            L.c.event_type,
            b["decision"].astext,
            b["reason_code"].astext,
            b["coverage"].astext,
            b["details"]["risk_level"].astext,
            b["model"].astext,
            b["usage"]["source"].astext,
        ]
        q = (
            select(
                *dims,
                func.count(),
                func.sum(num(b["usage"]["cost"].astext)),
                func.sum(num(b["usage"]["usage"]["input_tokens"].astext)),
                func.sum(num(b["usage"]["usage"]["output_tokens"].astext)),
            )
            .where(*self._time_conds(since, until), L.c.event_type.in_(sorted(OVERVIEW_TYPES)))
            .group_by(*[literal_column(str(i + 1)) for i in range(len(dims))])
        )
        with self._e.connect() as c:
            return [
                (r[0].isoformat(), *[None if x is None else str(x) for x in r[1:8]],
                 int(r[8]), int(r[9] or 0), int(r[10] or 0), int(r[11] or 0))
                for r in c.execute(q)
            ]  # fmt: skip

    def latency_percentiles(
        self, since: datetime | None, until: datetime | None, field: str = "latency_ms"
    ) -> tuple[float | None, float | None]:
        from sqlalchemy import Numeric, cast

        from captain.db import schema as s

        if field not in LATENCY_FIELDS:
            raise ValueError(f"unknown latency field: {field}")
        L = s.evidence_ledger  # noqa: N806
        lat = cast(L.c.body["usage"][field].astext, Numeric)
        q = (
            select(
                func.percentile_cont(0.5).within_group(lat),
                func.percentile_cont(0.95).within_group(lat),
            )
            .where(*self._time_conds(since, until), L.c.event_type == "usage.settled")
            .where(lat > 0)
        )
        with self._e.connect() as c:
            p50, p95 = c.execute(q).one()
        return (None if p50 is None else float(p50), None if p95 is None else float(p95))

    def usage_by(self, by: str, since: datetime | None, until: datetime | None) -> list[UsageRow]:
        from sqlalchemy import text

        from captain.db import schema as s

        L = s.evidence_ledger  # noqa: N806
        if by not in {"principal", "project", "app", "cost_centre", "team", "model"}:
            raise ValueError(f"unknown dimension: {by}")
        prefix = f"{by}:"
        sql = text(
            """
            select k, count(*), sum(tin), sum(tout), sum(cost),
                   sum(case when src is distinct from 'PROVIDER_REPORTED' then cost else 0 end), 0
            from (
              select coalesce(
                       (select substr(x, :n)
                          from unnest(string_to_array(body->'details'->>'scopes', ','))
                               with ordinality t(x, o)
                         where x like :pat order by o limit 1),
                       case when :by = 'model' then body->>'model' end) as k,
                     coalesce(nullif(body->'usage'->>'cost',''),'0')::bigint as cost,
                     coalesce(nullif(body->'usage'->'usage'->>'input_tokens',''),'0')::bigint
                       as tin,
                     coalesce(nullif(body->'usage'->'usage'->>'output_tokens',''),'0')::bigint
                       as tout,
                     body->'usage'->>'source' as src
              from evidence_ledger
              where event_type = 'usage.settled'
                and (cast(:since as timestamptz) is null or recorded_at >= :since)
                and (cast(:until as timestamptz) is null or recorded_at < :until)
            ) s where k is not null group by k
            """
        )
        out: dict[str, list[int]] = {}
        with self._e.connect() as c:
            for r in c.execute(sql, {"n": len(prefix) + 1, "pat": prefix + "%", "by": by,
                                     "since": since, "until": until}):  # fmt: skip
                out[str(r[0])] = [
                    int(r[1]),
                    int(r[2] or 0),
                    int(r[3] or 0),
                    int(r[4] or 0),
                    int(r[5] or 0),
                    0,
                ]
            if by == "principal":
                who = L.c.body[
                    "principal_id"
                ].astext  # one expression: same bind in SELECT/GROUP BY
                q = (
                    select(who, func.count())
                    .where(*self._time_conds(since, until), L.c.event_type == "call.refused")
                    .where(who.is_not(None))
                    .group_by(who)
                )
                for name, n in c.execute(q):
                    out.setdefault(str(name), [0, 0, 0, 0, 0, 0])[5] += int(n)
        return [(k, *v) for k, v in out.items()]  # type: ignore[misc]


class InMemoryAdminQueries:
    """Over the in-memory stores used by tests and the harness."""

    def __init__(self, infra: Any) -> None:
        self._i = infra

    def principals(self) -> list[PrincipalRecord]:
        s: InMemoryIdentityStore = self._i.identity_store
        return sorted(s._principals.values(), key=lambda p: p.id)  # noqa: SLF001

    def keys(self, principal_id: str | None = None) -> list[KeyRecord]:
        s: InMemoryIdentityStore = self._i.identity_store
        ks = [k for k in s._keys.values() if principal_id in (None, k.principal_id)]  # noqa: SLF001
        return sorted(ks, key=lambda k: k.created_at, reverse=True)

    def register_entries(self) -> list[RegisterEntry]:
        s: InMemoryRegisterStore = self._i.register_store
        return sorted(s._entries.values(), key=lambda e: e.id)  # noqa: SLF001

    def rate_limit_definitions(self) -> list[RateLimitDefinition]:
        c: InMemoryBudgetCatalog = self._i.budget_catalog
        return sorted(c.rates.values(), key=lambda r: r.scope)

    def budget_definitions(self) -> list[BudgetDefinition]:
        c: InMemoryBudgetCatalog = self._i.budget_catalog
        return sorted((d for ds in c.budgets.values() for d in ds), key=lambda d: d.scope)

    def approvals(self, state: str | None, limit: int) -> list[ApprovalTicket]:
        s: InMemoryApprovalStore = self._i.approval_store
        ts = [t for t in s._t.values() if state in (None, t.state.value)]  # noqa: SLF001
        return sorted(ts, key=lambda t: t.created_at, reverse=True)[:limit]

    def alerts(self, limit: int) -> list[Alert]:
        s: InMemoryAlertStore = self._i.alert_store
        return sorted(s.items.values(), key=lambda a: a.created_at, reverse=True)[:limit]

    def registry(self, kind: str) -> list[dict[str, Any]]:
        from captain.db.registry_store import _doc

        reg = self._i.registry
        if reg is None:
            return []
        store: InMemoryRegistryStore = reg._s  # noqa: SLF001
        short = {"provider": "p", "model": "m", "route": "r", "server": "s", "tool": "t"}[kind]
        return [_doc(v) for _, v in sorted(store._d[short].items())]  # noqa: SLF001

    def events(self, since: datetime | None, until: datetime | None) -> list[LedgerEntry]:
        rows = self._i.ledger_store.entries()
        return [
            e
            for e in rows
            if (since is None or e.recorded_at >= since)
            and (until is None or e.recorded_at < until)
        ]

    def events_page(
        self,
        since: datetime | None,
        until: datetime | None,
        types: set[str] | None,
        decision: str | None,
        principal_id: str | None,
        register_id: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[LedgerEntry]]:
        return page_from_entries(
            self.events(since, until), types, decision, principal_id, register_id, limit, offset
        )

    def coverage_counts(
        self, since: datetime | None, until: datetime | None, types: set[str], exclude: set[str]
    ) -> list[CoverageRow]:
        return coverage_from_entries(self.events(since, until), types, exclude)

    def partition_stats(self, since: datetime | None, until: datetime | None) -> list[PartitionRow]:
        return partitions_from_entries(self.events(since, until))

    def overview_rows(self, since: datetime | None, until: datetime | None) -> list[OverviewRow]:
        return overview_from_entries(self.events(since, until))

    def latency_percentiles(
        self, since: datetime | None, until: datetime | None, field: str = "latency_ms"
    ) -> tuple[float | None, float | None]:
        v = latencies_from_entries(self.events(since, until), field)
        return percentile(v, 0.5), percentile(v, 0.95)

    def usage_by(self, by: str, since: datetime | None, until: datetime | None) -> list[UsageRow]:
        return usage_from_entries(self.events(since, until), by)


class ResolvingQueries:
    """Erasure (ADR-005): the ledger holds pseudonyms for people. Every dashboard read goes
    through this wrapper, which shows the person again, or "erased subject" once their key
    is deleted, and turns a person filter into their pseudonym. Rows that only differ by
    an erased pseudonym are merged, so erased people appear once, as one subject. Use
    `raw` where the stored, signed bodies are needed (evidence export)."""

    def __init__(self, inner: AdminQueries, pseudonymiser: Pseudonymiser) -> None:
        self.raw = inner
        self._pz = pseudonymiser

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)

    def _entry(self, e: LedgerEntry) -> LedgerEntry:
        return replace(e, body=self._pz.resolve(e.body))

    def events(self, since: datetime | None, until: datetime | None) -> list[LedgerEntry]:
        return [self._entry(e) for e in self.raw.events(since, until)]

    def events_page(
        self,
        since: datetime | None,
        until: datetime | None,
        types: set[str] | None,
        decision: str | None,
        principal_id: str | None,
        register_id: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[LedgerEntry]]:
        pid = self._pz.lookup(principal_id) if principal_id else None
        total, rows = self.raw.events_page(
            since, until, types, decision, pid, register_id, limit, offset
        )
        return total, [self._entry(e) for e in rows]

    def coverage_counts(
        self, since: datetime | None, until: datetime | None, types: set[str], exclude: set[str]
    ) -> list[CoverageRow]:
        merged: dict[tuple[str, str, str | None], int] = {}
        for cov, ch, p, n in self.raw.coverage_counts(since, until, types, exclude):
            k = (cov, ch, self._pz.display(p))
            merged[k] = merged.get(k, 0) + n
        return [(c, ch, p, n) for (c, ch, p), n in merged.items()]

    def usage_by(self, by: str, since: datetime | None, until: datetime | None) -> list[UsageRow]:
        rows = self.raw.usage_by(by, since, until)
        if by != "principal":
            return rows
        merged: dict[str, list[int]] = {}
        for key, *nums in rows:
            acc = merged.setdefault(str(self._pz.display(key)), [0] * len(nums))
            for i, v in enumerate(nums):
                acc[i] += v
        return [(k, *v) for k, v in merged.items()]  # type: ignore[misc]
