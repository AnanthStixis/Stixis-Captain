"""Administrative operations on Captain Core.

Every operation:
1. checks the actor's permission (RBAC), and
2. leaves an evidence-ledger event naming the actor and the parameters (never secrets).

Ordering rule:
- Safety-INCREASING actions (revoke key, kill switch) take effect first and are then
  recorded; if recording fails, the error is raised but the action stays in force.
- Everything else (issuing keys, register changes, budget changes and overrides,
  un-kill, approvals) is recorded first and refused if evidence cannot be written,
  so no control can be weakened without a trace.

This is the Core-side surface the Phase 2 admin API / dashboard calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from captain.core.budget.model import BudgetDefinition, RateLimitDefinition
from captain.core.context.service import LocalRegisterProvider, is_control_downgrade
from captain.core.engine import CaptainCore, admin_event
from captain.core.identity.killswitch import KillScope
from captain.core.identity.rbac import Permission, require
from captain.core.identity.service import IssuedKey
from captain.core.models import ApprovalTicket, Environment, EventType, RegisterEntry
from captain.core.registry.service import McpServer, ModelEntry, Provider, Route, ToolEntry

RegistryItem = Provider | ModelEntry | Route | McpServer | ToolEntry


@dataclass(frozen=True, slots=True)
class AdminActor:
    id: str
    roles: frozenset[str]


class WritableBudgetCatalog(Protocol):
    def add(self, d: BudgetDefinition) -> None: ...


class AdminService:
    def __init__(
        self, core: CaptainCore, register: LocalRegisterProvider, budgets: WritableBudgetCatalog
    ) -> None:
        self._core = core
        self._register = register
        self._budgets = budgets

    @property
    def _ledger(self):  # type: ignore[no-untyped-def]
        return self._core.m.ledger

    # ------------------------------------------------------------------ keys
    def issue_key(
        self,
        actor: AdminActor,
        principal_id: str,
        register_id: str,
        project_id: str,
        application_id: str,
        environment: Environment,
        ttl: timedelta | None,
        label: str = "",
    ) -> IssuedKey:
        require(actor.roles, Permission.KEYS_WRITE)
        issued = self._core.m.identity.issue_key(
            principal_id, register_id, project_id, application_id, environment, ttl, label
        )
        try:
            self._ledger.enqueue(
                admin_event(
                    EventType.KEY_ISSUED,
                    actor.id,
                    {
                        "key_id": issued.record.key_id,
                        "principal_id": principal_id,
                        "project_id": project_id,
                        "application_id": application_id,
                        "environment": environment.value,
                        "label": label,
                        "expires_at": issued.record.expires_at.isoformat()
                        if issued.record.expires_at
                        else None,
                    },
                    register_id,
                )
            )
        except Exception:
            # No key may exist without evidence of who issued it.
            self._core.m.identity.revoke_key(issued.record.key_id, "issue not recorded")
            raise
        return issued

    def revoke_key(self, actor: AdminActor, key_id: str, reason: str) -> None:
        require(actor.roles, Permission.KEYS_WRITE)
        rec = self._core.m.identity.revoke_key(key_id, reason)  # safety-increasing: first
        self._ledger.enqueue(
            admin_event(
                EventType.KEY_REVOKED,
                actor.id,
                {"key_id": key_id, "reason": reason},
                rec.register_id,
            )
        )

    def rotate_key(self, actor: AdminActor, key_id: str) -> IssuedKey:
        require(actor.roles, Permission.KEYS_WRITE)
        new = self._core.m.identity.rotate_key(key_id)
        self._ledger.enqueue(
            admin_event(
                EventType.KEY_ROTATED,
                actor.id,
                {"old_key_id": key_id, "new_key_id": new.record.key_id},
                new.record.register_id,
            )
        )
        return new

    # ----------------------------------------------------------- kill switch
    def kill(self, actor: AdminActor, scope: KillScope, target: str, reason: str) -> None:
        require(actor.roles, Permission.KILL_SWITCH)
        self._core.kill(scope, target, reason, actor.id)

    def unkill(self, actor: AdminActor, scope: KillScope, target: str, reason: str) -> None:
        require(actor.roles, Permission.KILL_SWITCH)
        self._core.unkill(scope, target, reason, actor.id)

    # -------------------------------------------------------------- register
    def upsert_register(
        self, actor: AdminActor, entry: RegisterEntry, approval_ref: str | None = None
    ) -> RegisterEntry:
        require(actor.roles, Permission.REGISTER_WRITE)
        old = self._register.get(entry.id)
        downgrade = old is not None and is_control_downgrade(old, entry)
        self._ledger.enqueue(
            admin_event(
                EventType.REGISTER_CHANGED,
                actor.id,
                {
                    "tier": entry.risk_tier.value,
                    "autonomy": entry.autonomy.value,
                    "previous_tier": old.risk_tier.value if old else None,
                    "previous_autonomy": old.autonomy.value if old else None,
                    "high_risk": entry.eu_ai_act_high_risk,
                    "downgrade": downgrade,
                    "approval_ref": approval_ref,
                    "active": entry.active,
                },
                entry.id,
            )
        )
        return self._register.upsert(entry, approval_ref)

    # ---------------------------------------------------------------- budget
    def define_budget(self, actor: AdminActor, d: BudgetDefinition) -> None:
        require(actor.roles, Permission.BUDGET_WRITE)
        self._ledger.enqueue(
            admin_event(
                EventType.BUDGET_DEFINED,
                actor.id,
                {"scope": d.scope, "period": d.period.value, "limit": d.limit, "hard": d.hard},
            )
        )
        self._budgets.add(d)

    def define_rate_limit(self, actor: AdminActor, r: RateLimitDefinition) -> None:
        """Requests-per-minute / tokens-per-minute limit on a scope. Per-tool limits use the
        scopes `tool:<name>` (everyone) and `ptool:<principal>:<name>` (one caller)."""
        require(actor.roles, Permission.BUDGET_WRITE)
        if r.requests_per_minute is None and r.tokens_per_minute is None:
            raise ValueError("a rate limit needs requests_per_minute or tokens_per_minute")
        self._ledger.enqueue(
            admin_event(
                EventType.BUDGET_DEFINED,
                actor.id,
                {
                    "kind": "rate_limit",
                    "scope": r.scope,
                    "requests_per_minute": r.requests_per_minute,
                    "tokens_per_minute": r.tokens_per_minute,
                },
            )
        )
        self._budgets.add_rate(r)  # type: ignore[attr-defined]

    def remove_rate_limit(self, actor: AdminActor, scope: str) -> bool:
        """Lifting a limit is a weakening change: recorded BEFORE it takes effect."""
        require(actor.roles, Permission.BUDGET_WRITE)
        self._ledger.enqueue(
            admin_event(
                EventType.BUDGET_DEFINED,
                actor.id,
                {"kind": "rate_limit_removed", "scope": scope},
            )
        )
        return bool(self._budgets.remove_rate(scope))  # type: ignore[attr-defined]

    def remove_budget(self, actor: AdminActor, scope: str) -> bool:
        """Removing a spending limit is a weakening change: recorded BEFORE it takes effect."""
        require(actor.roles, Permission.BUDGET_WRITE)
        self._ledger.enqueue(
            admin_event(
                EventType.BUDGET_DEFINED, actor.id, {"kind": "budget_removed", "scope": scope}
            )
        )
        return bool(self._budgets.remove(scope))  # type: ignore[attr-defined]

    def override_budget(self, actor: AdminActor, scope: str, extra: int, reason: str) -> None:
        require(actor.roles, Permission.BUDGET_OVERRIDE)
        if extra <= 0 or not reason.strip():
            raise ValueError("override needs a positive amount and a reason")
        self._ledger.enqueue(
            admin_event(
                EventType.BUDGET_OVERRIDE,
                actor.id,
                {"scope": scope, "extra": extra, "reason": reason},
            )
        )
        self._core.m.budget.override(scope, extra, actor.id, reason)

    # -------------------------------------------------------------- approvals
    def decide_approval(
        self, actor: AdminActor, ticket_id: UUID, approve: bool, reason: str
    ) -> ApprovalTicket:
        require(actor.roles, Permission.APPROVAL_DECIDE)
        # The approval engine records approval.decided through its change listener.
        return self._core.decide_approval(ticket_id, approve, actor.id, reason)

    # -------------------------------------------------------------- registry
    def upsert_registry(self, actor: AdminActor, item: RegistryItem) -> None:
        """Provider, model, route, MCP server or tool. Recorded first, then applied
        (adding a model or tool widens what can be called)."""
        require(actor.roles, Permission.REGISTER_WRITE)
        reg = self._core.m.registry
        if reg is None:
            raise ValueError("no registry configured")
        details: dict[str, str | int | float | bool | None]
        if isinstance(item, Provider):
            details = {"kind": "provider", "key": item.id, "provider": item.kind.value,
                       "region": item.region, "active": item.active}  # fmt: skip
        elif isinstance(item, ModelEntry):
            details = {"kind": "model", "key": item.alias, "provider_id": item.provider_id,
                       "provider_model": item.provider_model, "active": item.active}  # fmt: skip
        elif isinstance(item, Route):
            details = {"kind": "route", "key": item.alias, "targets": ",".join(item.targets)}
        elif isinstance(item, McpServer):
            details = {"kind": "server", "key": item.id, "url": item.url, "active": item.active}
        else:
            details = {"kind": "tool", "key": item.name, "server_id": item.server_id,
                       "impact": item.impact.value, "risk": item.risk.value,
                       "read_only": item.read_only, "active": item.active}  # fmt: skip
        self._ledger.enqueue(admin_event(EventType.REGISTRY_CHANGED, actor.id, details))
        if isinstance(item, Provider):
            reg.upsert_provider(item)
        elif isinstance(item, ModelEntry):
            reg.upsert_model(item)
        elif isinstance(item, Route):
            reg.upsert_route(item)
        elif isinstance(item, McpServer):
            reg.upsert_server(item)
        else:
            reg.upsert_tool(item)

    # ---------------------------------------------------------------- policy
    def record_policy_published(
        self, actor: AdminActor, bundle: str, version: str, bundle_sha256: str, signer_key_id: str
    ) -> None:
        require(actor.roles, Permission.POLICY_PUBLISH)
        self._ledger.enqueue(
            admin_event(
                EventType.POLICY_CHANGED,
                actor.id,
                {
                    "bundle": bundle,
                    "version": version,
                    "sha256": bundle_sha256,
                    "signer_key_id": signer_key_id,
                },
            )
        )
