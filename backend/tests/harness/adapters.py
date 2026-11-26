"""Harness adapters: give every Core implementation the same test-setup surface.

`PolicyDouble` is a TEST DOUBLE for wiring tests only. It mimics just the rules the
harness needs. The real rules live in policies/captain/*.rego and are tested by
`opa test` and by the integration harness against a running OPA.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from captain.core.budget.model import BudgetDefinition, Period
from captain.core.context.service import Application, Project
from captain.core.engine import CaptainCore
from captain.core.factory import BuiltCore, CoreInfra, build_core
from captain.core.identity.killswitch import KillScope
from captain.core.identity.model import PrincipalRecord
from captain.core.ledger.chain import Ed25519Signer, KeyRingVerifier
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import (
    ApprovalTicket,
    ContextHints,
    Credential,
    ResolvedContext,
)
from captain.core.policy.opa import PolicyResult


class PolicyDouble:
    def evaluate(self, payload: dict[str, Any]) -> PolicyResult:
        req, reg = payload["request"], payload["register"]

        def r(decision: str, code: str) -> PolicyResult:
            return PolicyResult(
                decision=decision, reason_code=code, explanation=code,  # type: ignore[arg-type]
                policy_ids=("double",), policy_bundle="double", policy_version="0",
            )  # fmt: skip

        if req["action"] == "TOOL_CALL":
            if req["tool"] not in reg["allowed_tools"]:
                return r("BLOCK", "TOOL_NOT_ALLOWED")
            if reg["autonomy"] in ("A3", "A4"):
                return r("REQUIRE_APPROVAL", "APPROVAL_REQUIRED")
            return r("ALLOW", "ALLOWED")
        if req["action"] == "MODEL_CALL" and req["model"] not in reg["allowed_models"]:
            return r("BLOCK", "MODEL_NOT_ALLOWED")
        return r("ALLOW", "ALLOWED")


class CaptainCoreHarness:
    """Wraps CaptainCore with the stub's setup methods. Test secrets map to real keys."""

    def __init__(
        self, approval_ttl: timedelta = timedelta(minutes=30), erasure: bool = False
    ) -> None:
        from captain.core.subjects import InMemorySubjectStore

        signer = Ed25519Signer("test")
        self.built: BuiltCore = build_core(
            CoreInfra(
                pepper=b"t" * 32,
                policy=PolicyDouble(),
                guard=None,
                signer=signer,
                verifier=KeyRingVerifier({"test": signer.public_key()}),
                approvers=lambda r: ("fin-1", "fin-2"),
                subject_store=InMemorySubjectStore() if erasure else None,
            ),
            approval_ttl_s=approval_ttl.total_seconds(),
        )
        self.core: CaptainCore = self.built.core
        self._keys: dict[str, str] = {}

    # ---- CoreDecisionAPI passthrough (with test-secret translation)
    def resolve(self, credential: Credential, hints: ContextHints) -> ResolvedContext:
        raw = self._keys.get(credential.secret, credential.secret)
        return self.core.resolve(credential.model_copy(update={"secret": raw}), hints)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.core, name)

    # ---- setup surface shared with InMemoryCore
    def register_key(self, secret: str, ctx: ResolvedContext) -> None:
        infra = self.built.infra
        p = ctx.principal
        infra.identity_store.put_principal(
            PrincipalRecord(
                id=p.id, type=p.type.value, org_id=p.org_id, display_name=p.id, team_id=p.team_id
            )
        )
        self.built.register.upsert(ctx.register_entry)
        infra.context_store.projects[ctx.project_id] = Project(
            ctx.project_id, p.org_id, ctx.project_id, p.team_id
        )
        infra.context_store.applications[ctx.application_id] = Application(
            ctx.application_id, ctx.project_id, ctx.register_entry.id, ctx.application_id
        )
        issued = self.core.m.identity.issue_key(
            p.id, ctx.register_entry.id, ctx.project_id, ctx.application_id, ctx.environment, None
        )
        self._keys[secret] = issued.raw_key

    def revoke_key(self, secret: str) -> None:
        from captain.core.identity.keys import parse_key

        self.core.m.identity.revoke_key(parse_key(self._keys[secret]).key_id, "test")

    def kill_principal(self, principal_id: str) -> None:
        self.core.kill(KillScope.PRINCIPAL, principal_id, "test", "admin")

    def set_budget(self, scope: str, limit: int) -> None:
        self.built.infra.budget_catalog.add(BudgetDefinition(scope, Period.MONTHLY, limit))

    def budget(self, scope: str) -> dict[str, int]:
        bucket = BudgetDefinition(scope, Period.MONTHLY, 0).bucket(datetime.now(UTC))
        s = self.core.m.budget.state(bucket)
        return {"limit": s.limit, "spent": s.spent, "reserved": s.reserved}

    def decide_approval(self, ticket_id: UUID, approve: bool, approver_id: str) -> ApprovalTicket:
        return self.core.decide_approval(ticket_id, approve, approver_id, "test")

    @property
    def evidence_available(self) -> bool:
        store = self.built.infra.ledger_store
        assert isinstance(store, InMemoryLedgerStore)
        return store.available

    @evidence_available.setter
    def evidence_available(self, v: bool) -> None:
        store = self.built.infra.ledger_store
        assert isinstance(store, InMemoryLedgerStore)
        store.available = v

    @property
    def ledger(self) -> list[object]:
        store = self.built.infra.ledger_store
        assert isinstance(store, InMemoryLedgerStore)
        return [*store.rows, *store.outbox.values()]
