"""Admin actions: permission-checked, and every one leaves a ledger event."""

from __future__ import annotations

import pytest

from captain.core.admin import AdminActor, AdminService
from captain.core.budget.model import BudgetDefinition, Period
from captain.core.decision_api import AuthenticationError, EvidenceUnavailableError
from captain.core.identity.killswitch import KillScope
from captain.core.identity.rbac import PermissionDenied
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import (
    AutonomyLevel,
    ContextHints,
    Credential,
    Environment,
    PrincipalType,
    RiskTier,
)
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness

ADMIN = AdminActor("admin-1", frozenset({"admin"}))
VIEWER = AdminActor("viewer-1", frozenset({"viewer"}))


def setup() -> tuple[CaptainCoreHarness, AdminService, InMemoryLedgerStore]:
    h = CaptainCoreHarness()
    h.register_key(
        "k", make_context("svc", RiskTier.TIER_2, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    store.outbox.clear()
    return h, AdminService(h.core, h.built.register, h.built.infra.budget_catalog), store  # type: ignore[arg-type]


def events(store: InMemoryLedgerStore) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
    return [(t, b) for (_p, t, b) in store.outbox.values()]


def test_every_admin_action_is_recorded_with_actor() -> None:
    h, admin, store = setup()
    issued = admin.issue_key(
        ADMIN, "svc", "AI-svc", "proj-1", "app-svc", Environment.DEVELOPMENT, None, "ci"
    )
    rotated = admin.rotate_key(ADMIN, issued.record.key_id)
    admin.revoke_key(ADMIN, rotated.record.key_id, "leak")
    admin.kill(ADMIN, KillScope.MODEL, "claude-sonnet", "incident")
    admin.unkill(ADMIN, KillScope.MODEL, "claude-sonnet", "resolved")
    entry = h.built.register.get("AI-svc")
    assert entry
    admin.upsert_register(ADMIN, entry.model_copy(update={"risk_tier": RiskTier.TIER_1}))
    admin.define_budget(ADMIN, BudgetDefinition("team:team-1", Period.MONTHLY, 1000))
    admin.override_budget(ADMIN, "team:team-1", 500, "quarter end")
    admin.record_policy_published(
        ADMIN, "captain-tier-policies", "0.2.0", "ab" * 32, "captain-policy"
    )
    types = [t for t, _ in events(store)]
    assert types == [
        "key.issued", "key.rotated", "key.revoked", "killswitch.changed", "killswitch.changed",
        "register.changed", "budget.defined", "budget.override", "alert.raised", "policy.changed",
    ]  # fmt: skip
    for t_, body in events(store):
        assert body["principal_id"] == ("captain" if t_ == "alert.raised" else "admin-1")
    issued_body = events(store)[0][1]
    assert issued.raw_key not in str(issued_body)  # raw key never in evidence
    assert issued_body["register_id"] == "AI-svc"  # lands in the system's own partition


def test_permissions_enforced() -> None:
    _, admin, store = setup()
    with pytest.raises(PermissionDenied):
        admin.kill(VIEWER, KillScope.ORG, "org-1", "x")
    with pytest.raises(PermissionDenied):
        admin.override_budget(VIEWER, "team:team-1", 1, "x")
    assert events(store) == []


def test_safety_decreasing_actions_refused_without_evidence() -> None:
    h, admin, store = setup()
    admin.kill(ADMIN, KillScope.MODEL, "claude-sonnet", "incident")
    store.available = False
    with pytest.raises(EvidenceUnavailableError):
        admin.unkill(ADMIN, KillScope.MODEL, "claude-sonnet", "resolved")
    assert h.core.m.killswitch.match({KillScope.MODEL: "claude-sonnet"}) is not None  # still killed
    h.built.infra.budget_catalog.add(BudgetDefinition("team:team-1", Period.MONTHLY, 10))  # type: ignore[attr-defined]
    with pytest.raises(EvidenceUnavailableError):
        admin.override_budget(ADMIN, "team:team-1", 999, "no trace")
    entry = h.built.register.get("AI-svc")
    assert entry
    with pytest.raises(EvidenceUnavailableError):
        admin.upsert_register(
            ADMIN, entry.model_copy(update={"risk_tier": RiskTier.TIER_4}), "APR-1"
        )
    assert h.built.register.get("AI-svc").risk_tier is RiskTier.TIER_2  # type: ignore[union-attr]


def test_safety_increasing_actions_apply_even_without_evidence() -> None:
    h, admin, store = setup()
    ctx = h.resolve(Credential(kind="virtual_key", secret="k"), ContextHints())
    store.available = False
    with pytest.raises(EvidenceUnavailableError):
        admin.kill(ADMIN, KillScope.KEY, ctx.key_id or "", "leak")
    with pytest.raises(AuthenticationError):  # the key is revoked anyway
        h.resolve(Credential(kind="virtual_key", secret="k"), ContextHints())


def test_issued_key_revoked_if_issue_cannot_be_recorded() -> None:
    h, admin, store = setup()
    store.available = False
    with pytest.raises(EvidenceUnavailableError):
        admin.issue_key(ADMIN, "svc", "AI-svc", "proj-1", "app-svc", Environment.DEVELOPMENT, None)
    from captain.core.identity.model import KeyStatus

    keys = h.built.infra.identity_store.keys_for_principal("svc")
    assert any(
        k.status is KeyStatus.REVOKED and k.revoked_reason == "issue not recorded" for k in keys
    )


def test_approval_decision_requires_permission_and_is_recorded() -> None:
    from tests.conftest import tool_call

    h = CaptainCoreHarness()
    h.register_key(
        "a", make_context("agent", RiskTier.TIER_2, AutonomyLevel.A3, PrincipalType.AGENT)
    )
    admin = AdminService(h.core, h.built.register, h.built.infra.budget_catalog)  # type: ignore[arg-type]
    ctx = h.resolve(Credential(kind="virtual_key", secret="a"), ContextHints())
    req = tool_call(ctx, {"x": 1})
    t = h.core.request_approval(req, h.core.decide(req))
    with pytest.raises(PermissionDenied):
        admin.decide_approval(AdminActor("fin-1", frozenset({"viewer"})), t.ticket_id, True, "ok")
    admin.decide_approval(AdminActor("fin-1", frozenset({"approver"})), t.ticket_id, True, "ok")
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    assert "approval.decided" in [t for (_p, t, _b) in store.outbox.values()]


def test_registry_changes_recorded_before_applied() -> None:
    from captain.core.registry.service import (
        InMemoryRegistryStore,
        McpServer,
        Provider,
        ProviderKind,
        RegistryService,
        ToolEntry,
        ToolImpact,
        ToolRisk,
    )

    h, admin, store = setup()
    h.core.m.registry = RegistryService(InMemoryRegistryStore())
    admin.upsert_registry(ADMIN, Provider("p1", ProviderKind.OPENAI, "us", "vault:openai"))
    admin.upsert_registry(ADMIN, McpServer("erp", "https://erp/mcp", "o1"))
    admin.upsert_registry(
        ADMIN, ToolEntry("erp.pay", "erp", "Pay", ToolImpact.HIGH, ToolRisk.CRITICAL)
    )
    kinds = [b["details"]["kind"] for t, b in events(store) if t == "registry.changed"]
    assert kinds == ["provider", "server", "tool"]
    store.available = False
    with pytest.raises(EvidenceUnavailableError):
        admin.upsert_registry(
            ADMIN, ToolEntry("erp.refund", "erp", "Refund", ToolImpact.HIGH, ToolRisk.HIGH)
        )
    assert h.core.m.registry.usable_tool("erp.refund") is None  # not applied without evidence
    with pytest.raises(PermissionDenied):
        admin.upsert_registry(VIEWER, Provider("p2", ProviderKind.OPENAI, "us", "v"))
