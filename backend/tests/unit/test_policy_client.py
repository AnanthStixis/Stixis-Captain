from __future__ import annotations

import json

import httpx
import pytest

from captain.core.models import FindingType, GuardrailFinding, ResolvedContext
from captain.core.policy.opa import (
    DECISION_PATH,
    OpaPolicyEngine,
    PolicyUnavailableError,
    build_input,
)
from tests.conftest import model_call

GOOD = {
    "decision": "ALLOW",
    "reason_code": "ALLOWED",
    "explanation": "ok",
    "policy_ids": ["captain.tier.TIER_2", "captain.rule.allow"],
    "policy_bundle": "captain-tier-policies",
    "policy_version": "0.1.0",
    "limits": {"max_output_tokens": 4096},
}


def engine(handler: httpx.MockTransport) -> OpaPolicyEngine:
    return OpaPolicyEngine(
        "http://opa", client=httpx.Client(base_url="http://opa", transport=handler)
    )


def test_input_contract_has_no_content(app_ctx: ResolvedContext) -> None:
    f = GuardrailFinding(
        type=FindingType.PII, entity="EMAIL_ADDRESS", detector="regex", content_index=0
    )
    payload = build_input(model_call(app_ctx), (f,))
    assert payload["register"]["tier"] == "TIER_2"
    assert payload["findings"] == [{"type": "PII", "entity": "EMAIL_ADDRESS", "score": 1.0}]
    assert "text" not in json.dumps(payload)


def test_parses_result(app_ctx: ResolvedContext) -> None:
    seen: dict[str, object] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, json={"result": GOOD})

    r = engine(httpx.MockTransport(h)).evaluate(build_input(model_call(app_ctx), ()))
    assert seen["path"] == DECISION_PATH and r.policy_version == "0.1.0"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={}),  # undefined → bundle missing
        httpx.Response(500),
        httpx.Response(200, json={"result": {**GOOD, "decision": "MAYBE"}}),
        httpx.Response(200, json={"result": {**GOOD, "reason_code": "no matching rule"}}),
        httpx.Response(200, content=b"not json"),
    ],
)
def test_bad_answers_are_unavailable_never_allow(
    app_ctx: ResolvedContext, response: httpx.Response
) -> None:
    with pytest.raises(PolicyUnavailableError):
        engine(httpx.MockTransport(lambda _: response)).evaluate(
            build_input(model_call(app_ctx), ())
        )


def test_network_error(app_ctx: ResolvedContext) -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(PolicyUnavailableError):
        engine(httpx.MockTransport(boom)).evaluate(build_input(model_call(app_ctx), ()))
