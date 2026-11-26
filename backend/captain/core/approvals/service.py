"""Approval Engine (CTO F7). Channel-agnostic: MCP Proxy, API Gateway and Browser all
use the same tickets.

- Hold: a pending ticket is returned immediately; the caller polls or gets a callback.
- Binding: the ticket stores SHA-256 of the canonical arguments; changed arguments fail.
- Single use: an approved ticket is consumed once (CONSUMED); replay fails.
- Expiry: pending past `expires_at` becomes EXPIRED, which is a refusal.
- Separation of duties: the requester can never approve their own request.
- Delegation: if the primary approver is unavailable, delegates are also eligible.
- Re-check at release: the caller passes a re-check that re-runs policy and budget.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from captain.core.decision_api import ApprovalError
from captain.core.models import (
    ApprovalState,
    ApprovalTicket,
    Decision,
    DecisionType,
    GovernanceRequest,
)


def arguments_hash(arguments: dict[str, object] | None) -> str:
    canonical = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode()).hexdigest()


class ApprovalStore(Protocol):
    def get(self, ticket_id: UUID) -> ApprovalTicket | None: ...
    def put(self, ticket: ApprovalTicket) -> None: ...
    def compare_and_set(
        self, ticket_id: UUID, expected: ApprovalState, new: ApprovalTicket
    ) -> bool: ...
    def pending_for_principal(self, principal_id: str) -> list[ApprovalTicket]: ...
    def open_for_principal(self, principal_id: str) -> list[ApprovalTicket]: ...
    def pending(self, limit: int) -> list[ApprovalTicket]: ...


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._t: dict[UUID, ApprovalTicket] = {}
        self._lock = threading.Lock()

    def get(self, ticket_id: UUID) -> ApprovalTicket | None:
        return self._t.get(ticket_id)

    def put(self, ticket: ApprovalTicket) -> None:
        with self._lock:
            self._t[ticket.ticket_id] = ticket

    def compare_and_set(
        self, ticket_id: UUID, expected: ApprovalState, new: ApprovalTicket
    ) -> bool:
        with self._lock:
            cur = self._t.get(ticket_id)
            if cur is None or cur.state is not expected:
                return False
            self._t[ticket_id] = new
            return True

    def pending(self, limit: int) -> list[ApprovalTicket]:
        return [t for t in self._t.values() if t.state is ApprovalState.PENDING][:limit]

    def pending_for_principal(self, principal_id: str) -> list[ApprovalTicket]:
        return [
            t
            for t in self._t.values()
            if t.requested_by == principal_id and t.state is ApprovalState.PENDING
        ]

    def open_for_principal(self, principal_id: str) -> list[ApprovalTicket]:
        return [
            t
            for t in self._t.values()
            if t.requested_by == principal_id
            and t.state in (ApprovalState.PENDING, ApprovalState.APPROVED)
        ]


ApproverResolver = Callable[[GovernanceRequest], tuple[str, ...]]
ApproverResolverForTicket = Callable[[ApprovalTicket], tuple[str, ...]]
TicketListener = Callable[[ApprovalTicket], None]


class ApprovalService:
    def __init__(
        self,
        store: ApprovalStore,
        approvers: ApproverResolver,
        on_change: TicketListener,
        ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._store = store
        self._approvers = approvers
        self._on_change = on_change
        self._ttl = ttl

    def request(self, req: GovernanceRequest, decision: Decision) -> ApprovalTicket:
        if decision.decision is not DecisionType.REQUIRE_APPROVAL:
            raise ApprovalError("decision does not require approval")
        approvers = tuple(a for a in self._approvers(req) if a != req.context.principal.id)
        if not approvers:
            raise ApprovalError("no eligible approver configured for this register entry")
        t = ApprovalTicket(
            request_id=req.request_id,
            state=ApprovalState.PENDING,
            arguments_hash=arguments_hash(req.tool_arguments),
            requested_by=req.context.principal.id,
            register_id=req.context.register_entry.id,
            channel=req.channel,
            action_summary=f"{req.channel.value} {req.tool or req.model}",
            policy_version=decision.policy_version,
            assigned_approvers=approvers,
            expires_at=datetime.now(UTC) + self._ttl,
        )
        self._store.put(t)
        self._on_change(t)
        return t

    def find_open(self, req: GovernanceRequest) -> ApprovalTicket | None:
        """The newest unexpired PENDING or APPROVED ticket this caller already has for exactly
        this action: same principal, register entry, channel and tool, same argument hash.
        Lets a client that cannot attach a ticket id (it only repeats the call) still use an
        approval. It does not weaken anything: `consume` checks the same bindings, once."""
        want = f"{req.channel.value} {req.tool or req.model}"
        h = arguments_hash(req.tool_arguments)
        now = datetime.now(UTC)
        hits = [
            t
            for t in self._store.open_for_principal(req.context.principal.id)
            if t.register_id == req.context.register_entry.id
            and t.action_summary == want
            and t.arguments_hash == h
            and t.expires_at > now
        ]
        return max(hits, key=lambda t: t.created_at) if hits else None

    def status(self, ticket_id: UUID) -> ApprovalTicket:
        t = self._store.get(ticket_id)
        if t is None:
            raise ApprovalError("unknown ticket")
        if t.state is ApprovalState.PENDING and datetime.now(UTC) >= t.expires_at:
            expired = t.model_copy(update={"state": ApprovalState.EXPIRED, "reason": "timeout"})
            if self._store.compare_and_set(ticket_id, ApprovalState.PENDING, expired):
                self._on_change(expired)
            return self._store.get(ticket_id) or expired
        return t

    def decide(
        self, ticket_id: UUID, approve: bool, approver_id: str, reason: str
    ) -> ApprovalTicket:
        t = self.status(ticket_id)
        if t.state is not ApprovalState.PENDING:
            raise ApprovalError(f"ticket is {t.state}")
        if approver_id == t.requested_by:
            raise ApprovalError("requester cannot approve their own request")
        if approver_id not in t.assigned_approvers:
            raise ApprovalError("not an assigned approver for this ticket")
        new = t.model_copy(
            update={
                "state": ApprovalState.APPROVED if approve else ApprovalState.REJECTED,
                "approver_id": approver_id,
                "decided_at": datetime.now(UTC),
                "reason": reason,
            }
        )
        if not self._store.compare_and_set(ticket_id, ApprovalState.PENDING, new):
            raise ApprovalError("ticket changed concurrently")
        self._on_change(new)
        return new

    def consume(
        self, ticket_id: UUID, req: GovernanceRequest, recheck: Callable[[], None]
    ) -> ApprovalTicket:
        """Use an approval exactly once. `recheck` re-runs kill switch, policy and budget
        checks and raises if the action is no longer permitted."""
        t = self.status(ticket_id)
        if t.state is not ApprovalState.APPROVED:
            raise ApprovalError(f"ticket is {t.state}")
        # An approval belongs to the principal (and register entry) that requested it;
        # identical arguments from another agent must not ride on it.
        if (
            t.requested_by != req.context.principal.id
            or t.register_id != req.context.register_entry.id
        ):
            raise ApprovalError("approval belongs to a different principal")
        # ...and to the exact action: the same arguments must not unlock another tool.
        if t.action_summary != f"{req.channel.value} {req.tool or req.model}":
            raise ApprovalError("approval was granted for a different action")
        if t.arguments_hash != arguments_hash(req.tool_arguments):
            raise ApprovalError("arguments changed since approval")
        recheck()
        used = t.model_copy(update={"state": ApprovalState.CONSUMED})
        if not self._store.compare_and_set(ticket_id, ApprovalState.APPROVED, used):
            raise ApprovalError("approval already used")
        self._on_change(used)
        return used

    def escalate_due(
        self, after: timedelta, escalation: ApproverResolverForTicket, limit: int = 500
    ) -> list[ApprovalTicket]:
        """Pending tickets older than `after` (and not yet escalated) get the escalation
        approvers added. Called by the worker; the change listener records it."""
        now = datetime.now(UTC)
        out: list[ApprovalTicket] = []
        for t in self._store.pending(limit):
            if t.escalated_at is not None or now - t.created_at < after or now >= t.expires_at:
                continue
            extra = tuple(
                a for a in escalation(t) if a != t.requested_by and a not in t.assigned_approvers
            )
            new = t.model_copy(
                update={"assigned_approvers": t.assigned_approvers + extra, "escalated_at": now}
            )
            if self._store.compare_and_set(t.ticket_id, ApprovalState.PENDING, new):
                self._on_change(new)
                out.append(new)
        return out

    def cancel_for_principal(self, principal_id: str, reason: str) -> int:
        n = 0
        for t in self._store.pending_for_principal(principal_id):
            c = t.model_copy(update={"state": ApprovalState.CANCELLED, "reason": reason})
            if self._store.compare_and_set(t.ticket_id, ApprovalState.PENDING, c):
                self._on_change(c)
                n += 1
        return n
