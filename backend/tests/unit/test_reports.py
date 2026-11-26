from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import (
    ActualUsage,
    AutonomyLevel,
    ContextHints,
    CostEstimate,
    Credential,
    PrincipalType,
    RiskTier,
    TokenUsage,
    UsageSource,
)
from captain.core.usage.reports import (
    ProviderCost,
    chargeback,
    parse_provider_csv,
    reconcile,
    settled_costs,
    to_csv,
)
from tests.conftest import make_context, model_call
from tests.harness.adapters import CaptainCoreHarness

TODAY = datetime.now(UTC).date()  # ledger timestamps are UTC


def run_calls(costs: list[tuple[int, UsageSource]]) -> list:  # type: ignore[type-arg]
    h = CaptainCoreHarness()
    h.register_key(
        "k", make_context("svc", RiskTier.TIER_3, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    ctx = h.resolve(Credential(kind="virtual_key", secret="k"), ContextHints())
    for cost, src in costs:
        d = h.core.decide(model_call(ctx))
        r = h.core.reserve(
            d, CostEstimate(input_tokens=1, max_output_tokens=1, amount=10_000, price_version="p")
        )
        h.core.settle(
            r,
            ActualUsage(
                usage=TokenUsage(input_tokens=5, output_tokens=5),
                source=src,
                cost=cost,
                price_version="p",
                provider="anthropic",
            ),
        )
    h.core.m.ledger.write_pending()
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    return settled_costs(store.rows)


def test_chargeback_groups_by_attribution() -> None:
    costs = run_calls([(1000, UsageSource.PROVIDER_REPORTED), (500, UsageSource.ESTIMATED)])
    rows = chargeback(costs, TODAY, TODAY)
    assert rows == [
        {
            "cost_centre": "CC-100",
            "team": "team-1",
            "project": "proj-1",
            "model": "claude-sonnet",
            "cost_micros": 1500,
            "calls": 2,
            "estimated_cost_micros": 500,
        }
    ]
    csv = to_csv(rows)
    assert csv.splitlines()[0].startswith("cost_centre,team,project,model,cost_micros")


def test_reconcile_within_threshold_and_exceptions() -> None:
    costs = run_calls([(10_000, UsageSource.PROVIDER_REPORTED)])
    ok = reconcile(costs, [ProviderCost(TODAY, "anthropic", "claude-sonnet", 10_150)])
    assert ok[0].exception is None and ok[0].variance_pct == pytest.approx(-1.4778, rel=1e-3)
    bad = reconcile(costs, [ProviderCost(TODAY, "anthropic", "claude-sonnet", 11_000)])
    assert bad[0].exception == "VARIANCE"
    missing = reconcile(costs, [ProviderCost(TODAY, "openai", "gpt-x", 50)])
    assert {line.exception for line in missing} == {"MISSING_IN_PROVIDER", "MISSING_IN_CAPTAIN"}


def test_provider_csv() -> None:
    rows = parse_provider_csv(
        "day,provider,model,cost_micros\n2026-11-01,anthropic,claude-sonnet,1200\n"
    )
    assert rows == [ProviderCost(date(2026, 11, 1), "anthropic", "claude-sonnet", 1200)]
    with pytest.raises(ValueError):
        parse_provider_csv("day,cost\n2026-11-01,5\n")
