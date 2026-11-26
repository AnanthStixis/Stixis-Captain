"""Plain-language event descriptions for the dashboard (requirement: Activity, Audit and
Alerts understandable by everyone)."""

from __future__ import annotations

from typing import Any

import pytest

from captain.api.describe import (
    ALERTS,
    CATEGORIES,
    OUTCOMES,
    REASONS,
    describe,
    describe_alert,
    found_label,
    money,
    scope_label,
    who_label,
)
from captain.core.models import EventType, ReasonCode


def ev(event_type: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"event_type": event_type, "principal_id": "ravi@corp.example",
                            "register_id": "AI-DEV", "details": {}}  # fmt: skip
    return {**base, **kw}


def test_every_reason_code_and_event_type_has_wording() -> None:
    """A new code cannot reach the dashboard without plain-language text."""
    assert {r.value for r in ReasonCode} <= set(REASONS)
    assert {e.value for e in EventType} <= set(CATEGORIES)
    for e in EventType:
        out = describe(ev(e.value, reason_code="ALLOWED"))
        assert out["headline"] and out["outcome"] and out["outcome_tone"]
        assert "_" not in out["outcome"] and out["outcome"] in {w for w, _ in OUTCOMES.values()}
        assert (
            out["headline"][0].isupper() or out["headline"][0].isdigit() or "@" in out["headline"]
        )


def test_texts_are_plain_sentences_without_codes() -> None:
    for text in REASONS.values():
        assert text.endswith(".") and not any(c.isupper() and "_" in text for c in text)
    assert all("_" not in t for t in REASONS.values())


@pytest.mark.parametrize(
    ("event", "outcome", "words"),
    [
        (ev("call.allowed", model="claude-sonnet"), "Allowed",
         "ravi@corp.example's request to claude-sonnet was allowed."),
        (ev("call.masked", model="vscode", reason_code="PII_REDACTED",
            details={"detected": "PII:EMAIL_ADDRESS x1"}),
         "Allowed, data hidden", "An email address was hidden first."),
        (ev("call.refused", model="vscode", reason_code="BUDGET_EXHAUSTED"),
         "Blocked", "request to vscode was blocked."),
        (ev("call.refused", tool="demo.create_issue", reason_code="APPROVAL_REQUIRED"),
         "Waiting for approval", "use of the tool demo.create_issue needs approval."),
        (ev("call.refused", model="x", reason_code="INJECTION_DETECTED",
            details={"detected": "INJECTION:prompt_injection x1"}),
         "Blocked", "It contained a prompt-injection attempt."),
        (ev("usage.settled", model="vscode", cost_micros=40_000, usage_source="PROVIDER_REPORTED"),
         "Charged", "vscode use cost $0.04."),
        (ev("tool.held", tool="demo.create_issue"), "Waiting for approval",
         "is waiting for approval"),
        (ev("key.revoked", principal_id="priya@corp.example",
            details={"key_id": "910c", "reason": "left the company"}),
         "Settings change", "priya@corp.example revoked the Captain key 910c."),
        (ev("killswitch.changed", principal_id="priya@corp.example",
            details={"action": "activate", "scope": "channel", "target": "IDE", "reason": "x"}),
         "Alert", "turned ON an emergency stop for channel \"IDE\"."),
        (ev("budget.defined", principal_id="priya@corp.example",
            details={"scope": "principal:dev-app", "period": "MONTHLY", "limit": 50_000_000}),
         "Settings change", "set a monthly budget of $50.00 for person dev-app."),
        (ev("approval.decided", details={"state": "APPROVED", "reason": "C6 test"}),
         "Approved", "ravi@corp.example approved an action that was waiting for approval."),
        (ev("call.observed", coverage="BYPASS_DETECTED",
            details={"vendor": "desktop:mcp-config", "v_app": "cursor", "v_server": "jira",
                     "v_remote_host": "mcp.atlassian.com"}),
         "Possible bypass", "without going through Captain"),
        (ev("evaluation.scored", details={"overall": 0.3, "low": True}), "Low score",
         "below the alert level"),
        (ev("subject.erased", principal_id="priya@corp.example", details={"reason": "DSR-1"}),
         "Erased", "erased a person's identity"),
    ],
)  # fmt: skip
def test_examples_read_naturally(event: dict[str, Any], outcome: str, words: str) -> None:
    out = describe(event)
    assert out["outcome"] == outcome
    assert words in out["headline"], out["headline"]


def test_reason_is_explained_and_raw_codes_never_appear_in_the_headline() -> None:
    out = describe(ev("call.refused", model="vscode", reason_code="MODEL_NOT_ALLOWED"))
    assert out["why"] == "This AI model is not approved for this system."
    assert "MODEL_NOT_ALLOWED" not in out["headline"] and "call.refused" not in out["headline"]


def test_helpers() -> None:
    assert money(120) == "$0.0001" and money(1_234_567) == "$1.23" and money(None) == ""
    assert who_label(None) == "someone not identified"
    assert who_label("vendor-unmapped:ab12") == "an unrecognised user"
    inv = describe(ev("call.observed", principal_id="vendor-unmapped:ab12", coverage="OBSERVED",
                      details={"vendor": "desktop:inventory", "v_app": "vscode",
                               "v_running": True}))  # fmt: skip
    assert inv["headline"] == "VS Code was running on a computer used by an unrecognised user."
    assert inv["who"] == "An unrecognised user"
    nobody = describe(ev("call.refused", principal_id=None, reason_code="AUTH_INVALID"))
    assert nobody["headline"].startswith("Someone not identified's request")
    assert scope_label("ptool:ravi:jira.x") == "person ravi using tool jira.x"
    assert scope_label("org:dev-org") == "the whole organisation"
    assert found_label("PII:EMAIL_ADDRESS x1, PII:PHONE_NUMBER x2") == (
        "an email address and a phone number"
    )


def test_every_alert_kind_raised_by_captain_has_a_title_and_advice() -> None:
    kinds = ["budget.threshold", "budget.override", "register.downgrade", "approval.escalated",
             "evaluation.low_score", "ledger.lag", "import.failed"]  # fmt: skip
    assert set(kinds) <= set(ALERTS)
    for k in kinds:
        d = describe_alert(k)
        assert d["title"] and d["what_to_do"]
    assert describe_alert("something.new")["title"] == "Something new"
