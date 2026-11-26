"""Assemble a CaptainCore from module implementations.

`build_core()` takes explicit infrastructure so production (PostgreSQL / Valkey / OPA /
guard endpoints / KMS signer) and tests (in-memory) use the same wiring code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from captain.core.alerts import (
    Alert,
    AlertService,
    AlertStore,
    InMemoryAlertStore,
    LogNotifier,
    Notifier,
    RecipientResolver,
    Severity,
)
from captain.core.approvals.service import ApprovalService, ApprovalStore, InMemoryApprovalStore
from captain.core.budget.service import (
    BudgetAlert,
    BudgetCatalog,
    BudgetOverride,
    BudgetService,
    InMemoryBudgetCatalog,
)
from captain.core.budget.store import BudgetStore, InMemoryBudgetStore
from captain.core.bus import EventBus, InMemoryBus
from captain.core.context.service import (
    ContextService,
    ContextStore,
    InMemoryContextStore,
    InMemoryRegisterStore,
    LocalRegisterProvider,
    RegisterStore,
)
from captain.core.engine import CaptainCore, CoreModules, approval_event
from captain.core.evaluation.judge import Judge, Rubric
from captain.core.evaluation.service import DEFAULT_SAMPLE_RATE, EvaluationService
from captain.core.guardrails.detectors import Detector
from captain.core.guardrails.guard import GuardModel
from captain.core.guardrails.service import GuardrailService
from captain.core.identity.killswitch import InMemoryKillSwitchStore, KillSwitch, KillSwitchStore
from captain.core.identity.oidc import WorkloadAuthenticator
from captain.core.identity.service import IdentityService, IdentityStore, InMemoryIdentityStore
from captain.core.ledger.chain import Signer, Verifier
from captain.core.ledger.service import (
    InMemoryLedgerStore,
    InMemoryWormStore,
    LedgerService,
    LedgerStore,
    WormStore,
)
from captain.core.models import GovernanceRequest, PrincipalType, RegisterEntry
from captain.core.policy.opa import PolicyEngine
from captain.core.registry.service import RegistryService
from captain.core.scrub import InMemoryScrubber, Scrubber
from captain.core.subjects import Pseudonymiser, SubjectStore
from captain.core.usage.pricing import PriceCatalog


@dataclass
class Alerts:
    """Collected alerts; production forwards these to email/Teams and the ledger."""

    budget: list[BudgetAlert] = field(default_factory=list)
    overrides: list[BudgetOverride] = field(default_factory=list)
    register_downgrades: list[tuple[RegisterEntry, RegisterEntry]] = field(default_factory=list)


@dataclass
class CoreInfra:
    pepper: bytes
    policy: PolicyEngine
    guard: GuardModel | None
    signer: Signer
    verifier: Verifier
    approvers: Callable[[GovernanceRequest], tuple[str, ...]]
    bus: EventBus = field(default_factory=InMemoryBus)
    identity_store: IdentityStore = field(default_factory=InMemoryIdentityStore)
    context_store: ContextStore = field(default_factory=InMemoryContextStore)
    register_store: RegisterStore = field(default_factory=InMemoryRegisterStore)
    killswitch_store: KillSwitchStore = field(default_factory=InMemoryKillSwitchStore)
    budget_catalog: BudgetCatalog = field(default_factory=InMemoryBudgetCatalog)
    budget_store: BudgetStore = field(default_factory=InMemoryBudgetStore)
    approval_store: ApprovalStore = field(default_factory=InMemoryApprovalStore)
    ledger_store: LedgerStore = field(default_factory=InMemoryLedgerStore)
    worm: WormStore = field(default_factory=InMemoryWormStore)
    banned_terms: tuple[str, ...] = ()
    workload: WorkloadAuthenticator | None = None
    # Personal-data detector. None = regex + checksums only. For NER use
    # CombinedDetector(RegexPiiDetector(), PresidioDetector(AnalyzerEngine())).
    pii: Detector | None = None
    registry: RegistryService | None = None
    require_budget: bool = False  # production settings turn this on
    prices: PriceCatalog | None = None
    # Live evaluation (ADR-005): an in-perimeter judge plus the reviewed rubric. Both or
    # neither; None = evaluation off. Sample rate per register entry, default 5%.
    judge: Judge | None = None
    rubric: Rubric | None = None
    eval_default_rate: float = DEFAULT_SAMPLE_RATE
    eval_rates: dict[str, float] = field(default_factory=dict)
    # Erasure (ADR-005): where person keys live. None = identifiers are written in the
    # clear (unit-test default); production settings always set it.
    subject_store: SubjectStore | None = None
    # Erasure of the editable records (ADR-005). None with a subject store = the
    # in-memory scrubber, which only works over the in-memory stores.
    scrubber: Scrubber | None = None
    alert_store: AlertStore = field(default_factory=InMemoryAlertStore)
    notifiers: tuple[Notifier, ...] = (LogNotifier(),)
    alert_recipients: RecipientResolver = lambda a: ()


@dataclass
class BuiltCore:
    core: CaptainCore
    register: LocalRegisterProvider
    alerts: Alerts
    infra: CoreInfra
    alert_service: AlertService
    pseudonymiser: Pseudonymiser | None = None
    scrubber: Scrubber | None = None


def build_core(infra: CoreInfra, approval_ttl_s: float = 1800) -> BuiltCore:
    from datetime import timedelta

    alerts = Alerts()
    pseudo: Pseudonymiser | None = None
    if infra.subject_store is not None:
        ids = infra.identity_store

        kinds: dict[str, bool] = {}  # a principal's type never changes

        def is_person(pid: str) -> bool:
            if pid not in kinds:
                p = ids.get_principal(pid)  # unknown ids are treated as people (the safe side)
                if len(kinds) > 10_000:
                    kinds.clear()
                kinds[pid] = p is None or str(p.type) == PrincipalType.USER.value
            return kinds[pid]

        pseudo = Pseudonymiser(infra.subject_store, is_person)
    ledger = LedgerService(
        infra.ledger_store, infra.worm, infra.signer, infra.verifier,
        pseudo.event if pseudo else None,
    )  # fmt: skip
    svc = AlertService(infra.alert_store, infra.notifiers, ledger.enqueue, infra.alert_recipients)

    def on_downgrade(old: RegisterEntry, new: RegisterEntry) -> None:
        alerts.register_downgrades.append((old, new))
        svc.raise_alert(Alert(
            "register.downgrade", Severity.CRITICAL, f"Controls weakened for {new.id}",
            {"from_tier": old.risk_tier.value, "to_tier": new.risk_tier.value,
             "from_autonomy": old.autonomy.value, "to_autonomy": new.autonomy.value},
            register_id=new.id,
        ))  # fmt: skip

    def on_budget(a: BudgetAlert) -> None:
        alerts.budget.append(a)
        svc.raise_alert(Alert(
            "budget.threshold", Severity.CRITICAL if a.threshold >= 1 else Severity.WARNING,
            f"Budget {a.scope} reached {int(a.threshold * 100)}%",
            {"scope": a.scope, "bucket": a.bucket, "spent": a.spent, "limit": a.limit},
        ))  # fmt: skip

    def on_override(o: BudgetOverride) -> None:
        alerts.overrides.append(o)
        svc.raise_alert(Alert(
            "budget.override", Severity.WARNING, f"Budget override on {o.scope}",
            {"scope": o.scope, "extra": o.extra, "actor": o.actor_id, "reason": o.reason},
        ))  # fmt: skip

    register = LocalRegisterProvider(infra.bus, on_downgrade, infra.register_store)

    def on_ticket(t):  # type: ignore[no-untyped-def]
        if (e := approval_event(t)) is not None:
            ledger.enqueue(e)

    modules = CoreModules(
        identity=IdentityService(infra.identity_store, infra.pepper, infra.bus),
        context=ContextService(register, infra.context_store),
        killswitch=KillSwitch(infra.killswitch_store, infra.bus),
        guardrails=GuardrailService(infra.guard, pii=infra.pii, banned_terms=infra.banned_terms),
        policy=infra.policy,
        budget=BudgetService(
            infra.budget_catalog,
            infra.budget_store,
            on_budget,
            on_override,
            require_budget=infra.require_budget,
        ),
        approvals=ApprovalService(
            infra.approval_store, infra.approvers, on_ticket, ttl=timedelta(seconds=approval_ttl_s)
        ),
        ledger=ledger,
        workload=infra.workload,
        registry=infra.registry,
        prices=infra.prices,
        evaluation=EvaluationService(
            infra.judge, infra.rubric, ledger.enqueue, svc.raise_alert,
            infra.eval_default_rate, infra.eval_rates,
        )
        if infra.judge is not None and infra.rubric is not None
        else None,
    )  # fmt: skip
    scrubber = infra.scrubber
    if scrubber is None and pseudo is not None:
        if not isinstance(infra.identity_store, InMemoryIdentityStore):
            raise ValueError("erasure over database stores needs infra.scrubber (PgScrubber)")
        scrubber = InMemoryScrubber(infra)
    return BuiltCore(CaptainCore(modules), register, alerts, infra, svc, pseudo, scrubber)
