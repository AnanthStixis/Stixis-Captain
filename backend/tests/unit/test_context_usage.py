from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from captain.core.bus import InMemoryBus
from captain.core.context.service import (
    Application,
    ContextService,
    ControlDowngradeError,
    InMemoryContextStore,
    LocalRegisterProvider,
    Project,
)
from captain.core.decision_api import ContextUnresolvedError
from captain.core.identity.model import KeyRecord, PrincipalRecord
from captain.core.models import (
    AutonomyLevel,
    ContextHints,
    Environment,
    RegisterEntry,
    RiskTier,
    TokenUsage,
)
from captain.core.usage.normalize import from_anthropic, from_openai, normalize
from captain.core.usage.pricing import (
    ModelPrice,
    PriceCatalog,
    PriceNotFoundError,
    cost_of,
    estimate_cost,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def entry(
    tier: RiskTier = RiskTier.TIER_2, autonomy: AutonomyLevel = AutonomyLevel.A2
) -> RegisterEntry:
    return RegisterEntry(
        id="AI-1", name="n", risk_tier=tier, autonomy=autonomy, owner_id="o",
        cost_centre="CC", purpose="p",
    )  # fmt: skip


@pytest.fixture
def downgrades() -> list[tuple[RegisterEntry, RegisterEntry]]:
    return []


@pytest.fixture
def register(downgrades: list[tuple[RegisterEntry, RegisterEntry]]) -> LocalRegisterProvider:
    r = LocalRegisterProvider(InMemoryBus(), lambda o, n: downgrades.append((o, n)))
    r.upsert(entry())
    return r


@pytest.fixture
def ctx_service(register: LocalRegisterProvider) -> ContextService:
    s = InMemoryContextStore()
    s.projects["p1"] = Project(id="p1", org_id="o1", name="P", team_id="t1")
    s.applications["a1"] = Application(id="a1", project_id="p1", register_id="AI-1", name="A")
    return ContextService(register, s)


KEY = KeyRecord(
    key_id="k1", secret_hash="h", principal_id="u1", register_id="AI-1", project_id="p1",
    application_id="a1", environment=Environment.DEVELOPMENT, created_at=T0,
)  # fmt: skip
PRINCIPAL = PrincipalRecord(id="u1", type="USER", org_id="o1", display_name="U")


def test_resolve_ok(ctx_service: ContextService) -> None:
    c = ctx_service.resolve(KEY, PRINCIPAL, ContextHints())
    assert (c.project_id, c.application_id, c.principal.team_id) == ("p1", "a1", "t1")


@pytest.mark.parametrize(
    "hints",
    [
        ContextHints(project_id="p2"),
        ContextHints(application_id="a2"),
        ContextHints(environment=Environment.PRODUCTION),
    ],
)
def test_conflicting_hints_refused(ctx_service: ContextService, hints: ContextHints) -> None:
    with pytest.raises(ContextUnresolvedError):
        ctx_service.resolve(KEY, PRINCIPAL, hints)


def test_cross_org_refused(ctx_service: ContextService) -> None:
    other = PrincipalRecord(id="u1", type="USER", org_id="o2", display_name="U")
    with pytest.raises(ContextUnresolvedError):
        ctx_service.resolve(KEY, other, ContextHints())


def test_retired_register_entry_refused(
    ctx_service: ContextService, register: LocalRegisterProvider
) -> None:
    register.upsert(entry().model_copy(update={"active": False}))
    with pytest.raises(ContextUnresolvedError):
        ctx_service.resolve(KEY, PRINCIPAL, ContextHints())


def test_downgrade_needs_approval_and_alerts(
    register: LocalRegisterProvider, downgrades: list[tuple[RegisterEntry, RegisterEntry]]
) -> None:
    with pytest.raises(ControlDowngradeError):
        register.upsert(entry(tier=RiskTier.TIER_4))
    with pytest.raises(ControlDowngradeError):
        register.upsert(entry(autonomy=AutonomyLevel.A0))
    updated = register.upsert(entry(tier=RiskTier.TIER_4), approval_ref="APR-1")
    assert updated.version == 2 and len(downgrades) == 1
    register.upsert(entry(tier=RiskTier.TIER_1))  # tightening needs nothing


def test_high_risk_floor() -> None:
    with pytest.raises(ValueError):
        RegisterEntry(
            id="x", name="x", risk_tier=RiskTier.TIER_3, autonomy=AutonomyLevel.A1,
            owner_id="o", cost_centre="c", purpose="p", eu_ai_act_high_risk=True,
        )  # fmt: skip


# ------------------------------------------------------------------ pricing

PRICE = ModelPrice(
    model="claude-sonnet", provider="anthropic", version="2026-01", effective_from=T0,
    input_per_m=3_000_000, output_per_m=15_000_000, cached_input_per_m=300_000,
)  # fmt: skip


def test_cost_exact_integer_micro_units() -> None:
    # 1000 in @ $3/M = $0.003 = 3000 µ; 500 out @ $15/M = 7500 µ
    assert cost_of(TokenUsage(input_tokens=1000, output_tokens=500), PRICE) == 10_500


def test_cached_tokens_discounted() -> None:
    u = TokenUsage(input_tokens=1000, output_tokens=0, cached_input_tokens=800)
    assert cost_of(u, PRICE) == 200 * 3 + 800 * 3 // 10  # 600 + 240


def test_rounds_up_never_down() -> None:
    assert cost_of(TokenUsage(input_tokens=1, output_tokens=0), PRICE) == 3
    cheap = ModelPrice("m", "p", "v", T0, input_per_m=1, output_per_m=1)
    assert cost_of(TokenUsage(input_tokens=1), cheap) == 1


def test_estimate_is_upper_bound() -> None:
    e = estimate_cost(PRICE, input_tokens=1000, max_output_tokens=1000)
    assert e.amount >= cost_of(TokenUsage(input_tokens=1000, output_tokens=200), PRICE)
    assert e.price_version == "2026-01"


def test_price_versions() -> None:
    cat = PriceCatalog()
    cat.add(PRICE)
    later = ModelPrice("claude-sonnet", "anthropic", "2026-06", T0 + timedelta(days=180), 1, 1)
    cat.add(later)
    assert cat.price_at("claude-sonnet", T0 + timedelta(days=1)).version == "2026-01"
    assert cat.price_at("claude-sonnet", T0 + timedelta(days=200)).version == "2026-06"
    assert cat.by_version("claude-sonnet", "2026-01") is PRICE
    with pytest.raises(PriceNotFoundError):
        cat.price_at("claude-sonnet", T0 - timedelta(days=1))
    with pytest.raises(ValueError):
        cat.add(PRICE)


def test_normalize_openai() -> None:
    u = from_openai(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 20},
        }
    )
    assert (u.input_tokens, u.output_tokens, u.cached_input_tokens, u.reasoning_tokens) == (
        100, 50, 40, 20,
    )  # fmt: skip


def test_normalize_anthropic_includes_cache_reads_in_input() -> None:
    u, writes = from_anthropic(
        {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90,
         "cache_creation_input_tokens": 30}
    )  # fmt: skip
    assert (u.input_tokens, u.cached_input_tokens, writes) == (100, 90, 30)


def test_unknown_provider_is_not_guessed() -> None:
    assert normalize("mystery", {"tokens": 5}) is None
    assert normalize("openai", None) is None
