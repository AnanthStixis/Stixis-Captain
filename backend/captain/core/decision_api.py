"""Core Decision API — the only door into Captain Core.

Every integration in the Integration Layer depends on this interface and nothing
else in `captain.core`. Implementations:
- `captain.core.stub.InMemoryCore`  — deterministic in-memory stub for the harness (now)
- `captain.core.engine.CaptainCore` — real implementation wired to the 8 modules (Phase 1)

Call order for a governed action (see docs/adr/0002-core-decision-api.md):

    ctx = core.resolve(credential, hints)
    decision = core.decide(request)               # policy + guardrails
    if decision.decision is REQUIRE_APPROVAL: core.request_approval(...) / approval_status(...)
    reservation = core.reserve(decision, estimate) # budget, atomic across all levels
    ... call provider / tool ...
    core.settle(reservation, actual)              # or core.release(reservation) on failure
    core.record(event)                            # evidence; must succeed for Tier 1–2
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from captain.core.models import (
    ActualUsage,
    ApprovalTicket,
    ContextHints,
    CostEstimate,
    Credential,
    Decision,
    GovernanceRequest,
    LedgerReceipt,
    PricedUsage,
    ProviderTarget,
    ReasonCode,
    Reservation,
    ResolvedContext,
    ScopeUsage,
    Settlement,
    ToolDescriptor,
    ToolOutputVerdict,
    ToolTarget,
    UsageEvent,
)


class CoreError(Exception):
    """Base error. Carries a reason code integrations map to channel-specific errors."""

    reason_code: ReasonCode = ReasonCode.CONTROL_UNAVAILABLE

    def __init__(self, message: str, reason_code: ReasonCode | None = None) -> None:
        super().__init__(message)
        if reason_code is not None:
            self.reason_code = reason_code


class AuthenticationError(CoreError):
    reason_code = ReasonCode.AUTH_INVALID


class ContextUnresolvedError(CoreError):
    reason_code = ReasonCode.CONTEXT_UNRESOLVED


class BudgetExceededError(CoreError):
    reason_code = ReasonCode.BUDGET_EXHAUSTED

    def __init__(self, message: str, budget_key: str) -> None:
        super().__init__(message)
        self.budget_key = budget_key


class ApprovalError(CoreError):
    reason_code = ReasonCode.APPROVAL_REQUIRED


class EvidenceUnavailableError(CoreError):
    """Evidence could not be durably queued. Tier 1–2 callers must refuse the action."""

    reason_code = ReasonCode.CONTROL_UNAVAILABLE


@runtime_checkable
class CoreDecisionAPI(Protocol):
    # Identity and RBAC + Project Context
    def resolve(self, credential: Credential, hints: ContextHints) -> ResolvedContext: ...

    # OPA Policy Engine + PII and Security Guardrails (+ kill switch check)
    def decide(self, request: GovernanceRequest) -> Decision: ...

    # Budget and Rate Limits
    def reserve(self, decision: Decision, estimate: CostEstimate) -> Reservation: ...
    def settle(self, reservation: Reservation, actual: ActualUsage) -> Settlement: ...
    def release(self, reservation: Reservation) -> Settlement: ...

    # Approval Engine
    def request_approval(
        self, request: GovernanceRequest, decision: Decision
    ) -> ApprovalTicket: ...
    def approval_status(self, ticket_id: UUID) -> ApprovalTicket: ...
    def consume_approval(self, ticket_id: UUID, request: GovernanceRequest) -> ApprovalTicket: ...

    # Usage Collector + Evidence Ledger
    def record(self, event: UsageEvent) -> LedgerReceipt: ...


class OutputBlockedError(CoreError):
    """A model response was stopped by output guardrails (e.g. a secret in the reply)."""

    reason_code = ReasonCode.OUTPUT_BLOCKED

    def __init__(self, entity: str) -> None:
        super().__init__(f"output blocked: {entity}")
        self.entity = entity


class ModelNotPricedError(CoreError):
    """No price for the model: Captain cannot reserve budget, so it refuses the call."""

    reason_code = ReasonCode.MODEL_NOT_PRICED


class NoRouteError(CoreError):
    reason_code = ReasonCode.NO_ROUTE


@runtime_checkable
class OutputGuard(Protocol):
    """Checks a model response (streamed or whole) according to the tier's mode.
    Raises an error with reason SECRET_DETECTED-style blocking where the tier requires."""

    mode: str

    def feed(self, chunk: str) -> Iterator[str]: ...
    def finish(self) -> Iterator[str]: ...


@runtime_checkable
class McpCoreAPI(CoreDecisionAPI, Protocol):
    """Extra Core operations the MCP proxy needs (public contract, like GatewayCoreAPI)."""

    def find_approval(self, request: GovernanceRequest) -> ApprovalTicket | None: ...
    def list_tools(self, ctx: ResolvedContext) -> list[ToolDescriptor]: ...
    def tool_target(self, ctx: ResolvedContext, tool: str) -> ToolTarget: ...
    def screen_tool_output(self, request: GovernanceRequest, text: str) -> ToolOutputVerdict: ...


@runtime_checkable
class GatewayCoreAPI(CoreDecisionAPI, Protocol):
    """Extra Core operations the model gateway needs. Still only public contract:
    integrations never import Core internals (ADR-001, enforced by tests)."""

    def route(self, decision: Decision) -> list[ProviderTarget]: ...
    def estimate_cost(
        self, decision: Decision, input_chars: int, max_output_tokens: int
    ) -> CostEstimate: ...
    def price_usage(
        self,
        target: ProviderTarget,
        usage_format: str,
        raw_usage: dict[str, object] | None,
        fallback_input_chars: int,
        fallback_output_chars: int,
    ) -> PricedUsage: ...
    def output_guard(self, decision: Decision) -> OutputGuard: ...
    def usage(self, ctx: ResolvedContext) -> list[ScopeUsage]: ...
    def evaluate(self, decision: Decision, messages: list[dict[str, Any]], answer: str) -> None: ...
