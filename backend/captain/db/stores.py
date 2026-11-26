"""PostgreSQL implementations of the Core store protocols.

Concurrency notes:
- Budget reservations lock all affected bucket rows with SELECT … FOR UPDATE in a
  stable (sorted) order, so concurrent reservations serialise without deadlocks and
  a hard cap can never be overshot.
- Settle deletes the reservation row with RETURNING, so a reservation settles once.
- Approval state changes use UPDATE … WHERE state = :expected (compare-and-set).
- The evidence ledger is INSERT-only; UPDATE/DELETE/TRUNCATE are rejected by a trigger.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from captain.core.alerts import Alert, Severity
from captain.core.budget.model import (
    BucketState,
    BudgetDefinition,
    BudgetLevel,
    Period,
    RateLimitDefinition,
)
from captain.core.budget.store import ReservationRejected, StoredReservation, UnknownReservation
from captain.core.context.service import Application, Project
from captain.core.identity.killswitch import KillScope, KillSwitchEntry
from captain.core.identity.model import KeyRecord, KeyStatus, PrincipalRecord
from captain.core.identity.oidc import WorkloadBinding
from captain.core.ledger.chain import Anchor, LedgerEntry
from captain.core.models import ApprovalState, ApprovalTicket, Environment, RegisterEntry
from captain.core.secrets_store import StoredSecret
from captain.core.usage.pricing import ModelPrice, PriceCatalog
from captain.db import schema as s

# =================================================================== identity


class PgIdentityStore:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    @staticmethod
    def _key(r: Any) -> KeyRecord:
        return KeyRecord(
            key_id=r.key_id,
            secret_hash=r.secret_hash,
            principal_id=r.principal_id,
            register_id=r.register_id,
            project_id=r.project_id,
            application_id=r.application_id,
            environment=Environment(r.environment),
            created_at=r.created_at,
            expires_at=r.expires_at,
            status=KeyStatus(r.status),
            revoked_at=r.revoked_at,
            revoked_reason=r.revoked_reason,
            rotated_from=r.rotated_from,
            last_used_at=r.last_used_at,
            allowed_cidrs=tuple(r.allowed_cidrs or ()),
            label=r.label,
        )

    def get_key(self, key_id: str) -> KeyRecord | None:
        with self._e.connect() as c:
            r = c.execute(select(s.virtual_keys).where(s.virtual_keys.c.key_id == key_id)).first()
        return self._key(r) if r else None

    def put_key(self, record: KeyRecord) -> None:
        row = {
            "key_id": record.key_id,
            "secret_hash": record.secret_hash,
            "principal_id": record.principal_id,
            "register_id": record.register_id,
            "project_id": record.project_id,
            "application_id": record.application_id,
            "environment": record.environment.value,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "status": record.status.value,
            "revoked_at": record.revoked_at,
            "revoked_reason": record.revoked_reason,
            "rotated_from": record.rotated_from,
            "last_used_at": record.last_used_at,
            "allowed_cidrs": list(record.allowed_cidrs),
            "label": record.label,
        }
        stmt = pg_insert(s.virtual_keys).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["key_id"], set_={k: stmt.excluded[k] for k in row if k != "key_id"}
        )
        with self._e.begin() as c:
            c.execute(stmt)

    def keys_for_principal(self, principal_id: str) -> list[KeyRecord]:
        with self._e.connect() as c:
            rows = c.execute(
                select(s.virtual_keys).where(s.virtual_keys.c.principal_id == principal_id)
            )
            return [self._key(r) for r in rows]

    def get_principal(self, principal_id: str) -> PrincipalRecord | None:
        with self._e.connect() as c:
            r = c.execute(select(s.principals).where(s.principals.c.id == principal_id)).first()
        if r is None:
            return None
        return PrincipalRecord(
            id=r.id,
            type=r.type,
            org_id=r.org_id,
            display_name=r.display_name,
            team_id=r.team_id,
            idp_subject=r.idp_subject,
            roles=frozenset(r.roles or ()),
            active=r.active,
            job_title=r.job_title,
        )

    def put_principal(self, record: PrincipalRecord) -> None:
        row = {
            "id": record.id,
            "type": record.type,
            "org_id": record.org_id,
            "team_id": record.team_id,
            "display_name": record.display_name,
            "idp_subject": record.idp_subject,
            "roles": sorted(record.roles),
            "active": record.active,
            "job_title": record.job_title,
        }
        stmt = pg_insert(s.principals).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"], set_={k: stmt.excluded[k] for k in row if k != "id"}
        )
        with self._e.begin() as c:
            c.execute(stmt)

    def touch_key(self, key_id: str, at: datetime) -> None:
        with self._e.begin() as c:
            c.execute(
                update(s.virtual_keys)
                .where(s.virtual_keys.c.key_id == key_id)
                .values(last_used_at=at)
            )


class PgKillSwitchStore:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def activate(self, entry: KillSwitchEntry) -> None:
        row = {
            "scope": entry.scope.value,
            "target": entry.target,
            "reason": entry.reason,
            "actor_id": entry.actor_id,
            "activated_at": entry.activated_at,
        }
        stmt = pg_insert(s.kill_switches).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["scope", "target"],
            set_={
                "reason": stmt.excluded.reason,
                "actor_id": stmt.excluded.actor_id,
                "activated_at": stmt.excluded.activated_at,
            },
        )
        with self._e.begin() as c:
            c.execute(stmt)

    def deactivate(self, scope: KillScope, target: str) -> None:
        with self._e.begin() as c:
            c.execute(
                delete(s.kill_switches).where(
                    and_(s.kill_switches.c.scope == scope.value, s.kill_switches.c.target == target)
                )
            )

    def active(self) -> list[KillSwitchEntry]:
        with self._e.connect() as c:
            return [
                KillSwitchEntry(KillScope(r.scope), r.target, r.reason, r.actor_id, r.activated_at)
                for r in c.execute(select(s.kill_switches))
            ]


# ==================================================================== context


class PgRegisterStore:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def get(self, register_id: str) -> RegisterEntry | None:
        with self._e.connect() as c:
            r = c.execute(
                select(s.register_entries.c.doc).where(s.register_entries.c.id == register_id)
            ).first()
        return RegisterEntry.model_validate(r.doc) if r else None

    def put(self, entry: RegisterEntry) -> None:
        doc = entry.model_dump(mode="json")
        stmt = pg_insert(s.register_entries).values(
            id=entry.id, version=entry.version, doc=doc, updated_at=datetime.now(UTC)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "version": stmt.excluded.version,
                "doc": stmt.excluded.doc,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        with self._e.begin() as c:
            c.execute(stmt)


class PgContextStore:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def project(self, project_id: str) -> Project | None:
        with self._e.connect() as c:
            r = c.execute(select(s.projects).where(s.projects.c.id == project_id)).first()
        return Project(r.id, r.org_id, r.name, r.team_id, r.active) if r else None

    def application(self, application_id: str) -> Application | None:
        with self._e.connect() as c:
            r = c.execute(
                select(s.applications).where(s.applications.c.id == application_id)
            ).first()
        if r is None:
            return None
        return Application(
            r.id,
            r.project_id,
            r.register_id,
            r.name,
            frozenset(Environment(e) for e in r.environments),
            r.active,
        )

    def put_project(self, p: Project) -> None:
        row = {
            "id": p.id,
            "org_id": p.org_id,
            "name": p.name,
            "team_id": p.team_id,
            "active": p.active,
        }
        stmt = pg_insert(s.projects).values(**row)
        with self._e.begin() as c:
            c.execute(
                stmt.on_conflict_do_update(
                    index_elements=["id"], set_={k: stmt.excluded[k] for k in row if k != "id"}
                )
            )

    def put_application(self, a: Application) -> None:
        row = {
            "id": a.id,
            "project_id": a.project_id,
            "register_id": a.register_id,
            "name": a.name,
            "environments": sorted(e.value for e in a.environments),
            "active": a.active,
        }
        stmt = pg_insert(s.applications).values(**row)
        with self._e.begin() as c:
            c.execute(
                stmt.on_conflict_do_update(
                    index_elements=["id"], set_={k: stmt.excluded[k] for k in row if k != "id"}
                )
            )


# ===================================================================== budget


class PgBudgetCatalog:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def add(self, d: BudgetDefinition) -> None:
        row = {
            "scope": d.scope,
            "period": d.period.value,
            "limit_micros": d.limit,
            "hard": d.hard,
            "alert_thresholds": [str(t) for t in d.alert_thresholds],
        }
        stmt = pg_insert(s.budget_definitions).values(**row)
        with self._e.begin() as c:
            c.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_budget_definitions_scope",
                    set_={
                        "limit_micros": stmt.excluded.limit_micros,
                        "hard": stmt.excluded.hard,
                        "alert_thresholds": stmt.excluded.alert_thresholds,
                    },
                )
            )

    def add_rate(self, r: RateLimitDefinition) -> None:
        row = {
            "scope": r.scope,
            "requests_per_minute": r.requests_per_minute,
            "tokens_per_minute": r.tokens_per_minute,
        }
        stmt = pg_insert(s.rate_limits).values(**row)
        with self._e.begin() as c:
            c.execute(
                stmt.on_conflict_do_update(
                    index_elements=["scope"],
                    set_={
                        "requests_per_minute": stmt.excluded.requests_per_minute,
                        "tokens_per_minute": stmt.excluded.tokens_per_minute,
                    },
                )
            )

    def remove_rate(self, scope: str) -> bool:
        with self._e.begin() as c:
            res = c.execute(delete(s.rate_limits).where(s.rate_limits.c.scope == scope))
            return bool(res.rowcount)

    def budgets_for(self, scopes: tuple[str, ...]) -> list[BudgetDefinition]:
        with self._e.connect() as c:
            rows = c.execute(
                select(s.budget_definitions).where(s.budget_definitions.c.scope.in_(scopes))
            )
            return [
                BudgetDefinition(
                    r.scope,
                    Period(r.period),
                    r.limit_micros,
                    r.hard,
                    tuple(float(t) for t in r.alert_thresholds),
                )
                for r in rows
            ]

    def rate_limits_for(self, scopes: tuple[str, ...]) -> list[RateLimitDefinition]:
        with self._e.connect() as c:
            rows = c.execute(select(s.rate_limits).where(s.rate_limits.c.scope.in_(scopes)))
            return [
                RateLimitDefinition(r.scope, r.requests_per_minute, r.tokens_per_minute)
                for r in rows
            ]


class PgBudgetStore:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def reserve(
        self, reservation_id: str, levels: list[BudgetLevel], amount: int, ttl_s: int
    ) -> None:
        if not levels:
            return
        b = s.budget_buckets
        with self._e.begin() as c:
            for lv in levels:
                stmt = pg_insert(b).values(bucket=lv.bucket, limit_micros=lv.limit)
                c.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["bucket"], set_={"limit_micros": lv.limit}
                    )
                )
            names = sorted(lv.bucket for lv in levels)  # stable lock order → no deadlocks
            rows = {
                r.bucket: r
                for r in c.execute(
                    select(b).where(b.c.bucket.in_(names)).order_by(b.c.bucket).with_for_update()
                )
            }
            for lv in levels:
                r = rows[lv.bucket]
                if (
                    lv.hard
                    and r.spent_micros + r.reserved_micros + amount > lv.limit + r.extra_micros
                ):
                    raise ReservationRejected(lv.bucket)  # transaction rolls back
            c.execute(
                update(b)
                .where(b.c.bucket.in_(names))
                .values(reserved_micros=b.c.reserved_micros + amount)
            )
            c.execute(
                insert(s.budget_reservations).values(
                    reservation_id=reservation_id,
                    amount_micros=amount,
                    buckets=names,
                    expires_at=datetime.now(UTC) + timedelta(seconds=ttl_s),
                )
            )

    def settle(self, reservation_id: str, charged: int) -> StoredReservation:
        r_t, b = s.budget_reservations, s.budget_buckets
        with self._e.begin() as c:
            r = c.execute(
                delete(r_t)
                .where(r_t.c.reservation_id == reservation_id)
                .returning(r_t.c.amount_micros, r_t.c.buckets)
            ).first()
            if r is None:
                raise UnknownReservation(reservation_id)
            c.execute(
                update(b)
                .where(b.c.bucket.in_(r.buckets))
                .values(
                    reserved_micros=b.c.reserved_micros - r.amount_micros,
                    spent_micros=b.c.spent_micros + charged,
                )
            )
        return StoredReservation(reservation_id, r.amount_micros, tuple(r.buckets))

    def state(self, bucket: str) -> BucketState:
        with self._e.connect() as c:
            r = c.execute(
                select(s.budget_buckets).where(s.budget_buckets.c.bucket == bucket)
            ).first()
        if r is None:
            return BucketState(0, 0, 0)
        return BucketState(r.limit_micros + r.extra_micros, r.spent_micros, r.reserved_micros)

    def mark_alerted(self, bucket: str, threshold: float) -> bool:
        """Atomic across instances: only the INSERT that creates the row returns True."""
        stmt = (
            pg_insert(s.budget_alert_marks)
            .values(bucket=bucket, threshold=str(threshold), at=datetime.now(UTC))
            .on_conflict_do_nothing()
            .returning(s.budget_alert_marks.c.bucket)  # a row comes back only if inserted
        )
        with self._e.begin() as c:
            return c.execute(stmt).first() is not None

    def add_extra(self, bucket: str, amount: int) -> None:
        """Budget override for this period, stored with the counters so every instance
        sees it and it survives restarts."""
        b = s.budget_buckets
        with self._e.begin() as c:
            stmt = pg_insert(b).values(bucket=bucket, limit_micros=0, extra_micros=amount)
            c.execute(
                stmt.on_conflict_do_update(
                    index_elements=["bucket"], set_={"extra_micros": b.c.extra_micros + amount}
                )
            )

    def add_spend(self, bucket: str, amount: int) -> None:
        b = s.budget_buckets
        with self._e.begin() as c:
            stmt = pg_insert(b).values(bucket=bucket, limit_micros=0, spent_micros=amount)
            c.execute(
                stmt.on_conflict_do_update(
                    index_elements=["bucket"], set_={"spent_micros": b.c.spent_micros + amount}
                )
            )

    def sweep(self) -> int:
        with self._e.connect() as c:
            ids = [
                r.reservation_id
                for r in c.execute(
                    select(s.budget_reservations.c.reservation_id).where(
                        s.budget_reservations.c.expires_at <= func.now()
                    )
                )
            ]
        n = 0
        for rid in ids:
            try:
                self.settle(rid, 0)
                n += 1
            except UnknownReservation:
                pass
        return n

    def hit_rate(
        self, bucket: str, requests: int, tokens: int, rpm: int | None, tpm: int | None
    ) -> bool:
        w = s.rate_windows
        minute = int(time.time() // 60)
        with self._e.begin() as c:
            c.execute(
                pg_insert(w)
                .values(bucket=bucket, minute=minute, requests=0, tokens=0)
                .on_conflict_do_nothing()
            )
            r = c.execute(
                select(w).where(and_(w.c.bucket == bucket, w.c.minute == minute)).with_for_update()
            ).one()
            if (rpm is not None and r.requests + requests > rpm) or (
                tpm is not None and r.tokens + tokens > tpm
            ):
                return False
            c.execute(
                update(w)
                .where(and_(w.c.bucket == bucket, w.c.minute == minute))
                .values(requests=w.c.requests + requests, tokens=w.c.tokens + tokens)
            )
            c.execute(delete(w).where(w.c.minute < minute - 5))  # housekeeping
        return True


# ================================================================== approvals


class PgApprovalStore:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def get(self, ticket_id: UUID) -> ApprovalTicket | None:
        with self._e.connect() as c:
            r = c.execute(
                select(s.approval_tickets.c.doc).where(
                    s.approval_tickets.c.ticket_id == str(ticket_id)
                )
            ).first()
        return ApprovalTicket.model_validate(r.doc) if r else None

    def put(self, ticket: ApprovalTicket) -> None:
        with self._e.begin() as c:
            c.execute(
                insert(s.approval_tickets).values(
                    ticket_id=str(ticket.ticket_id),
                    state=ticket.state.value,
                    requested_by=ticket.requested_by,
                    doc=ticket.model_dump(mode="json"),
                    updated_at=datetime.now(UTC),
                )
            )

    def compare_and_set(
        self, ticket_id: UUID, expected: ApprovalState, new: ApprovalTicket
    ) -> bool:
        t = s.approval_tickets
        with self._e.begin() as c:
            res = c.execute(
                update(t)
                .where(and_(t.c.ticket_id == str(ticket_id), t.c.state == expected.value))
                .values(
                    state=new.state.value,
                    doc=new.model_dump(mode="json"),
                    updated_at=datetime.now(UTC),
                )
            )
            return res.rowcount == 1

    def pending(self, limit: int) -> list[ApprovalTicket]:
        t = s.approval_tickets
        with self._e.connect() as c:
            rows = c.execute(
                select(t.c.doc).where(t.c.state == ApprovalState.PENDING.value).limit(limit)
            )
            return [ApprovalTicket.model_validate(r.doc) for r in rows]

    def pending_for_principal(self, principal_id: str) -> list[ApprovalTicket]:
        t = s.approval_tickets
        with self._e.connect() as c:
            rows = c.execute(
                select(t.c.doc).where(
                    and_(t.c.requested_by == principal_id, t.c.state == ApprovalState.PENDING.value)
                )
            )
            return [ApprovalTicket.model_validate(r.doc) for r in rows]

    def open_for_principal(self, principal_id: str) -> list[ApprovalTicket]:
        t = s.approval_tickets
        with self._e.connect() as c:
            rows = c.execute(
                select(t.c.doc).where(
                    and_(
                        t.c.requested_by == principal_id,
                        t.c.state.in_([ApprovalState.PENDING.value, ApprovalState.APPROVED.value]),
                    )
                )
            )
            return [ApprovalTicket.model_validate(r.doc) for r in rows]


# ==================================================================== pricing


def load_price_catalog(engine: Engine) -> PriceCatalog:
    def read() -> list[ModelPrice]:
        with engine.connect() as c:
            return [_price_row(r) for r in c.execute(select(s.model_prices))]

    cat = PriceCatalog(persist=lambda p: save_price(engine, p), reload=read)
    for p in read():
        cat.add(p)
    return cat


def _price_row(r: Any) -> ModelPrice:
    return ModelPrice(
        r.model,
        r.provider,
        r.version,
        r.effective_from,
        r.input_per_m,
        r.output_per_m,
        r.cached_input_per_m,
        r.cache_write_per_m,
        r.currency,
    )


def save_price(engine: Engine, p: ModelPrice) -> None:
    """Prices are versioned and never edited in place: a new version is a new row."""
    with engine.begin() as c:
        c.execute(
            insert(s.model_prices).values(
                model=p.model,
                version=p.version,
                provider=p.provider,
                effective_from=p.effective_from,
                input_per_m=p.input_per_m,
                output_per_m=p.output_per_m,
                cached_input_per_m=p.cached_input_per_m,
                cache_write_per_m=p.cache_write_per_m,
                currency=p.currency,
            )
        )


# ===================================================================== ledger


class PgLedgerStore:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def outbox_put(
        self, event_id: str, partition: str, body: dict[str, Any], event_type: str
    ) -> None:
        with self._e.begin() as c:
            c.execute(
                insert(s.ledger_outbox).values(
                    event_id=event_id, partition=partition, event_type=event_type, body=body
                )
            )

    def outbox_take(self, limit: int) -> list[tuple[str, str, str, dict[str, Any]]]:
        o = s.ledger_outbox
        with self._e.connect() as c:
            rows = c.execute(select(o).order_by(o.c.id).limit(limit))
            return [(r.event_id, r.partition, r.event_type, r.body) for r in rows]

    def outbox_done(self, event_ids: list[str]) -> None:
        with self._e.begin() as c:
            c.execute(delete(s.ledger_outbox).where(s.ledger_outbox.c.event_id.in_(event_ids)))

    def written_event_ids(self, event_ids: list[str]) -> set[str]:
        if not event_ids:
            return set()
        L = s.evidence_ledger  # noqa: N806
        with self._e.connect() as c:
            rows = c.execute(select(L.c.event_id).where(L.c.event_id.in_(event_ids)))
            return {r.event_id for r in rows}

    def append(self, entries: list[LedgerEntry]) -> None:
        with self._e.begin() as c:
            c.execute(
                insert(s.evidence_ledger),
                [
                    {
                        "partition": e.partition,
                        "seq": e.seq,
                        "event_id": e.event_id,
                        "event_type": e.event_type,
                        "recorded_at": e.recorded_at,
                        "body": e.body,
                        "prev_hash": e.prev_hash,
                        "entry_hash": e.entry_hash,
                        "signature": e.signature,
                        "key_id": e.key_id,
                    }
                    for e in entries
                ],
            )

    @staticmethod
    def _entry(r: Any, anchor_id: str | None = None) -> LedgerEntry:
        return LedgerEntry(
            r.partition,
            r.seq,
            r.event_id,
            r.event_type,
            r.recorded_at,
            r.body,
            r.prev_hash,
            r.entry_hash,
            r.signature,
            r.key_id,
            anchor_id,
        )

    def last(self, partition: str) -> LedgerEntry | None:
        L = s.evidence_ledger  # noqa: N806
        with self._e.connect() as c:
            r = c.execute(
                select(L).where(L.c.partition == partition).order_by(L.c.seq.desc()).limit(1)
            ).first()
        return self._entry(r) if r else None

    def entries(self, partition: str | None = None) -> list[LedgerEntry]:
        L, A = s.evidence_ledger, s.ledger_anchor_ranges  # noqa: N806
        q = (
            select(L, A.c.anchor_id)
            .select_from(
                L.outerjoin(
                    A,
                    and_(
                        A.c.partition == L.c.partition, L.c.seq.between(A.c.first_seq, A.c.last_seq)
                    ),
                )
            )
            .order_by(L.c.partition, L.c.seq)
        )
        if partition is not None:
            q = q.where(L.c.partition == partition)
        with self._e.connect() as c:
            return [self._entry(r, r.anchor_id) for r in c.execute(q)]

    def unanchored(self) -> list[LedgerEntry]:
        L, A = s.evidence_ledger, s.ledger_anchor_ranges  # noqa: N806
        anchored = (
            select(A.c.partition, func.max(A.c.last_seq).label("hi"))
            .group_by(A.c.partition)
            .subquery()
        )
        q = (
            select(L)
            .select_from(L.outerjoin(anchored, anchored.c.partition == L.c.partition))
            .where(L.c.seq > func.coalesce(anchored.c.hi, 0))
            .order_by(L.c.partition, L.c.seq)
        )
        with self._e.connect() as c:
            return [self._entry(r) for r in c.execute(q)]

    def mark_anchored(self, anchor: Anchor) -> None:
        with self._e.begin() as c:
            c.execute(
                insert(s.ledger_anchor_ranges),
                [
                    {
                        "anchor_id": anchor.anchor_id,
                        "partition": p,
                        "first_seq": lo,
                        "last_seq": hi,
                        "created_at": anchor.created_at,
                    }
                    for p, (lo, hi) in anchor.ranges.items()
                ],
            )


# ========================================================== workload identity


class PgWorkloadBindings:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def put(self, b: WorkloadBinding) -> None:
        row = {
            "issuer": b.issuer,
            "subject": b.subject,
            "principal_id": b.principal_id,
            "register_id": b.register_id,
            "project_id": b.project_id,
            "application_id": b.application_id,
            "environment": b.environment.value,
        }
        stmt = pg_insert(s.workload_bindings).values(**row)
        with self._e.begin() as c:
            c.execute(
                stmt.on_conflict_do_update(
                    index_elements=["issuer", "subject"],
                    set_={k: stmt.excluded[k] for k in row if k not in ("issuer", "subject")},
                )
            )

    def get(self, issuer: str, subject: str) -> WorkloadBinding | None:
        w = s.workload_bindings
        with self._e.connect() as c:
            r = c.execute(
                select(w).where(and_(w.c.issuer == issuer, w.c.subject == subject))
            ).first()
        if r is None:
            return None
        return WorkloadBinding(
            r.issuer,
            r.subject,
            r.principal_id,
            r.register_id,
            r.project_id,
            r.application_id,
            Environment(r.environment),
        )


# ===================================================================== alerts


class PgAlertStore:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    @staticmethod
    def _doc(a: Alert) -> dict[str, Any]:
        return {
            "kind": a.kind, "severity": a.severity.value, "subject": a.subject,
            "details": a.details, "register_id": a.register_id, "recipients": list(a.recipients),
        }  # fmt: skip

    def put(self, a: Alert) -> None:
        with self._e.begin() as c:
            c.execute(insert(s.alerts).values(
                id=str(a.id), kind=a.kind, severity=a.severity.value, created_at=a.created_at,
                delivered=a.delivered, attempts=a.attempts, doc=self._doc(a),
            ))  # fmt: skip

    def undelivered(self, limit: int) -> list[Alert]:
        al = s.alerts
        with self._e.connect() as c:
            rows = c.execute(
                select(al).where(al.c.delivered.is_(False)).order_by(al.c.created_at).limit(limit)
            )
            return [
                Alert(
                    kind=r.doc["kind"],
                    severity=Severity(r.doc["severity"]),
                    subject=r.doc["subject"],
                    details=r.doc["details"],
                    register_id=r.doc["register_id"],
                    recipients=tuple(r.doc["recipients"]),
                    id=UUID(r.id),
                    created_at=r.created_at,
                    delivered=r.delivered,
                    attempts=r.attempts,
                )  # fmt: skip
                for r in rows
            ]

    def mark(self, alert_id: UUID, delivered: bool, attempts: int) -> None:
        with self._e.begin() as c:
            c.execute(
                update(s.alerts)
                .where(s.alerts.c.id == str(alert_id))
                .values(delivered=delivered, attempts=attempts)
            )


class PgSubjectStore:
    """Erasure keys (ADR-005). `get_or_create` is race-safe: two writers creating the same
    subject at once both end up with the one row that won.

    No in-process cache: erasure happens in one process (the Admin API) while others (the
    gateway, MCP proxy, Core) keep writing evidence, so every lookup reads the table. A cache
    once let a gateway keep writing a re-created person under their ERASED pseudonym."""

    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def get_or_create(self, subject: str, make: Callable[[], tuple[bytes, str]]) -> str:
        t = s.subject_keys
        with self._e.begin() as c:
            row = c.execute(select(t.c.pseudonym).where(t.c.subject == subject)).first()
            if row is None:
                key, pseudo = make()
                c.execute(
                    pg_insert(t)
                    .values(
                        subject=subject, key=key, pseudonym=pseudo, created_at=datetime.now(UTC)
                    )
                    .on_conflict_do_nothing(index_elements=[t.c.subject])
                )
                row = c.execute(select(t.c.pseudonym).where(t.c.subject == subject)).first()
        assert row is not None
        return str(row[0])

    def key(self, subject: str) -> bytes | None:
        t = s.subject_keys
        with self._e.connect() as c:
            r = c.execute(select(t.c.key).where(t.c.subject == subject)).first()
        return bytes(r[0]) if r else None

    def subject_for(self, pseudonym: str) -> str | None:
        t = s.subject_keys
        with self._e.connect() as c:
            r = c.execute(select(t.c.subject).where(t.c.pseudonym == pseudonym)).first()
        return str(r[0]) if r else None

    def pseudonym_of(self, subject: str) -> str | None:
        t = s.subject_keys
        with self._e.connect() as c:
            r = c.execute(select(t.c.pseudonym).where(t.c.subject == subject)).first()
        return str(r[0]) if r else None

    def delete(self, subject: str) -> str | None:
        t = s.subject_keys
        with self._e.begin() as c:
            r = c.execute(delete(t).where(t.c.subject == subject).returning(t.c.pseudonym)).first()
        return str(r[0]) if r else None

    def delete_prefix_before(self, prefix: str, before: datetime) -> int:
        t = s.subject_keys
        with self._e.begin() as c:
            r = c.execute(delete(t).where(t.c.subject.startswith(prefix), t.c.created_at < before))
        return int(r.rowcount or 0)


class PgSecretStore:
    """Encrypted company AI keys (captain.core.secrets_store). Holds ciphertext only."""

    def __init__(self, engine: Engine) -> None:
        self._e = engine

    @staticmethod
    def _row(r: Any) -> StoredSecret:
        return StoredSecret(
            id=r.id, provider_id=r.provider_id, ciphertext=bytes(r.ciphertext),
            nonce=bytes(r.nonce), wrapped_dek=bytes(r.wrapped_dek), wrap_key_id=r.wrap_key_id,
            last4=r.last4, created_by=r.created_by, created_at=r.created_at,
        )  # fmt: skip

    def put(self, x: StoredSecret) -> None:
        with self._e.begin() as c:
            c.execute(insert(s.provider_secrets).values(
                id=x.id, provider_id=x.provider_id, ciphertext=x.ciphertext, nonce=x.nonce,
                wrapped_dek=x.wrapped_dek, wrap_key_id=x.wrap_key_id, last4=x.last4,
                created_by=x.created_by, created_at=x.created_at,
            ))  # fmt: skip

    def get(self, secret_id: str) -> StoredSecret | None:
        t = s.provider_secrets
        with self._e.connect() as c:
            r = c.execute(select(t).where(t.c.id == secret_id)).first()
        return self._row(r) if r else None

    def all(self) -> list[StoredSecret]:
        t = s.provider_secrets
        with self._e.connect() as c:
            return [self._row(r) for r in c.execute(select(t).order_by(t.c.created_at))]

    def delete(self, secret_id: str) -> bool:
        t = s.provider_secrets
        with self._e.begin() as c:
            return bool(c.execute(delete(t).where(t.c.id == secret_id)).rowcount)
