from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from captain.core.approvals.service import ApprovalService, InMemoryApprovalStore
from captain.core.budget.model import BudgetDefinition, Period, RateLimitDefinition
from captain.core.budget.service import (
    BudgetAlert,
    BudgetOverride,
    BudgetService,
    InMemoryBudgetCatalog,
    RateLimitedError,
)
from captain.core.budget.store import InMemoryBudgetStore, UnknownReservation
from captain.core.decision_api import ApprovalError, BudgetExceededError
from captain.core.models import (
    ApprovalState,
    ApprovalTicket,
    Decision,
    DecisionType,
    ReasonCode,
    ResolvedContext,
    RiskTier,
)
from tests.conftest import tool_call

SCOPES = ("org:o1", "team:t1", "key:k1")


@pytest.fixture
def events() -> dict[str, list[object]]:
    return {"alerts": [], "overrides": []}


@pytest.fixture
def budget(events: dict[str, list[object]]) -> BudgetService:
    cat = InMemoryBudgetCatalog()
    cat.add(BudgetDefinition("org:o1", Period.MONTHLY, 1_000_000))
    cat.add(BudgetDefinition("team:t1", Period.MONTHLY, 1_000))
    cat.add(BudgetDefinition("key:k1", Period.DAILY, 500, hard=False))
    cat.add_rate(RateLimitDefinition("key:k1", requests_per_minute=3))
    return BudgetService(
        cat, InMemoryBudgetStore(), events["alerts"].append, events["overrides"].append
    )


def test_reserve_settle(budget: BudgetService) -> None:
    r = budget.reserve(uuid4(), SCOPES, 400)
    assert len(r.budget_keys) == 3
    budget.settle(r, 100)
    team = next(k for k in r.budget_keys if ":team:" in k)
    s = budget.state(team)
    assert (s.spent, s.reserved) == (100, 0)


def test_hard_limit_refuses_soft_limit_allows(budget: BudgetService) -> None:
    budget.reserve(uuid4(), SCOPES, 900)  # key:k1 soft limit 500 exceeded → still allowed
    with pytest.raises(BudgetExceededError) as e:
        budget.reserve(uuid4(), SCOPES, 200)  # team hard limit 1000
    assert ":team:t1:" in e.value.budget_key


def test_double_settle_refused(budget: BudgetService) -> None:
    r = budget.reserve(uuid4(), SCOPES, 10)
    budget.settle(r, 5)
    with pytest.raises(UnknownReservation):
        budget.settle(r, 5)


def test_concurrent_no_overshoot(budget: BudgetService) -> None:
    def go(_: int) -> bool:
        try:
            budget.reserve(uuid4(), SCOPES, 30)
            return True
        except BudgetExceededError:
            return False

    with ThreadPoolExecutor(32) as p:
        ok = sum(p.map(go, range(100)))
    assert ok == 33


def test_alerts_once_per_threshold(budget: BudgetService, events: dict[str, list[object]]) -> None:
    for _ in range(3):
        budget.settle(budget.reserve(uuid4(), SCOPES, 300), 300)
    team_alerts = [
        a for a in events["alerts"] if isinstance(a, BudgetAlert) and a.scope == "team:t1"
    ]
    assert [a.threshold for a in team_alerts] == [0.8]  # 900/1000 crosses 80% once


def test_override_is_audited_and_raises_limit(
    budget: BudgetService, events: dict[str, list[object]]
) -> None:
    budget.settle(budget.reserve(uuid4(), SCOPES, 1000), 1000)
    with pytest.raises(BudgetExceededError):
        budget.reserve(uuid4(), SCOPES, 1)
    budget.override("team:t1", 500, "finops-1", "quarter-end batch")
    budget.reserve(uuid4(), SCOPES, 400)
    assert isinstance(events["overrides"][0], BudgetOverride)
    with pytest.raises(ValueError):
        budget.override("team:t1", 5, "x", " ")


def test_rate_limit(budget: BudgetService) -> None:
    for _ in range(3):
        budget.check_rate(SCOPES, 10)
    with pytest.raises(RateLimitedError) as e:
        budget.check_rate(SCOPES, 10)
    assert e.value.reason_code is ReasonCode.RATE_LIMITED


def test_sweep_releases_expired() -> None:
    store = InMemoryBudgetStore()
    from captain.core.budget.model import BudgetLevel

    store.reserve("r1", [BudgetLevel("b", 100, True)], 60, ttl_s=0)
    assert store.sweep() == 1 and store.state("b").reserved == 0


# ------------------------------------------------------------------ approvals


def _decision(req_id: object, d: DecisionType = DecisionType.REQUIRE_APPROVAL) -> Decision:
    return Decision(
        request_id=req_id,
        trace_id="t",
        decision=d,
        reason_code=ReasonCode.APPROVAL_REQUIRED,  # type: ignore[arg-type]
        explanation="x",
        policy_bundle="b",
        policy_version="1",
        risk_tier=RiskTier.TIER_2,
    )


@pytest.fixture
def changes() -> list[ApprovalTicket]:
    return []


@pytest.fixture
def approvals(changes: list[ApprovalTicket]) -> ApprovalService:
    return ApprovalService(
        InMemoryApprovalStore(),
        lambda r: ("fin-1", "fin-2", r.context.principal.id),
        changes.append,
    )


def test_full_flow(
    approvals: ApprovalService, agent_ctx: ResolvedContext, changes: list[ApprovalTicket]
) -> None:
    req = tool_call(agent_ctx, {"po": 1})
    t = approvals.request(req, _decision(req.request_id))
    assert agent_ctx.principal.id not in t.assigned_approvers  # requester excluded
    approvals.decide(t.ticket_id, True, "fin-2", "ok")
    approvals.consume(t.ticket_id, req, recheck=lambda: None)
    with pytest.raises(ApprovalError):
        approvals.consume(t.ticket_id, req, recheck=lambda: None)
    assert [c.state for c in changes] == [
        ApprovalState.PENDING,
        ApprovalState.APPROVED,
        ApprovalState.CONSUMED,
    ]


def test_self_and_unassigned_approval_refused(
    approvals: ApprovalService, agent_ctx: ResolvedContext
) -> None:
    req = tool_call(agent_ctx, {"po": 1})
    t = approvals.request(req, _decision(req.request_id))
    with pytest.raises(ApprovalError):
        approvals.decide(t.ticket_id, True, agent_ctx.principal.id, "me")
    with pytest.raises(ApprovalError):
        approvals.decide(t.ticket_id, True, "random", "x")


def test_recheck_failure_keeps_ticket_unconsumed(
    approvals: ApprovalService, agent_ctx: ResolvedContext
) -> None:
    req = tool_call(agent_ctx, {"po": 1})
    t = approvals.request(req, _decision(req.request_id))
    approvals.decide(t.ticket_id, True, "fin-1", "ok")

    def fail() -> None:
        raise BudgetExceededError("no money", "b")

    with pytest.raises(BudgetExceededError):
        approvals.consume(t.ticket_id, req, recheck=fail)
    assert approvals.status(t.ticket_id).state is ApprovalState.APPROVED


def test_changed_args_refused(approvals: ApprovalService, agent_ctx: ResolvedContext) -> None:
    req = tool_call(agent_ctx, {"po": 1})
    t = approvals.request(req, _decision(req.request_id))
    approvals.decide(t.ticket_id, True, "fin-1", "ok")
    with pytest.raises(ApprovalError):
        approvals.consume(t.ticket_id, tool_call(agent_ctx, {"po": 999}), recheck=lambda: None)


def test_expiry_and_cancel(agent_ctx: ResolvedContext) -> None:
    svc = ApprovalService(
        InMemoryApprovalStore(), lambda r: ("a",), lambda t: None, ttl=timedelta(0)
    )
    req = tool_call(agent_ctx, {})
    t = svc.request(req, _decision(req.request_id))
    assert svc.status(t.ticket_id).state is ApprovalState.EXPIRED
    svc2 = ApprovalService(InMemoryApprovalStore(), lambda r: ("a",), lambda t: None)
    t2 = svc2.request(req, _decision(req.request_id))
    assert svc2.cancel_for_principal(agent_ctx.principal.id, "kill") == 1
    assert svc2.status(t2.ticket_id).state is ApprovalState.CANCELLED


def test_no_approver_configured(agent_ctx: ResolvedContext) -> None:
    svc = ApprovalService(
        InMemoryApprovalStore(), lambda r: (r.context.principal.id,), lambda t: None
    )
    req = tool_call(agent_ctx, {})
    with pytest.raises(ApprovalError):
        svc.request(req, _decision(req.request_id))


def test_escalation_adds_backup_approvers_once(agent_ctx: ResolvedContext) -> None:
    changes: list[ApprovalTicket] = []
    svc = ApprovalService(InMemoryApprovalStore(), lambda r: ("fin-1",), changes.append)
    req = tool_call(agent_ctx, {"po": 1})
    t = svc.request(req, _decision(req.request_id))
    assert svc.escalate_due(timedelta(hours=1), lambda t: ("cfo",)) == []  # not due yet
    esc = svc.escalate_due(timedelta(0), lambda t: ("cfo", agent_ctx.principal.id))
    assert esc[0].assigned_approvers == ("fin-1", "cfo")  # requester never added
    assert svc.escalate_due(timedelta(0), lambda t: ("cfo",)) == []  # only once
    svc.decide(t.ticket_id, True, "cfo", "approved on escalation")
    from captain.core.engine import approval_event

    kinds = [e.event_type.value for c in changes if (e := approval_event(c))]
    assert kinds == ["approval.escalated", "approval.decided"]


def test_no_budget_means_refused_when_required() -> None:
    from captain.core.budget.service import BudgetNotConfiguredError

    cat = InMemoryBudgetCatalog()
    strict = BudgetService(
        cat, InMemoryBudgetStore(), lambda a: None, lambda o: None, require_budget=True
    )
    with pytest.raises(BudgetNotConfiguredError) as e:
        strict.reserve(uuid4(), ("org:o1", "team:t1"), 10)
    assert e.value.reason_code is ReasonCode.BUDGET_NOT_CONFIGURED
    cat.add(BudgetDefinition("team:t1", Period.MONTHLY, 1000, hard=False))
    with pytest.raises(BudgetNotConfiguredError):  # a soft budget alone is not a cap
        strict.reserve(uuid4(), ("org:o1", "team:t1"), 10)
    cat.add(BudgetDefinition("org:o1", Period.MONTHLY, 1000))
    strict.reserve(uuid4(), ("org:o1", "team:t1"), 10)  # now capped by the org budget


def test_settings_require_budget_by_default(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from captain.config import load_settings

    base = {
        "CAPTAIN_DATABASE_URL": "postgresql+psycopg://u@h/d",
        "CAPTAIN_KEY_PEPPER": "ab" * 32,
        "CAPTAIN_LEDGER_KEY": f"pem:{tmp_path}/k.pem",
        "CAPTAIN_WORM": f"dir:{tmp_path}",
    }
    assert load_settings(base).require_budget is True
    assert load_settings({**base, "CAPTAIN_REQUIRE_BUDGET": "0"}).require_budget is False
