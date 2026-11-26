from __future__ import annotations

import pytest

from captain.core.models import (
    ActionType,
    AutonomyLevel,
    Channel,
    ContentRef,
    ContextHints,
    Credential,
    DecisionType,
    FindingType,
    GovernanceRequest,
    GuardrailFinding,
    PrincipalType,
    ReasonCode,
    RiskLevel,
    RiskTier,
)
from captain.core.registry.service import (
    InMemoryRegistryStore,
    McpServer,
    ModelEntry,
    Provider,
    ProviderKind,
    RegistryError,
    RegistryService,
    Route,
    ToolEntry,
    ToolImpact,
    ToolRisk,
)
from captain.core.risk import ToolFacts, assess, level_for
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness

PO_SCHEMA = {
    "type": "object",
    "properties": {"amount": {"type": "integer", "maximum": 10000}, "vendor": {"type": "string"}},
    "required": ["amount", "vendor"],
    "additionalProperties": False,
}


def registry() -> RegistryService:
    r = RegistryService(InMemoryRegistryStore())
    r.upsert_provider(Provider("anthropic-eu", ProviderKind.ANTHROPIC, "eu", "vault:anthropic"))
    r.upsert_provider(Provider("openai-us", ProviderKind.OPENAI, "us", "vault:openai"))
    r.upsert_model(ModelEntry("claude-sonnet", "anthropic-eu", "claude-sonnet-5-5", "eu"))
    r.upsert_model(ModelEntry("gpt-standard", "openai-us", "gpt-x", "us"))
    r.upsert_route(Route("chat-default", ("claude-sonnet", "gpt-standard")))
    r.upsert_server(McpServer("erp", "https://erp.internal/mcp", "owner-1"))
    r.upsert_tool(
        ToolEntry(
            "erp.create_po",
            "erp",
            "Create purchase order",
            ToolImpact.HIGH,
            ToolRisk.HIGH,
            PO_SCHEMA,
        )
    )
    r.upsert_tool(
        ToolEntry("jira.create_issue", "erp", "Create issue", ToolImpact.LOW, ToolRisk.LOW)
    )
    return r


# ------------------------------------------------------------------ registry


def test_route_order_and_fallback_when_provider_disabled() -> None:
    r = registry()
    assert [t.alias for t in r.resolve_route("chat-default")] == ["claude-sonnet", "gpt-standard"]
    r.upsert_provider(
        Provider("anthropic-eu", ProviderKind.ANTHROPIC, "eu", "vault:anthropic", active=False)
    )
    assert [t.alias for t in r.resolve_route("chat-default")] == ["gpt-standard"]
    assert r.resolve_route("claude-sonnet") == []
    assert r.resolve_route("gpt-standard")[0].model.provider_model == "gpt-x"


def test_registry_rejects_broken_entries() -> None:
    r = registry()
    with pytest.raises(RegistryError):
        r.upsert_model(ModelEntry("x", "no-such-provider", "m", "eu"))
    with pytest.raises(RegistryError):
        r.upsert_route(Route("r", ("not-registered",)))
    with pytest.raises(RegistryError):
        r.upsert_tool(ToolEntry("t", "no-server", "d", ToolImpact.LOW, ToolRisk.LOW))
    with pytest.raises(RegistryError):
        r.upsert_tool(
            ToolEntry("t", "erp", "d", ToolImpact.LOW, ToolRisk.LOW, {"type": "nonsense"})
        )


def test_argument_validation_never_echoes_values() -> None:
    r = registry()
    t = r.usable_tool("erp.create_po")
    assert t is not None
    assert r.validate_arguments(t, {"amount": 500, "vendor": "Acme"}) == []
    errs = r.validate_arguments(t, {"amount": 50_000, "vendor": "SECRET-VENDOR", "extra": 1})
    assert errs and "amount: maximum violated" in errs
    assert not any("SECRET-VENDOR" in e or "50000" in e for e in errs)


def test_disabled_server_disables_its_tools() -> None:
    r = registry()
    r.upsert_server(McpServer("erp", "https://erp.internal/mcp", "owner-1", active=False))
    assert r.usable_tool("erp.create_po") is None


# ---------------------------------------------------------------------- risk


def _req(ctx, action=ActionType.MODEL_CALL, contents=(), tool=None):  # type: ignore[no-untyped-def]
    return GovernanceRequest(
        trace_id="t",
        context=ctx,
        channel=Channel.API,
        action=action,
        model="claude-sonnet" if action is ActionType.MODEL_CALL else None,
        tool=tool,
        contents=contents,
    )


def f(t: FindingType, score: float = 1.0, entity: str = "x") -> GuardrailFinding:
    return GuardrailFinding(type=t, entity=entity, detector="d", content_index=0, score=score)


def test_risk_levels_and_factors() -> None:
    c4 = make_context("a", RiskTier.TIER_4, AutonomyLevel.A1, PrincipalType.APPLICATION)
    low = assess(_req(c4), (), None)
    assert (low.level, low.score) == (RiskLevel.LOW, 5) and low.factors == ("tier:TIER_4(+5)",)

    c2 = make_context("b", RiskTier.TIER_2, AutonomyLevel.A3, PrincipalType.AGENT)
    risky = assess(
        _req(c2, ActionType.TOOL_CALL, (ContentRef(text="doc", trusted=False),), "erp.create_po"),
        (f(FindingType.INJECTION, 0.9, "indirect_prompt_injection"), f(FindingType.SECRET)),
        ToolFacts("high", "HIGH", False),
    )
    assert risky.level is RiskLevel.CRITICAL and risky.score == 100  # capped
    assert {"finding:secret(+30)", "tool:high_impact(+20)", "autonomy:A3(+15)"} <= set(
        risky.factors
    )


def test_low_injection_score_does_not_count() -> None:
    c = make_context("a", RiskTier.TIER_3, AutonomyLevel.A1, PrincipalType.APPLICATION)
    assert assess(_req(c), (f(FindingType.INJECTION, 0.2),), None).score == 15


def test_read_only_high_impact_tool_scores_lower() -> None:
    c = make_context("a", RiskTier.TIER_3, AutonomyLevel.A3, PrincipalType.AGENT)
    rw = assess(_req(c, ActionType.TOOL_CALL, tool="t"), (), ToolFacts("high", "LOW", False))
    ro = assess(_req(c, ActionType.TOOL_CALL, tool="t"), (), ToolFacts("high", "LOW", True))
    assert rw.score - ro.score == 20


@pytest.mark.parametrize(
    ("score", "level"), [(0, "LOW"), (24, "LOW"), (25, "MEDIUM"), (50, "HIGH"), (75, "CRITICAL")]
)
def test_thresholds(score: int, level: str) -> None:
    assert level_for(score).value == level


# ------------------------------------------------------------------- engine


def engine_with_registry() -> tuple[CaptainCoreHarness, object, object]:
    h = CaptainCoreHarness()
    h.core.m.registry = registry()
    agent = make_context("agent", RiskTier.TIER_2, AutonomyLevel.A2, PrincipalType.AGENT)
    agent = agent.model_copy(
        update={
            "register_entry": agent.register_entry.model_copy(
                update={
                    "allowed_tools": frozenset({"erp.create_po", "jira.create_issue"}),
                    "allowed_models": frozenset({"claude-sonnet", "unregistered-model"}),
                }
            )
        }
    )
    h.register_key("a", agent)
    ctx = h.resolve(Credential(kind="virtual_key", secret="a"), ContextHints())
    return h, ctx, agent


def test_engine_blocks_unregistered_model_and_tool_and_bad_args() -> None:
    h, ctx, _ = engine_with_registry()
    core = h.core
    m = GovernanceRequest(
        trace_id="t",
        context=ctx,
        channel=Channel.API,
        action=ActionType.MODEL_CALL,
        model="unregistered-model",
    )
    assert core.decide(m).reason_code is ReasonCode.MODEL_NOT_REGISTERED

    def tool(name: str, args: dict[str, object]) -> GovernanceRequest:
        return GovernanceRequest(
            trace_id="t",
            context=ctx,
            channel=Channel.MCP,
            action=ActionType.TOOL_CALL,
            tool=name,
            tool_arguments=args,
        )

    assert core.decide(tool("unknown.tool", {})).reason_code is ReasonCode.TOOL_NOT_REGISTERED
    bad = core.decide(tool("erp.create_po", {"amount": 99_999, "vendor": "x"}))
    assert bad.reason_code is ReasonCode.TOOL_ARGUMENTS_INVALID and "amount" in bad.explanation
    ok = core.decide(tool("erp.create_po", {"amount": 100, "vendor": "x"}))
    assert ok.decision is DecisionType.ALLOW
    assert ok.risk_level is RiskLevel.HIGH and "tool:high_impact(+20)" in ok.risk_factors


def test_every_decision_carries_risk_and_ledger_records_it() -> None:
    from captain.core.ledger.service import InMemoryLedgerStore

    h, ctx, _ = engine_with_registry()
    d = h.core.decide(
        GovernanceRequest(
            trace_id="t",
            context=ctx,
            channel=Channel.API,
            action=ActionType.MODEL_CALL,
            model="claude-sonnet",
        )
    )
    assert d.risk_level is RiskLevel.MEDIUM and d.risk_score == 30
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    body = list(store.outbox.values())[-1][2]
    assert body["details"]["risk_level"] == "MEDIUM" and body["details"]["risk_score"] == 30


def test_route_alias_is_callable_when_any_target_enabled() -> None:
    r = registry()
    assert r.is_callable("chat-default") and r.is_callable("claude-sonnet")
    assert not r.is_callable("nothing")
    r.upsert_provider(
        Provider("anthropic-eu", ProviderKind.ANTHROPIC, "eu", "vault:anthropic", active=False)
    )
    r.upsert_provider(
        Provider("openai-us", ProviderKind.OPENAI, "us", "vault:openai", active=False)
    )
    assert not r.is_callable("chat-default")  # every target disabled
