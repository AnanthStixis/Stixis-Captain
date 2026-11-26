from __future__ import annotations

import pytest
from pydantic import ValidationError

from captain.core.models import (
    ActionType,
    Channel,
    ContentRef,
    CostEstimate,
    Credential,
    DecisionType,
    GovernanceRequest,
    ResolvedContext,
    RiskTier,
    TokenUsage,
)


def test_tier_fail_mode() -> None:
    assert RiskTier.TIER_1.fails_closed and RiskTier.TIER_2.fails_closed
    assert not RiskTier.TIER_3.fails_closed and not RiskTier.TIER_4.fails_closed


def test_model_call_requires_model(app_ctx: ResolvedContext) -> None:
    with pytest.raises(ValidationError):
        GovernanceRequest(
            trace_id="t", context=app_ctx, channel=Channel.API, action=ActionType.MODEL_CALL
        )


def test_tool_call_requires_tool(app_ctx: ResolvedContext) -> None:
    with pytest.raises(ValidationError):
        GovernanceRequest(
            trace_id="t", context=app_ctx, channel=Channel.MCP, action=ActionType.TOOL_CALL
        )


def test_content_ref_needs_text_or_hash() -> None:
    with pytest.raises(ValidationError):
        ContentRef()
    assert ContentRef(content_hmac="abc").text is None


def test_money_is_non_negative_int() -> None:
    with pytest.raises(ValidationError):
        CostEstimate(input_tokens=1, max_output_tokens=1, amount=-1, price_version="p1")
    with pytest.raises(ValidationError):
        CostEstimate(input_tokens=1, max_output_tokens=1, amount=1.5, price_version="p1")  # type: ignore[arg-type]


def test_secret_not_in_repr() -> None:
    c = Credential(kind="virtual_key", secret="super-secret-value")
    assert "super-secret-value" not in repr(c)


def test_models_are_immutable(app_ctx: ResolvedContext) -> None:
    with pytest.raises(ValidationError):
        app_ctx.project_id = "other"  # type: ignore[misc]


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=1, bogus=2)  # type: ignore[call-arg]


def test_total_tokens() -> None:
    assert TokenUsage(input_tokens=10, output_tokens=5).total_tokens == 15


def test_decision_enum_is_complete() -> None:
    assert {d.value for d in DecisionType} == {
        "ALLOW",
        "BLOCK",
        "REQUIRE_APPROVAL",
        "ALLOW_WITH_REDACTION",
        "ALLOW_WITH_LIMITS",
        "OBSERVE_ONLY",
    }
