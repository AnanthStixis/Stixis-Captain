"""Evidence says WHAT the guardrails found (kinds and counts), never the values."""

from __future__ import annotations

from typing import Any

from captain.core.engine import summarize_findings
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import ContentRef, FindingType, GuardrailFinding, RiskTier
from tests.conftest import model_call
from tests.unit.test_engine import Guard, RedactPolicy, setup

EMAIL = "ravi.private@example.com"
SECRET = "AKIAIOSFODNN7EXAMPLE"


def events(h: Any) -> list[tuple[str, dict[str, Any]]]:
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    return [(t, b) for (_p, t, b) in store.outbox.values()]


def test_masked_call_lists_the_kinds_found_but_not_the_values() -> None:
    h, ctx = setup(RiskTier.TIER_3, policy=RedactPolicy(), guard=Guard())
    req = model_call(ctx).model_copy(
        update={"contents": (ContentRef(text=f"mail {EMAIL} and again {EMAIL}"),)}
    )
    d = h.core.decide(req)
    assert d.decision.value == "ALLOW_WITH_REDACTION"
    (ev,) = [b for t, b in events(h) if t == "call.masked"]
    detected = ev["details"]["detected"]
    assert detected == "PII:EMAIL_ADDRESS x2"
    assert EMAIL not in str(events(h))  # not in any evidence


def test_a_call_with_nothing_found_records_no_detection() -> None:
    h, ctx = setup(RiskTier.TIER_3, policy=RedactPolicy(), guard=Guard())
    h.core.decide(model_call(ctx).model_copy(update={"contents": (ContentRef(text="hello"),)}))
    (ev,) = [b for t, b in events(h) if t == "call.allowed"]
    assert ev["details"]["detected"] is None


def test_summary_groups_counts_and_is_bounded() -> None:
    f = lambda t, e: GuardrailFinding(type=t, entity=e, detector="x", content_index=0)  # noqa: E731
    got = summarize_findings(
        (
            f(FindingType.PII, "EMAIL_ADDRESS"),
            f(FindingType.SECRET, "AWS_ACCESS_KEY"),
            f(FindingType.PII, "EMAIL_ADDRESS"),
        )
    )
    assert got == "PII:EMAIL_ADDRESS x2, SECRET:AWS_ACCESS_KEY x1"
    assert summarize_findings(()) is None
    # a guard-model score near zero is not a finding; a likely injection is
    low = GuardrailFinding(
        type=FindingType.INJECTION,
        entity="prompt_injection",
        detector="g",
        content_index=0,
        score=0.02,
    )
    high = low.model_copy(update={"score": 0.9})
    assert summarize_findings((low,)) is None
    assert summarize_findings((low, high)) == "INJECTION:prompt_injection x1"
    many = tuple(f(FindingType.PII, f"ENTITY_{i}") for i in range(200))
    assert len(summarize_findings(many) or "") <= 500
