"""Regressions for defects found in the pre-Phase-2 Core review."""

from __future__ import annotations

from uuid import uuid4

import pytest

from captain.core.approvals.service import ApprovalService, InMemoryApprovalStore
from captain.core.budget.model import BudgetDefinition, Period
from captain.core.budget.service import BudgetService, InMemoryBudgetCatalog
from captain.core.budget.store import InMemoryBudgetStore
from captain.core.bus import InMemoryBus, Topic
from captain.core.decision_api import ApprovalError, BudgetExceededError
from captain.core.ledger.chain import Ed25519Signer, KeyRingVerifier
from captain.core.ledger.service import InMemoryLedgerStore, InMemoryWormStore, LedgerService
from captain.core.models import (
    AutonomyLevel,
    Channel,
    CoverageClass,
    Decision,
    DecisionType,
    EventType,
    PrincipalType,
    ReasonCode,
    RiskTier,
    UsageEvent,
)
from tests.conftest import make_context, tool_call


# 1 ------------------------------------------------------------------ approvals
def test_approval_cannot_be_consumed_by_another_principal() -> None:
    svc = ApprovalService(InMemoryApprovalStore(), lambda r: ("fin-1",), lambda t: None)
    owner = make_context("agent-a", RiskTier.TIER_2, AutonomyLevel.A3, PrincipalType.AGENT)
    other = make_context("agent-b", RiskTier.TIER_2, AutonomyLevel.A3, PrincipalType.AGENT)
    req = tool_call(owner, {"po": 1})
    d = Decision(
        request_id=req.request_id,
        trace_id="t",
        decision=DecisionType.REQUIRE_APPROVAL,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="x",
        policy_bundle="b",
        policy_version="1",
        risk_tier=RiskTier.TIER_2,
    )
    t = svc.request(req, d)
    svc.decide(t.ticket_id, True, "fin-1", "ok")
    with pytest.raises(ApprovalError):  # same arguments, different agent
        svc.consume(t.ticket_id, tool_call(other, {"po": 1}), recheck=lambda: None)
    svc.consume(t.ticket_id, req, recheck=lambda: None)  # the owner still can


# 2 --------------------------------------------------------------------- ledger
class CrashAfterAppend(InMemoryLedgerStore):
    crash = True

    def outbox_done(self, event_ids: list[str]) -> None:
        if self.crash:
            self.crash = False
            raise RuntimeError("process killed between append and outbox delete")
        super().outbox_done(event_ids)


def test_ledger_writer_recovers_after_crash_without_duplicates() -> None:
    store, s = CrashAfterAppend(), Ed25519Signer("k")
    svc = LedgerService(store, InMemoryWormStore(), s, KeyRingVerifier({"k": s.public_key()}))
    for i in range(3):
        svc.enqueue(
            UsageEvent(
                trace_id=f"t{i}",
                event_type=EventType.CALL_ALLOWED,
                channel=Channel.API,
                coverage=CoverageClass.GOVERNED,
                principal_id="u",
                register_id="AI-1",
            )
        )
    with pytest.raises(RuntimeError):
        svc.write_pending()
    svc.write_pending()  # restart: must not re-seal the same events
    ids = [e.event_id for e in store.rows]
    assert len(ids) == len(set(ids)) == 3 and not store.outbox
    assert svc.verify().ok


# 3 --------------------------------------------------------------------- budget
def test_budget_override_visible_to_other_instances_and_survives_restart() -> None:
    cat, store = InMemoryBudgetCatalog(), InMemoryBudgetStore()
    cat.add(BudgetDefinition("team:t1", Period.MONTHLY, 100))
    a = BudgetService(cat, store, lambda x: None, lambda x: None)
    b = BudgetService(cat, store, lambda x: None, lambda x: None)  # another process / after restart
    a.settle(a.reserve(uuid4(), ("team:t1",), 100), 100)
    a.override("team:t1", 50, "finops", "quarter end")
    b.reserve(uuid4(), ("team:t1",), 40)  # must see A's override
    with pytest.raises(BudgetExceededError):
        b.reserve(uuid4(), ("team:t1",), 20)


# 4 ------------------------------------------------------------------------ bus
def test_failing_subscriber_does_not_block_others() -> None:
    bus, got = InMemoryBus(), []

    def bad(_: str) -> None:
        raise RuntimeError("buggy handler")

    bus.subscribe(Topic.KEY_REVOKED, bad)
    bus.subscribe(Topic.KEY_REVOKED, got.append)
    bus.publish(Topic.KEY_REVOKED, "k1")  # must not raise into the publisher
    assert got == ["k1"]


# 5 ----------------------------------------------------- kill-switch cache
def test_kill_switch_cache_avoids_store_and_invalidates_on_bus() -> None:
    from captain.core.identity.killswitch import InMemoryKillSwitchStore, KillScope, KillSwitch

    class CountingStore(InMemoryKillSwitchStore):
        reads = 0

        def active(self):  # type: ignore[no-untyped-def]
            CountingStore.reads += 1
            return super().active()

    store, bus = CountingStore(), InMemoryBus()
    a = KillSwitch(store, bus, cache_ttl_s=60)  # instance A
    b = KillSwitch(store, bus, cache_ttl_s=60)  # instance B, same bus
    for _ in range(50):
        assert b.match({KillScope.MODEL: "m"}) is None
    assert CountingStore.reads == 1  # cached, not 50 queries
    a.activate(KillScope.MODEL, "m", "incident", "admin")
    assert b.match({KillScope.MODEL: "m"}) is not None  # B sees it at once via the bus


def test_kill_switch_cache_backstop_ttl() -> None:
    from captain.core.identity.killswitch import InMemoryKillSwitchStore, KillScope, KillSwitch

    store = InMemoryKillSwitchStore()
    k = KillSwitch(store, bus=None, cache_ttl_s=0)  # no bus: TTL alone must pick it up
    assert k.match({KillScope.ORG: "o"}) is None
    other = KillSwitch(store)
    other.activate(KillScope.ORG, "o", "x", "admin")
    assert k.match({KillScope.ORG: "o"}) is not None


# 6 ------------------------------------------------------ rate vs budget order
def test_budget_refusal_does_not_consume_rate_slot() -> None:
    from captain.core.budget.model import RateLimitDefinition
    from captain.core.models import ContextHints, CostEstimate, Credential
    from tests.conftest import model_call
    from tests.harness.adapters import CaptainCoreHarness

    h = CaptainCoreHarness()
    h.register_key(
        "k", make_context("svc", RiskTier.TIER_3, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    ctx = h.resolve(Credential(kind="virtual_key", secret="k"), ContextHints())
    h.set_budget("org:org-1", 100)
    h.built.infra.budget_catalog.add_rate(
        RateLimitDefinition(f"principal:{ctx.principal.id}", requests_per_minute=1)
    )  # type: ignore[attr-defined]
    big = CostEstimate(input_tokens=1, max_output_tokens=1, amount=500, price_version="p")
    small = CostEstimate(input_tokens=1, max_output_tokens=1, amount=10, price_version="p")
    with pytest.raises(BudgetExceededError):
        h.core.reserve(h.core.decide(model_call(ctx)), big)  # refused for budget
    h.core.reserve(h.core.decide(model_call(ctx)), small)  # the one rate slot is still free
