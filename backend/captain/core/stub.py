"""In-memory stub of the Core Decision API.

ILLUSTRATIVE / TEST-ONLY. Not production code: no persistence, no OPA, no real
guardrails, single process. It exists so that:
1. the contract tests in tests/harness/ can pin down required behaviour now, and
2. integration teams can build against the contract before the real Core lands.

The real `CaptainCore` must pass the same harness suite.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID

from captain.core.decision_api import (
    ApprovalError,
    AuthenticationError,
    BudgetExceededError,
    ContextUnresolvedError,
    EvidenceUnavailableError,
)
from captain.core.models import (
    ActionType,
    ActualUsage,
    ApprovalState,
    ApprovalTicket,
    ContextHints,
    CostEstimate,
    Credential,
    Decision,
    DecisionType,
    GovernanceRequest,
    LedgerReceipt,
    ReasonCode,
    Reservation,
    ResolvedContext,
    Settlement,
    UsageEvent,
)

STUB_POLICY_BUNDLE = "stub"
STUB_POLICY_VERSION = "0.0.0-stub"
APPROVAL_AUTONOMY = {"A3", "A4"}


def arguments_hash(arguments: dict[str, object] | None) -> str:
    canonical = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _key_id(secret: str) -> str:
    # Real implementation: HMAC-SHA-256 with a server-side pepper over the secret part.
    return hmac.new(b"stub-pepper", secret.encode(), hashlib.sha256).hexdigest()


class InMemoryCore:
    """Thread-safe in-memory implementation of CoreDecisionAPI."""

    def __init__(self, approval_ttl: timedelta = timedelta(minutes=30)) -> None:
        self._lock = threading.Lock()
        self._contexts: dict[str, ResolvedContext] = {}  # key hash -> context
        self._revoked: set[str] = set()
        self._killed_principals: set[str] = set()
        self._budgets: dict[str, dict[str, int]] = {}  # key -> {limit, spent, reserved}
        self._reservations: dict[UUID, Reservation] = {}
        self._tickets: dict[UUID, ApprovalTicket] = {}
        self._ticket_targets: dict[UUID, UUID] = {}
        self.ledger: list[UsageEvent] = []
        self.evidence_available = True
        self._approval_ttl = approval_ttl

    # ---------------------------------------------------------------- test setup
    def register_key(self, secret: str, context: ResolvedContext) -> None:
        self._contexts[_key_id(secret)] = context

    def revoke_key(self, secret: str) -> None:
        self._revoked.add(_key_id(secret))

    def kill_principal(self, principal_id: str) -> None:
        with self._lock:
            self._killed_principals.add(principal_id)
            for tid, t in list(self._tickets.items()):
                if t.state is ApprovalState.PENDING:
                    self._tickets[tid] = t.model_copy(update={"state": ApprovalState.CANCELLED})

    def set_budget(self, key: str, limit: int) -> None:
        self._budgets[key] = {"limit": limit, "spent": 0, "reserved": 0}

    def budget(self, key: str) -> dict[str, int]:
        return dict(self._budgets[key])

    def decide_approval(self, ticket_id: UUID, approve: bool, approver_id: str) -> ApprovalTicket:
        with self._lock:
            t = self._tickets[ticket_id]
            if t.state is not ApprovalState.PENDING:
                raise ApprovalError(f"ticket is {t.state}")
            new = ApprovalState.APPROVED if approve else ApprovalState.REJECTED
            t = t.model_copy(
                update={"state": new, "approver_id": approver_id, "decided_at": datetime.now(UTC)}
            )
            self._tickets[ticket_id] = t
            return t

    # ---------------------------------------------------------------- contract
    def resolve(self, credential: Credential, hints: ContextHints) -> ResolvedContext:
        kid = _key_id(credential.secret)
        if kid in self._revoked:
            raise AuthenticationError("key revoked", ReasonCode.KEY_REVOKED)
        ctx = self._contexts.get(kid)
        if ctx is None:
            raise AuthenticationError("unknown credential")
        if hints.project_id and hints.project_id != ctx.project_id:
            # Never silently re-assign to another project.
            raise ContextUnresolvedError("project hint conflicts with key binding")
        return ctx

    def decide(self, request: GovernanceRequest) -> Decision:
        ctx = request.context
        entry = ctx.register_entry

        def out(d: DecisionType, r: ReasonCode, why: str, **kw: object) -> Decision:
            return Decision(
                request_id=request.request_id,
                trace_id=request.trace_id,
                decision=d,
                reason_code=r,
                explanation=why,
                policy_bundle=STUB_POLICY_BUNDLE,
                policy_version=STUB_POLICY_VERSION,
                risk_tier=entry.risk_tier,
                **kw,
            )

        if ctx.principal.id in self._killed_principals:
            return out(DecisionType.BLOCK, ReasonCode.KILL_SWITCH_ACTIVE, "Principal is killed.")
        if request.action is ActionType.MODEL_CALL and request.model not in entry.allowed_models:
            return out(
                DecisionType.BLOCK,
                ReasonCode.MODEL_NOT_ALLOWED,
                f"Model '{request.model}' is not approved for {entry.id}.",
            )
        if request.action is ActionType.TOOL_CALL:
            if request.tool not in entry.allowed_tools:
                return out(
                    DecisionType.BLOCK,
                    ReasonCode.TOOL_NOT_ALLOWED,
                    f"Tool '{request.tool}' is not allowlisted for {entry.id}.",
                )
            if entry.autonomy.value in APPROVAL_AUTONOMY:
                return out(
                    DecisionType.REQUIRE_APPROVAL,
                    ReasonCode.APPROVAL_REQUIRED,
                    f"{entry.autonomy} actions require human approval.",
                )
        return out(DecisionType.ALLOW, ReasonCode.ALLOWED, "All checks passed.")

    def reserve(self, decision: Decision, estimate: CostEstimate) -> Reservation:
        if not decision.permits_execution:
            raise BudgetExceededError("decision does not permit execution", "n/a")
        keys = tuple(sorted(self._budgets))  # stub: every budget applies
        with self._lock:  # atomic across all levels (real impl: one Valkey Lua script)
            for k in keys:
                b = self._budgets[k]
                if b["spent"] + b["reserved"] + estimate.amount > b["limit"]:
                    raise BudgetExceededError(f"budget '{k}' would be exceeded", k)
            for k in keys:
                self._budgets[k]["reserved"] += estimate.amount
            r = Reservation(
                request_id=decision.request_id,
                amount=estimate.amount,
                budget_keys=keys,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
            self._reservations[r.reservation_id] = r
            return r

    def _finish(self, reservation: Reservation, charged: int) -> Settlement:
        with self._lock:
            if self._reservations.pop(reservation.reservation_id, None) is None:
                raise BudgetExceededError("reservation unknown or already settled", "n/a")
            for k in reservation.budget_keys:
                self._budgets[k]["reserved"] -= reservation.amount
                self._budgets[k]["spent"] += charged
        return Settlement(
            reservation_id=reservation.reservation_id,
            reserved=reservation.amount,
            charged=charged,
            released=max(reservation.amount - charged, 0),
        )

    def settle(self, reservation: Reservation, actual: ActualUsage) -> Settlement:
        return self._finish(reservation, actual.cost)

    def release(self, reservation: Reservation) -> Settlement:
        return self._finish(reservation, 0)

    def request_approval(self, request: GovernanceRequest, decision: Decision) -> ApprovalTicket:
        if decision.decision is not DecisionType.REQUIRE_APPROVAL:
            raise ApprovalError("decision does not require approval")
        t = ApprovalTicket(
            request_id=request.request_id,
            state=ApprovalState.PENDING,
            arguments_hash=arguments_hash(request.tool_arguments),
            expires_at=datetime.now(UTC) + self._approval_ttl,
        )
        with self._lock:
            self._tickets[t.ticket_id] = t
        return t

    def approval_status(self, ticket_id: UUID) -> ApprovalTicket:
        with self._lock:
            t = self._tickets[ticket_id]
            if t.state is ApprovalState.PENDING and datetime.now(UTC) >= t.expires_at:
                t = t.model_copy(update={"state": ApprovalState.EXPIRED})
                self._tickets[ticket_id] = t
            return t

    def consume_approval(self, ticket_id: UUID, request: GovernanceRequest) -> ApprovalTicket:
        t = self.approval_status(ticket_id)
        with self._lock:
            if t.state is not ApprovalState.APPROVED:
                raise ApprovalError(f"ticket is {t.state}")
            if t.arguments_hash != arguments_hash(request.tool_arguments):
                raise ApprovalError("arguments changed since approval")
            if request.context.principal.id in self._killed_principals:
                raise ApprovalError("principal killed", ReasonCode.KILL_SWITCH_ACTIVE)
            t = t.model_copy(update={"state": ApprovalState.CONSUMED})
            self._tickets[ticket_id] = t
            return t

    def record(self, event: UsageEvent) -> LedgerReceipt:
        if not self.evidence_available:
            raise EvidenceUnavailableError("evidence queue unavailable")
        with self._lock:
            self.ledger.append(event)
        return LedgerReceipt(
            event_id=event.event_id,
            partition=event.register_id or "unattributed",
            accepted_at=datetime.now(UTC),
        )
