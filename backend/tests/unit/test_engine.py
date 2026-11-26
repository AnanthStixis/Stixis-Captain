"""CaptainCore engine: fail modes by tier, redaction, kill switch scopes, evidence."""

from __future__ import annotations

from typing import Any

import pytest

from captain.core.budget.model import BudgetDefinition, Period
from captain.core.budget.store import BudgetStoreUnavailable, InMemoryBudgetStore
from captain.core.decision_api import CoreError
from captain.core.guardrails.guard import GuardScore, GuardUnavailableError
from captain.core.identity.killswitch import KillScope
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import (
    ActionType,
    ActualUsage,
    AutonomyLevel,
    Channel,
    ContentRef,
    ContextHints,
    CostEstimate,
    Credential,
    DecisionType,
    GovernanceRequest,
    PrincipalType,
    ReasonCode,
    RiskTier,
    TokenUsage,
    UsageSource,
)
from captain.core.policy.opa import PolicyResult, PolicyUnavailableError
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness, PolicyDouble


class DownPolicy:
    def evaluate(self, payload: dict[str, Any]) -> PolicyResult:
        raise PolicyUnavailableError("down")


class RedactPolicy(PolicyDouble):
    def evaluate(self, payload: dict[str, Any]) -> PolicyResult:
        if any(f["type"] == "PII" for f in payload["findings"]):
            return PolicyResult(
                decision="ALLOW_WITH_REDACTION", reason_code="PII_REDACTED", explanation="mask",  # type: ignore[arg-type]
                policy_ids=("x",), policy_bundle="b", policy_version="1",
            )  # fmt: skip
        return super().evaluate(payload)


class Guard:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def classify(self, text: str) -> GuardScore:
        if self.fail:
            raise GuardUnavailableError("down")
        return GuardScore(0.0, 0.0, "g")


class FlakyBudgetStore(InMemoryBudgetStore):
    def reserve(self, *a: Any, **k: Any) -> None:
        raise BudgetStoreUnavailable("valkey down")


def setup(tier: RiskTier, **infra: Any) -> tuple[CaptainCoreHarness, Any]:
    h = CaptainCoreHarness()
    for k, v in infra.items():
        setattr(h.built.infra, k, v)
    m = h.core.m
    if "policy" in infra:
        m.policy = infra["policy"]
    if "guard" in infra:
        from captain.core.guardrails.service import GuardrailService

        m.guardrails = GuardrailService(infra["guard"])
    if "budget_store" in infra:
        m.budget._store = infra["budget_store"]  # noqa: SLF001
    h.register_key("k", make_context("svc", tier, AutonomyLevel.A2, PrincipalType.APPLICATION))
    ctx = h.resolve(Credential(kind="virtual_key", secret="k"), ContextHints())
    return h, ctx


def call(ctx: Any, text: str | None = None, model: str = "claude-sonnet") -> GovernanceRequest:
    return GovernanceRequest(
        trace_id="t", context=ctx, channel=Channel.API, action=ActionType.MODEL_CALL, model=model,
        contents=(ContentRef(text=text),) if text else (),
    )  # fmt: skip


@pytest.mark.parametrize("tier", [RiskTier.TIER_1, RiskTier.TIER_2])
def test_policy_down_fails_closed_for_tier_1_2(tier: RiskTier) -> None:
    h, ctx = setup(tier, policy=DownPolicy())
    d = h.core.decide(call(ctx))
    assert (d.decision, d.reason_code) == (DecisionType.BLOCK, ReasonCode.CONTROL_UNAVAILABLE)


@pytest.mark.parametrize("tier", [RiskTier.TIER_3, RiskTier.TIER_4])
def test_policy_down_fails_open_flagged_for_tier_3_4(tier: RiskTier) -> None:
    h, ctx = setup(tier, policy=DownPolicy())
    d = h.core.decide(call(ctx))
    assert d.permits_execution and d.fail_open_applied
    assert d.reason_code is ReasonCode.CONTROL_UNAVAILABLE_FAIL_OPEN


def test_guard_down_fails_closed_tier_1_open_tier_3() -> None:
    h1, c1 = setup(RiskTier.TIER_1, guard=Guard(fail=True))
    assert h1.core.decide(call(c1, "hello")).reason_code is ReasonCode.CONTROL_UNAVAILABLE
    h3, c3 = setup(RiskTier.TIER_3, guard=Guard(fail=True))
    d = h3.core.decide(call(c3, "hello"))
    assert d.permits_execution and d.fail_open_applied


def test_evidence_down_refuses_tier_1_flags_tier_3() -> None:
    h1, c1 = setup(RiskTier.TIER_1, guard=Guard())
    h1.evidence_available = False
    assert h1.core.decide(call(c1)).reason_code is ReasonCode.CONTROL_UNAVAILABLE
    h3, c3 = setup(RiskTier.TIER_3, guard=Guard())
    h3.evidence_available = False
    d = h3.core.decide(call(c3))
    assert d.permits_execution and d.fail_open_applied


def test_redaction_applied_and_findings_have_no_text() -> None:
    h, ctx = setup(RiskTier.TIER_2, policy=RedactPolicy(), guard=Guard())
    d = h.core.decide(call(ctx, "email me at jane@corp.com"))
    assert d.decision is DecisionType.ALLOW_WITH_REDACTION
    assert d.redacted_contents and d.redacted_contents[0].text == "email me at <EMAIL_ADDRESS_1>"
    assert "jane@corp.com" not in d.model_dump_json(exclude={"redacted_contents"})


@pytest.mark.parametrize(
    ("scope", "target"),
    [(KillScope.MODEL, "claude-sonnet"), (KillScope.ORG, "org-1"), (KillScope.CHANNEL, "API")],
)
def test_kill_switch_scopes(scope: KillScope, target: str) -> None:
    h, ctx = setup(RiskTier.TIER_3)
    h.core.kill(scope, target, "incident", "admin")
    assert h.core.decide(call(ctx)).reason_code is ReasonCode.KILL_SWITCH_ACTIVE


def test_kill_key_revokes_it() -> None:
    h, ctx = setup(RiskTier.TIER_3)
    assert ctx.key_id
    h.core.kill(KillScope.KEY, ctx.key_id, "leak", "admin")
    from captain.core.decision_api import AuthenticationError

    with pytest.raises(AuthenticationError):
        h.resolve(Credential(kind="virtual_key", secret="k"), ContextHints())


def test_budget_store_down() -> None:
    est = CostEstimate(input_tokens=1, max_output_tokens=1, amount=1, price_version="p")
    h1, c1 = setup(RiskTier.TIER_1, budget_store=FlakyBudgetStore())
    with pytest.raises(CoreError) as e:
        h1.core.reserve(h1.core.decide(call(c1)), est)
    assert e.value.reason_code is ReasonCode.CONTROL_UNAVAILABLE
    h3, c3 = setup(RiskTier.TIER_3, budget_store=FlakyBudgetStore())
    r = h3.core.reserve(h3.core.decide(call(c3)), est)
    assert r.fail_open_applied and r.amount == 0
    h3.core.settle(
        r, ActualUsage(usage=TokenUsage(), source=UsageSource.ESTIMATED, cost=1, price_version="p")
    )


def test_full_flow_leaves_verifiable_evidence() -> None:
    h, ctx = setup(RiskTier.TIER_2)
    h.built.infra.budget_catalog.add(BudgetDefinition("org:org-1", Period.MONTHLY, 10_000))
    d = h.core.decide(call(ctx))
    r = h.core.reserve(
        d, CostEstimate(input_tokens=10, max_output_tokens=10, amount=500, price_version="p")
    )
    h.core.settle(
        r,
        ActualUsage(
            usage=TokenUsage(input_tokens=10, output_tokens=4),
            source=UsageSource.PROVIDER_REPORTED,
            cost=120,
            price_version="p",
        ),
    )
    h.core.decide(call(ctx, model="not-allowed"))
    ledger = h.core.m.ledger
    assert ledger.write_pending() == 3
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    types = [e.event_type for e in store.rows]
    assert types == ["call.allowed", "usage.settled", "call.refused"]
    assert {e.partition for e in store.rows} == {ctx.register_entry.id}
    assert ledger.anchor() is not None
    assert ledger.verify().ok


def test_decision_carries_scopes_for_budget() -> None:
    h, ctx = setup(RiskTier.TIER_4)
    d = h.core.decide(call(ctx))
    assert "org:org-1" in d.budget_scopes and f"key:{ctx.key_id}" in d.budget_scopes
    assert "model:claude-sonnet" in d.budget_scopes and "cost_centre:CC-100" in d.budget_scopes


def test_budget_refusal_is_recorded_in_ledger() -> None:
    """UC-02: a refusal over the cap costs nothing and is written to the ledger."""
    from captain.core.decision_api import BudgetExceededError

    h, ctx = setup(RiskTier.TIER_2)
    h.built.infra.budget_catalog.add(BudgetDefinition("org:org-1", Period.MONTHLY, 100))  # type: ignore[attr-defined]
    d = h.core.decide(call(ctx))
    with pytest.raises(BudgetExceededError):
        h.core.reserve(
            d, CostEstimate(input_tokens=1, max_output_tokens=1, amount=101, price_version="p")
        )
    h.core.m.ledger.write_pending()
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    refused = [e for e in store.rows if e.event_type == "call.refused"]
    assert len(refused) == 1 and refused[0].body["reason_code"] == "BUDGET_EXHAUSTED"


def test_restore_only_for_same_principal() -> None:
    from captain.core.decision_api import CoreError

    h, ctx = setup(RiskTier.TIER_2, policy=RedactPolicy(), guard=Guard())
    d = h.core.decide(call(ctx, "email jane@corp.com"))
    reply = "I will write to <EMAIL_ADDRESS_1> today."
    assert h.core.restore(d, reply, ctx.principal.id) == "I will write to jane@corp.com today."
    with pytest.raises(CoreError):
        h.core.restore(d, reply, "someone-else")
    h.core.finish_response(d)
    with pytest.raises(CoreError):
        h.core.restore(d, reply, ctx.principal.id)


def test_stream_guard_follows_tier_and_masks_new_pii() -> None:
    h, ctx = setup(RiskTier.TIER_1, guard=Guard())
    d = h.core.decide(call(ctx))
    sg = h.core.stream_guard(d)
    assert sg.mode == "buffer"
    assert list(sg.feed("Contact bob@x.com. ")) == []  # T1 holds everything
    assert "".join(sg.finish()) == "Contact <EMAIL_ADDRESS_1>. "
    h4, c4 = setup(RiskTier.TIER_4)
    assert h4.core.stream_guard(h4.core.decide(call(c4))).mode == "passthrough"


def test_presidio_detector_configurable_in_build() -> None:
    from types import SimpleNamespace

    from captain.core.factory import CoreInfra, build_core
    from captain.core.guardrails.detectors import RegexPiiDetector
    from captain.core.guardrails.presidio import CombinedDetector, PresidioDetector
    from captain.core.ledger.chain import Ed25519Signer, KeyRingVerifier

    class Ner:
        def analyze(self, **kw: Any) -> list[SimpleNamespace]:
            i = kw["text"].find("Priya")
            return (
                [SimpleNamespace(entity_type="PERSON", start=i, end=i + 5, score=0.9)]
                if i >= 0
                else []
            )

    s = Ed25519Signer("k")
    built = build_core(CoreInfra(
        pepper=b"x" * 32, policy=PolicyDouble(), guard=None, signer=s,
        verifier=KeyRingVerifier({"k": s.public_key()}), approvers=lambda r: ("a",),
        pii=CombinedDetector(RegexPiiDetector(), PresidioDetector(Ner())),
    ))  # fmt: skip
    rep = built.core.m.guardrails.inspect([ContentRef(text="Ask Priya")], RiskTier.TIER_4)
    assert [f.entity for f in rep.findings] == ["PERSON"]


def test_unattributable_refusals_leave_security_evidence_without_the_key() -> None:
    """A wrong key or a revoked key cannot be filed under a register entry, but is not lost:
    it goes to the 'security' partition, once per reason and subject, never with the key."""
    from captain.core.decision_api import AuthenticationError

    h, ctx = setup(RiskTier.TIER_3)
    bad = Credential(kind="virtual_key", secret="cpt_live_nope_nope_00000000")
    for _ in range(3):
        with pytest.raises(AuthenticationError):
            h.resolve(bad, ContextHints(client_ip="10.1.2.3"))
    with pytest.raises(AuthenticationError):
        h.resolve(bad, ContextHints(client_ip="10.9.9.9"))  # another source: its own event
    h.core.m.ledger.write_pending()
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    sec = [e for e in store.rows if e.partition == "security"]
    assert len(sec) == 2  # rate-limited: three identical refusals became one
    assert sec[0].body["details"]["stage"] == "authentication"
    assert sec[0].body["reason_code"] == "AUTH_INVALID" and sec[0].body["decision"] == "BLOCK"
    assert "nope" not in str(sec[0].body)  # the credential is never recorded
    assert h.core.m.ledger.verify().ok


def test_security_events_cannot_flood_the_ledger_or_carry_free_text() -> None:
    """The client address is attacker-controlled: only real IPs are recorded, and the ledger
    takes at most a fixed number of security events a minute, however many sources attack."""
    from captain.core.decision_api import AuthenticationError
    from captain.core.engine import SECURITY_EVENTS_PER_MINUTE

    h, ctx = setup(RiskTier.TIER_3)
    bad = Credential(kind="virtual_key", secret="cpt_live_nope_nope_00000000")
    for i in range(SECURITY_EVENTS_PER_MINUTE * 3):  # many distinct sources
        with pytest.raises(AuthenticationError):
            h.resolve(bad, ContextHints(client_ip=f"10.0.{i // 250}.{i % 250}"))
    with pytest.raises(AuthenticationError):
        h.resolve(bad, ContextHints(client_ip="'; DROP TABLE x; -- " + "A" * 5000))
    h.core.m.ledger.write_pending()
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    sec = [e for e in store.rows if e.partition == "security"]
    assert len(sec) == SECURITY_EVENTS_PER_MINUTE  # capped, not 180+
    assert all(len(str(e.body["details"]["client_ip"])) <= 45 for e in sec)
    assert "DROP TABLE" not in str(sec)
    assert h.core.m.ledger.verify().ok
