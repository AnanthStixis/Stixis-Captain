"""Seed a local development dataset:  python -m captain.devseed

Creates (idempotently) one demo application and everything it needs, through the
audited admin path so the ledger records it:
- principal `dev-app`, AI Register entry `AI-DEV` (Tier 3, A2), project and application
- registry: provider `anthropic-dev` + model alias `claude-sonnet`
- organisation budget (monthly) so calls are capped
- a virtual key, saved to backend/.dev/dev-app.key (never printed)

Development only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from captain.config import build_from_env, load_settings
from captain.core.admin import AdminActor, AdminService
from captain.core.budget.model import BudgetDefinition, Period
from captain.core.context.service import Application, Project
from captain.core.identity.model import PrincipalRecord
from captain.core.models import AutonomyLevel, Environment, RegisterEntry, RiskTier
from captain.core.registry.service import (
    McpServer,
    ModelEntry,
    Provider,
    ProviderKind,
    ToolEntry,
    ToolImpact,
    ToolRisk,
)
from captain.core.usage.pricing import ModelPrice
from captain.db.stores import PgContextStore, save_price
from captain.devsetup import DEV

SEED_ADMIN = AdminActor("dev-seed", frozenset({"admin"}))


def infra_engine() -> Engine:
    return create_engine(load_settings().database_url)


def seed_mcp(admin: AdminService, built: Any, infra: Any) -> None:
    """A demo agent and a mock MCP server (python -m uvicorn --factory
    captain.devmcp:create_dev_mcp_from_env --port 8310). The agent is Tier 3, autonomy
    A3: reads run freely, the high-impact `create_issue` waits for a human approval."""
    infra.identity_store.put_principal(
        PrincipalRecord(
            id="dev-agent",
            type="AGENT",
            org_id="dev-org",
            display_name="Dev agent",
            team_id="dev-team",
        )
    )
    admin.upsert_registry(
        SEED_ADMIN,
        McpServer(
            "demo-mcp",
            "http://127.0.0.1:8310/mcp",
            "dev-owner",
            credential_ref="env:CAPTAIN_MOCK_MCP_KEY",
        ),
    )
    admin.upsert_registry(
        SEED_ADMIN,
        ToolEntry(
            "demo.search_issues",
            "demo-mcp",
            "Search issues",
            ToolImpact.LOW,
            ToolRisk.LOW,
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            read_only=True,
            upstream_name="search_issues",
        ),
    )
    admin.upsert_registry(
        SEED_ADMIN,
        ToolEntry(
            "demo.create_issue",
            "demo-mcp",
            "Create an issue",
            ToolImpact.HIGH,
            ToolRisk.MEDIUM,
            {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
            upstream_name="create_issue",
        ),
    )
    if built.register.get("AI-AGENT") is None:
        admin.upsert_register(
            SEED_ADMIN,
            RegisterEntry(
                id="AI-AGENT",
                name="Developer agent",
                risk_tier=RiskTier.TIER_3,
                autonomy=AutonomyLevel.A3,
                owner_id="dev-owner",
                cost_centre="CC-DEV",
                purpose="Local MCP development",
                allowed_tools=frozenset({"demo.search_issues", "demo.create_issue"}),
            ),
        )
    infra.context_store.put_application(
        Application("dev-agent-application", "dev-project", "AI-AGENT", "Dev agent application")
    )
    key_file = DEV / "dev-agent.key"
    if not key_file.exists():
        issued = admin.issue_key(
            SEED_ADMIN,
            "dev-agent",
            "AI-AGENT",
            "dev-project",
            "dev-agent-application",
            Environment.DEVELOPMENT,
            None,
            "dev seed",
        )
        key_file.write_text(issued.raw_key, encoding="utf-8")


def seed_claude_code(admin: AdminService, built: Any, infra: Any) -> None:
    """A developer using Claude Code with the Captain hook (ide/claude-code/captain_hook.py).
    Local tools are registered as `claudecode.<Tool>` on a virtual server. Tier 3, A3:
    reads and edits run, `Bash` (high impact) waits for approval. Tools not registered
    here (Task, MCP tools, ...) are refused until someone registers them."""
    infra.identity_store.put_principal(
        PrincipalRecord(
            id="dev-coder",
            type="USER",
            org_id="dev-org",
            display_name="Dev coder",
            team_id="dev-team",
        )
    )
    admin.upsert_registry(
        SEED_ADMIN, McpServer("claude-code-local", "local://claude-code", "dev-owner")
    )
    tools = {
        "Read": (ToolImpact.LOW, ToolRisk.LOW, True),
        "Glob": (ToolImpact.LOW, ToolRisk.LOW, True),
        "Grep": (ToolImpact.LOW, ToolRisk.LOW, True),
        "Edit": (ToolImpact.LOW, ToolRisk.MEDIUM, False),
        "Write": (ToolImpact.LOW, ToolRisk.MEDIUM, False),
        "Bash": (ToolImpact.HIGH, ToolRisk.HIGH, False),
    }
    for name, (impact, risk, read_only) in tools.items():
        admin.upsert_registry(
            SEED_ADMIN,
            ToolEntry(
                f"claudecode.{name}",
                "claude-code-local",
                f"Claude Code {name}",
                impact,
                risk,
                read_only=read_only,
            ),
        )
    if built.register.get("AI-CODER") is None:
        admin.upsert_register(
            SEED_ADMIN,
            RegisterEntry(
                id="AI-CODER",
                name="Developer coding assistant",
                risk_tier=RiskTier.TIER_3,
                autonomy=AutonomyLevel.A3,
                owner_id="dev-owner",
                cost_centre="CC-DEV",
                purpose="Local Claude Code hook development",
                allowed_tools=frozenset(f"claudecode.{n}" for n in tools),
            ),
        )
    infra.context_store.put_application(
        Application("dev-coder-application", "dev-project", "AI-CODER", "Dev coder application")
    )
    key_file = DEV / "dev-coder.key"
    if not key_file.exists():
        issued = admin.issue_key(
            SEED_ADMIN,
            "dev-coder",
            "AI-CODER",
            "dev-project",
            "dev-coder-application",
            Environment.DEVELOPMENT,
            None,
            "dev seed",
        )
        key_file.write_text(issued.raw_key, encoding="utf-8")


def main() -> None:
    built, _ = build_from_env()
    core, infra = built.core, built.infra
    admin = AdminService(core, built.register, infra.budget_catalog)  # type: ignore[arg-type]

    infra.identity_store.put_principal(
        PrincipalRecord(
            id="dev-app",
            type="APPLICATION",
            org_id="dev-org",
            display_name="Dev app",
            team_id="dev-team",
        )
    )
    reg = core.m.registry
    target = reg.usable_model("claude-sonnet") if reg else None
    if target is None or target.provider_id != "mock-provider":
        # Local mock provider (python -m uvicorn captain.devprovider:app --port 8090):
        # OpenAI-compatible, deterministic, no real spend.
        admin.upsert_registry(
            SEED_ADMIN,
            Provider(
                "mock-provider",
                ProviderKind.OPENAI,
                "local",
                "env:CAPTAIN_MOCK_PROVIDER_KEY",
                base_url="http://127.0.0.1:8090/v1",
            ),
        )
        admin.upsert_registry(
            SEED_ADMIN, ModelEntry("claude-sonnet", "mock-provider", "mock-claude", "local")
        )
    try:  # example price: $3 / $15 per million input / output tokens (versioned)
        save_price(
            infra_engine(),
            ModelPrice(
                "claude-sonnet",
                "mock-provider",
                "dev-1",
                datetime(2026, 1, 1, tzinfo=UTC),
                input_per_m=3_000_000,
                output_per_m=15_000_000,
            ),
        )
    except IntegrityError:
        pass  # already seeded
    if built.register.get("AI-DEV") is None:
        admin.upsert_register(
            SEED_ADMIN,
            RegisterEntry(
                id="AI-DEV",
                name="Developer sandbox",
                risk_tier=RiskTier.TIER_3,
                autonomy=AutonomyLevel.A2,
                owner_id="dev-owner",
                cost_centre="CC-DEV",
                purpose="Local development",
                allowed_models=frozenset({"claude-sonnet"}),
            ),
        )
    ctx = infra.context_store
    assert isinstance(ctx, PgContextStore)
    ctx.put_project(Project("dev-project", "dev-org", "Dev project", "dev-team"))
    ctx.put_application(Application("dev-application", "dev-project", "AI-DEV", "Dev application"))
    admin.define_budget(
        SEED_ADMIN, BudgetDefinition("org:dev-org", Period.MONTHLY, 50_000_000)
    )  # $50

    seed_mcp(admin, built, infra)
    seed_claude_code(admin, built, infra)

    key_file = DEV / "dev-app.key"
    if not key_file.exists():
        issued = admin.issue_key(
            SEED_ADMIN,
            "dev-app",
            "AI-DEV",
            "dev-project",
            "dev-application",
            Environment.DEVELOPMENT,
            None,
            "dev seed",
        )
        key_file.write_text(issued.raw_key, encoding="utf-8")
    print(f"seeded; dev key saved to {key_file} (not printed)")


if __name__ == "__main__":
    main()
