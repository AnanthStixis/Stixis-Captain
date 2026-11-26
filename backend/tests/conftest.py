"""Shared fixtures. Any CoreDecisionAPI implementation can be plugged into `core`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from captain.core.models import (
    ActionType,
    AutonomyLevel,
    Channel,
    ContextHints,
    Credential,
    Environment,
    GovernanceRequest,
    Principal,
    PrincipalType,
    RegisterEntry,
    ResolvedContext,
    RiskTier,
)
from captain.core.stub import InMemoryCore

APP_KEY = "cpt_test_app_secret"  # test value only
AGENT_KEY = "cpt_test_agent_secret"  # test value only


def make_context(
    principal_id: str, tier: RiskTier, autonomy: AutonomyLevel, ptype: PrincipalType
) -> ResolvedContext:
    return ResolvedContext(
        principal=Principal(id=principal_id, type=ptype, org_id="org-1", team_id="team-1"),
        register_entry=RegisterEntry(
            id=f"AI-{principal_id}",
            name=principal_id,
            risk_tier=tier,
            autonomy=autonomy,
            owner_id="owner-1",
            cost_centre="CC-100",
            purpose="test",
            allowed_models=frozenset({"claude-sonnet"}),
            allowed_tools=frozenset({"jira.create_issue"}),
        ),
        project_id="proj-1",
        application_id=f"app-{principal_id}",
        environment=Environment.DEVELOPMENT,
    )


@pytest.fixture(params=["stub", "captain"])
def core_factory(request: pytest.FixtureRequest) -> Callable[..., Any]:
    """Every contract test runs against the stub AND the real CaptainCore."""
    from tests.harness.adapters import CaptainCoreHarness

    def make(approval_ttl: timedelta = timedelta(minutes=30)) -> Any:
        c: Any = (
            InMemoryCore(approval_ttl=approval_ttl)
            if request.param == "stub"
            else CaptainCoreHarness(approval_ttl=approval_ttl)
        )
        c.register_key(
            APP_KEY,
            make_context(
                "fraud-triage", RiskTier.TIER_2, AutonomyLevel.A2, PrincipalType.APPLICATION
            ),
        )
        c.register_key(
            AGENT_KEY,
            make_context("procurement", RiskTier.TIER_2, AutonomyLevel.A3, PrincipalType.AGENT),
        )
        return c

    return make


@pytest.fixture
def core(core_factory: Callable[..., Any]) -> Any:
    return core_factory()


@pytest.fixture
def app_ctx(core: Any) -> ResolvedContext:
    return core.resolve(Credential(kind="virtual_key", secret=APP_KEY), ContextHints())


@pytest.fixture
def agent_ctx(core: Any) -> ResolvedContext:
    return core.resolve(Credential(kind="virtual_key", secret=AGENT_KEY), ContextHints())


def model_call(ctx: ResolvedContext, model: str = "claude-sonnet") -> GovernanceRequest:
    return GovernanceRequest(
        trace_id="t-1", context=ctx, channel=Channel.API, action=ActionType.MODEL_CALL, model=model
    )


def tool_call(ctx: ResolvedContext, args: dict[str, object]) -> GovernanceRequest:
    return GovernanceRequest(
        trace_id="t-2",
        context=ctx,
        channel=Channel.MCP,
        action=ActionType.TOOL_CALL,
        tool="jira.create_issue",
        tool_arguments=args,
    )
