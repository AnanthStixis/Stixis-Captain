"""Core Decision API contract harness.

These tests define required behaviour of ANY CoreDecisionAPI implementation.
Today they run against the in-memory stub; the real CaptainCore must pass them too.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest

from captain.core.decision_api import (
    ApprovalError,
    AuthenticationError,
    BudgetExceededError,
    ContextUnresolvedError,
    CoreDecisionAPI,
    EvidenceUnavailableError,
)
from captain.core.models import (
    ActualUsage,
    ApprovalState,
    Channel,
    ContextHints,
    CostEstimate,
    CoverageClass,
    Credential,
    DecisionType,
    EventType,
    ReasonCode,
    ResolvedContext,
    TokenUsage,
    UsageEvent,
    UsageSource,
)
from tests.conftest import AGENT_KEY, APP_KEY, model_call, tool_call


def est(amount: int) -> CostEstimate:
    return CostEstimate(input_tokens=100, max_output_tokens=100, amount=amount, price_version="p1")


def actual(cost: int) -> ActualUsage:
    return ActualUsage(
        usage=TokenUsage(input_tokens=100, output_tokens=40),
        source=UsageSource.PROVIDER_REPORTED,
        cost=cost,
        price_version="p1",
    )


def test_stub_satisfies_protocol(core: Any) -> None:
    assert isinstance(getattr(core, "core", core), CoreDecisionAPI)


# ---------------------------------------------------------------- resolve


def test_unknown_credential_rejected(core: Any) -> None:
    with pytest.raises(AuthenticationError):
        core.resolve(Credential(kind="virtual_key", secret="nope"), ContextHints())


def test_revoked_key_rejected_with_reason(core: Any) -> None:
    core.revoke_key(APP_KEY)
    with pytest.raises(AuthenticationError) as e:
        core.resolve(Credential(kind="virtual_key", secret=APP_KEY), ContextHints())
    assert e.value.reason_code is ReasonCode.KEY_REVOKED


def test_conflicting_project_hint_is_not_silently_reassigned(core: Any) -> None:
    with pytest.raises(ContextUnresolvedError):
        core.resolve(
            Credential(kind="virtual_key", secret=APP_KEY), ContextHints(project_id="someone-else")
        )


# ---------------------------------------------------------------- decide


def test_allowed_model(core: Any, app_ctx: ResolvedContext) -> None:
    d = core.decide(model_call(app_ctx))
    assert d.decision is DecisionType.ALLOW and d.permits_execution
    assert d.policy_version and d.trace_id == "t-1"


def test_disallowed_model_has_specific_reason(core: Any, app_ctx: ResolvedContext) -> None:
    d = core.decide(model_call(app_ctx, model="random-model"))
    assert d.decision is DecisionType.BLOCK
    assert d.reason_code is ReasonCode.MODEL_NOT_ALLOWED
    assert not d.permits_execution


def test_kill_switch_blocks(core: Any, app_ctx: ResolvedContext) -> None:
    core.kill_principal(app_ctx.principal.id)
    assert core.decide(model_call(app_ctx)).reason_code is ReasonCode.KILL_SWITCH_ACTIVE


def test_every_decision_has_a_specific_reason(
    core: Any, app_ctx: ResolvedContext, agent_ctx: ResolvedContext
) -> None:
    for req in (model_call(app_ctx), model_call(app_ctx, "x"), tool_call(agent_ctx, {"a": 1})):
        d = core.decide(req)
        assert d.reason_code in ReasonCode and d.explanation


# ---------------------------------------------------------------- budget


def test_reserve_settle_releases_unused(core: Any, app_ctx: ResolvedContext) -> None:
    core.set_budget("principal:fraud-triage", 1_000)
    d = core.decide(model_call(app_ctx))
    r = core.reserve(d, est(600))
    s = core.settle(r, actual(250))
    assert (s.charged, s.released) == (250, 350)
    assert core.budget("principal:fraud-triage") == {"limit": 1_000, "spent": 250, "reserved": 0}


def test_release_on_failure_charges_nothing(core: Any, app_ctx: ResolvedContext) -> None:
    core.set_budget("team:team-1", 1_000)
    r = core.reserve(core.decide(model_call(app_ctx)), est(500))
    assert core.release(r).charged == 0
    assert core.budget("team:team-1")["reserved"] == 0


def test_double_settle_rejected(core: Any, app_ctx: ResolvedContext) -> None:
    core.set_budget("team:team-1", 1_000)
    r = core.reserve(core.decide(model_call(app_ctx)), est(100))
    core.settle(r, actual(50))
    with pytest.raises(BudgetExceededError):
        core.settle(r, actual(50))


def test_tightest_level_wins(core: Any, app_ctx: ResolvedContext) -> None:
    core.set_budget("org:org-1", 1_000_000)
    core.set_budget("project:proj-1", 100)
    with pytest.raises(BudgetExceededError) as e:
        core.reserve(core.decide(model_call(app_ctx)), est(101))
    assert "project:proj-1" in e.value.budget_key
    assert core.budget("org:org-1")["reserved"] == 0  # nothing reserved on any level


def test_cannot_reserve_for_blocked_decision(core: Any, app_ctx: ResolvedContext) -> None:
    core.set_budget("team:team-1", 1_000)
    with pytest.raises(BudgetExceededError):
        core.reserve(core.decide(model_call(app_ctx, "x")), est(1))


def test_no_overshoot_under_100_concurrent_calls(core: Any, app_ctx: ResolvedContext) -> None:
    """CTO acceptance: concurrent calls against a nearly exhausted budget never overshoot."""
    core.set_budget("principal:fraud-triage", 1_000)
    d = core.decide(model_call(app_ctx))

    def attempt(_: int) -> bool:
        try:
            core.reserve(d, est(30))
            return True
        except BudgetExceededError:
            return False

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(attempt, range(100)))
    assert sum(results) == 33  # floor(1000 / 30)
    b = core.budget("principal:fraud-triage")
    assert b["reserved"] + b["spent"] <= b["limit"]


# ---------------------------------------------------------------- approvals


def test_a3_tool_call_requires_approval_and_is_single_use(
    core: Any, agent_ctx: ResolvedContext
) -> None:
    req = tool_call(agent_ctx, {"amount": 500})
    d = core.decide(req)
    assert d.decision is DecisionType.REQUIRE_APPROVAL
    t = core.request_approval(req, d)
    assert t.state is ApprovalState.PENDING
    core.decide_approval(t.ticket_id, approve=True, approver_id="fin-1")
    assert core.consume_approval(t.ticket_id, req).state is ApprovalState.CONSUMED
    with pytest.raises(ApprovalError):  # replay refused
        core.consume_approval(t.ticket_id, req)


def test_approval_bound_to_arguments(core: Any, agent_ctx: ResolvedContext) -> None:
    req = tool_call(agent_ctx, {"amount": 500})
    t = core.request_approval(req, core.decide(req))
    core.decide_approval(t.ticket_id, approve=True, approver_id="fin-1")
    with pytest.raises(ApprovalError):
        core.consume_approval(t.ticket_id, tool_call(agent_ctx, {"amount": 50_000}))


def test_rejected_approval_cannot_be_used(core: Any, agent_ctx: ResolvedContext) -> None:
    req = tool_call(agent_ctx, {"amount": 1})
    t = core.request_approval(req, core.decide(req))
    core.decide_approval(t.ticket_id, approve=False, approver_id="fin-1")
    with pytest.raises(ApprovalError):
        core.consume_approval(t.ticket_id, req)


def test_approval_expires_to_refusal(core_factory: Callable[..., Any]) -> None:
    c = core_factory(approval_ttl=timedelta(seconds=0))
    ctx = c.resolve(Credential(kind="virtual_key", secret=AGENT_KEY), ContextHints())
    req = tool_call(ctx, {"amount": 1})
    t = c.request_approval(req, c.decide(req))
    assert c.approval_status(t.ticket_id).state is ApprovalState.EXPIRED


def test_kill_switch_cancels_pending_approvals(core: Any, agent_ctx: ResolvedContext) -> None:
    req = tool_call(agent_ctx, {"amount": 1})
    t = core.request_approval(req, core.decide(req))
    core.kill_principal(agent_ctx.principal.id)
    assert core.approval_status(t.ticket_id).state is ApprovalState.CANCELLED


# ---------------------------------------------------------------- record


def _event(ctx: ResolvedContext) -> UsageEvent:
    return UsageEvent(
        trace_id="t-1",
        event_type=EventType.CALL_ALLOWED,
        channel=Channel.API,
        coverage=CoverageClass.GOVERNED,
        principal_id=ctx.principal.id,
        register_id=ctx.register_entry.id,
        decision=DecisionType.ALLOW,
        reason_code=ReasonCode.ALLOWED,
        usage=actual(10),
    )


def test_record_returns_receipt_partitioned_by_register(
    core: Any, app_ctx: ResolvedContext
) -> None:
    receipt = core.record(_event(app_ctx))
    assert receipt.partition == app_ctx.register_entry.id
    assert len(core.ledger) == 1


def test_evidence_unavailable_is_raised_not_swallowed(core: Any, app_ctx: ResolvedContext) -> None:
    """Integrations must refuse Tier 1–2 actions when this is raised (CTO F4)."""
    core.evidence_available = False
    with pytest.raises(EvidenceUnavailableError):
        core.record(_event(app_ctx))


def test_observed_events_accepted_without_decision(core: Any, app_ctx: ResolvedContext) -> None:
    ev = UsageEvent(
        trace_id="t-9",
        event_type=EventType.CALL_OBSERVED,
        channel=Channel.HOOK,
        coverage=CoverageClass.OBSERVED,
        principal_id=None,
        register_id=None,
    )
    assert core.record(ev).partition == "unattributed"
