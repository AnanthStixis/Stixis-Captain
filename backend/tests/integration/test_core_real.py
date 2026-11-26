"""End-to-end Captain Core with NO test doubles: real OPA server (local binary) +
real PostgreSQL stores + PostgreSQL event bus.

OPA binary: CAPTAIN_OPA_BIN, or <repo>/tools/opa/opa.exe (gitignored). Skipped if absent.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.engine import Engine

from captain.core.budget.model import BudgetDefinition, Period
from captain.core.context.service import Application, Project
from captain.core.factory import BuiltCore, CoreInfra, build_core
from captain.core.identity.model import PrincipalRecord
from captain.core.ledger.chain import Ed25519Signer, KeyRingVerifier
from captain.core.ledger.service import InMemoryWormStore
from captain.core.models import (
    ActionType,
    ActualUsage,
    ApprovalState,
    AutonomyLevel,
    Channel,
    ContextHints,
    CostEstimate,
    Credential,
    DecisionType,
    Environment,
    GovernanceRequest,
    ReasonCode,
    RegisterEntry,
    RiskTier,
    TokenUsage,
    UsageSource,
)
from captain.core.policy.opa import OpaPolicyEngine
from captain.db.bus import PostgresBus
from captain.db.stores import (
    PgApprovalStore,
    PgBudgetCatalog,
    PgBudgetStore,
    PgContextStore,
    PgIdentityStore,
    PgKillSwitchStore,
    PgLedgerStore,
    PgRegisterStore,
)
from tests.integration.conftest import URL

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]


def _opa_bin() -> str | None:
    cand = os.environ.get("CAPTAIN_OPA_BIN") or str(REPO / "tools" / "opa" / "opa.exe")
    return cand if Path(cand).exists() else shutil.which("opa")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def opa_url() -> Iterator[str]:
    binary = _opa_bin()
    if not binary:
        pytest.skip("OPA binary not found")
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603 — fixed args, local verified binary
        [binary, "run", "--server", f"--addr=127.0.0.1:{port}", "--log-level=error", "policies"],
        # Relative path on purpose: OPA reads "C:\..." as "<prefix>:<path>" on Windows.
        cwd=REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while True:
        try:
            if httpx.get(f"{url}/health?bundles", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail("OPA did not start")
        time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def real(pg: Engine, opa_url: str) -> Iterator[tuple[BuiltCore, str, str]]:
    assert URL
    bus = PostgresBus(URL)
    signer = Ed25519Signer("it")
    built = build_core(
        CoreInfra(
            pepper=b"i" * 32,
            policy=OpaPolicyEngine(opa_url, timeout_s=2.0),
            guard=None,
            signer=signer,
            verifier=KeyRingVerifier({"it": signer.public_key()}),
            approvers=lambda r: ("fin-1",),
            bus=bus,
            identity_store=PgIdentityStore(pg),
            context_store=PgContextStore(pg),
            register_store=PgRegisterStore(pg),
            killswitch_store=PgKillSwitchStore(pg),
            budget_catalog=PgBudgetCatalog(pg),
            budget_store=PgBudgetStore(pg),
            approval_store=PgApprovalStore(pg),
            ledger_store=PgLedgerStore(pg),
            worm=InMemoryWormStore(),
        )
    )
    ids, ctx_store = built.infra.identity_store, built.infra.context_store
    assert isinstance(ctx_store, PgContextStore)
    keys = {}
    for pid, autonomy in (("svc", AutonomyLevel.A2), ("agent", AutonomyLevel.A3)):
        ids.put_principal(
            PrincipalRecord(id=pid, type="APPLICATION", org_id="o1", display_name=pid, team_id="t1")
        )
        built.register.upsert(
            RegisterEntry(
                id=f"AI-{pid}",
                name=pid,
                risk_tier=RiskTier.TIER_2,
                autonomy=autonomy,
                owner_id="own",
                cost_centre="CC1",
                purpose="test",
                allowed_models=frozenset({"claude-sonnet"}),
                allowed_tools=frozenset({"jira.create_issue"}),
            )
        )
        ctx_store.put_project(Project("p1", "o1", "P", "t1"))
        ctx_store.put_application(Application(f"app-{pid}", "p1", f"AI-{pid}", pid))
        keys[pid] = built.core.m.identity.issue_key(
            pid, f"AI-{pid}", "p1", f"app-{pid}", Environment.DEVELOPMENT, None
        ).raw_key
    try:
        yield built, keys["svc"], keys["agent"]
    finally:
        bus.close()


def _ctx(built: BuiltCore, raw: str):  # type: ignore[no-untyped-def]
    return built.core.resolve(Credential(kind="virtual_key", secret=raw), ContextHints())


def _model(ctx, model: str = "claude-sonnet", max_tokens: int | None = 500) -> GovernanceRequest:  # type: ignore[no-untyped-def]
    return GovernanceRequest(
        trace_id="it",
        context=ctx,
        channel=Channel.API,
        action=ActionType.MODEL_CALL,
        model=model,
        max_output_tokens=max_tokens,
    )


def test_real_policy_decisions(real) -> None:  # type: ignore[no-untyped-def]
    built, svc, _ = real
    ctx = _ctx(built, svc)
    ok = built.core.decide(_model(ctx))
    assert (ok.decision, ok.policy_bundle, ok.policy_version) == (
        DecisionType.ALLOW,
        "captain-tier-policies",
        "0.1.0",
    )
    assert "captain.tier.TIER_2" in ok.matched_policy_ids
    capped = built.core.decide(_model(ctx, max_tokens=None))
    assert (
        capped.decision is DecisionType.ALLOW_WITH_LIMITS
        and capped.limits.max_output_tokens == 4096
    )
    bad = built.core.decide(_model(ctx, model="other"))
    assert (bad.decision, bad.reason_code) == (DecisionType.BLOCK, ReasonCode.MODEL_NOT_ALLOWED)


def test_real_budget_and_evidence(real) -> None:  # type: ignore[no-untyped-def]
    built, svc, _ = real
    built.infra.budget_catalog.add(BudgetDefinition("team:t1", Period.MONTHLY, 1_000))  # type: ignore[attr-defined]
    core = built.core
    ctx = _ctx(built, svc)
    d = core.decide(_model(ctx))
    r = core.reserve(
        d, CostEstimate(input_tokens=10, max_output_tokens=500, amount=800, price_version="p")
    )
    from captain.core.decision_api import BudgetExceededError

    with pytest.raises(BudgetExceededError):  # 800 reserved of 1000 → second call refused
        core.reserve(
            core.decide(_model(ctx)),
            CostEstimate(input_tokens=1, max_output_tokens=1, amount=300, price_version="p"),
        )
    core.settle(
        r,
        ActualUsage(
            usage=TokenUsage(input_tokens=10, output_tokens=40),
            source=UsageSource.PROVIDER_REPORTED,
            cost=120,
            price_version="p",
        ),
    )
    ledger = core.m.ledger
    assert ledger.write_pending() == 4  # allowed, allowed, budget refusal (UC-02), settled
    assert ledger.anchor() is not None
    assert ledger.verify().ok


def test_real_approval_flow(real) -> None:  # type: ignore[no-untyped-def]
    built, _, agent = real
    core = built.core
    ctx = _ctx(built, agent)
    req = GovernanceRequest(
        trace_id="it",
        context=ctx,
        channel=Channel.MCP,
        action=ActionType.TOOL_CALL,
        tool="jira.create_issue",
        tool_arguments={"summary": "x"},
    )
    d = core.decide(req)
    assert d.decision is DecisionType.REQUIRE_APPROVAL
    t = core.request_approval(req, d)
    core.decide_approval(t.ticket_id, True, "fin-1", "ok")
    assert core.consume_approval(t.ticket_id, req).state is ApprovalState.CONSUMED


def test_opa_down_fails_closed_for_tier_2(real, pg) -> None:  # type: ignore[no-untyped-def]
    built, svc, _ = real
    built.core.m.policy = OpaPolicyEngine(f"http://127.0.0.1:{_free_port()}", timeout_s=0.3)
    d = built.core.decide(_model(_ctx(built, svc)))
    assert (d.decision, d.reason_code) == (DecisionType.BLOCK, ReasonCode.CONTROL_UNAVAILABLE)


def test_registry_impact_reaches_real_opa(real) -> None:  # type: ignore[no-untyped-def]
    """Tier 3 + A3: a LOW-impact registered tool is allowed, a HIGH-impact one is held."""
    from captain.core.registry.service import (
        InMemoryRegistryStore,
        McpServer,
        RegistryService,
        ToolEntry,
        ToolImpact,
        ToolRisk,
    )

    built, _, agent = real
    reg = RegistryService(InMemoryRegistryStore())
    reg.upsert_server(McpServer("srv", "https://srv/mcp", "o"))
    reg.upsert_tool(ToolEntry("jira.create_issue", "srv", "issue", ToolImpact.LOW, ToolRisk.LOW))
    built.core.m.registry = reg
    entry = built.register.get("AI-agent")
    assert entry
    built.register.upsert(entry.model_copy(update={"risk_tier": RiskTier.TIER_1}))  # tighten first
    ctx = _ctx(built, agent)
    req = GovernanceRequest(
        trace_id="it",
        context=ctx,
        channel=Channel.MCP,
        action=ActionType.TOOL_CALL,
        tool="jira.create_issue",
        tool_arguments={},
    )
    assert (
        built.core.decide(req).decision is DecisionType.REQUIRE_APPROVAL
    )  # Tier 1: all A3 actions
    built.register.upsert(
        entry.model_copy(update={"risk_tier": RiskTier.TIER_3}), approval_ref="APR-1"
    )
    ctx = _ctx(built, agent)
    low = built.core.decide(req.model_copy(update={"context": ctx}))
    assert low.decision is DecisionType.ALLOW and low.risk_level is not None
    reg.upsert_tool(ToolEntry("jira.create_issue", "srv", "issue", ToolImpact.HIGH, ToolRisk.HIGH))
    assert (
        built.core.decide(req.model_copy(update={"context": ctx})).decision
        is DecisionType.REQUIRE_APPROVAL
    )
