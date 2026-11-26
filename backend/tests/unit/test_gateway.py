"""API Gateway end to end: CaptainCore (in-memory) + mock OpenAI-compatible provider.
No real provider is called and no real money is spent."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from captain.core.decision_api import GatewayCoreAPI
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import AutonomyLevel, PrincipalType, RiskTier
from captain.core.registry.service import (
    InMemoryRegistryStore,
    ModelEntry,
    Provider,
    ProviderKind,
    RegistryService,
    Route,
)
from captain.core.usage.pricing import ModelPrice, PriceCatalog
from captain.devprovider import mock_transport
from captain.integrations.api_gateway.app import create_gateway_app
from captain.integrations.api_gateway.provider import OpenAICompatibleClient
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness
from tests.unit.test_engine import Guard, RedactPolicy

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ALIASES = ("claude-sonnet", "resilient", "always-down", "bad-request", "leak", "drop", "unpriced")


class FixedSecrets:
    def secret(self, ref: str) -> str:
        return "mock-key"


def registry() -> RegistryService:
    r = RegistryService(InMemoryRegistryStore())
    r.upsert_provider(
        Provider("mock", ProviderKind.OPENAI, "eu", "env:MOCK", base_url="http://mock/v1")
    )
    for alias, pm in [
        ("claude-sonnet", "mock-1"),
        ("p503", "fail-503"),
        ("ok2", "mock-2"),
        ("bad-request", "fail-400"),
        ("leak", "leak-secret"),
        ("drop", "drop-stream"),
        ("unpriced", "mock-3"),
        ("down2", "fail-503-b"),
    ]:
        r.upsert_model(ModelEntry(alias, "mock", pm, "eu"))
    r.upsert_route(Route("resilient", ("p503", "ok2")))
    r.upsert_route(Route("always-down", ("p503", "down2")))
    return r


def prices() -> PriceCatalog:
    c = PriceCatalog()
    for alias in (
        "claude-sonnet",
        "p503",
        "ok2",
        "resilient",
        "always-down",
        "down2",
        "bad-request",
        "leak",
        "drop",
    ):
        c.add(ModelPrice(alias, "mock", "v1", T0, input_per_m=3_000_000, output_per_m=15_000_000))
    return c


def gateway(
    tier: RiskTier = RiskTier.TIER_3, budget: int = 10_000_000, policy: Any = None
) -> tuple[CaptainCoreHarness, TestClient, dict[str, str]]:
    h = CaptainCoreHarness()
    m = h.core.m
    m.registry, m.prices = registry(), prices()
    if policy:
        m.policy = policy
    from captain.core.guardrails.service import GuardrailService

    m.guardrails = GuardrailService(Guard())
    ctx = make_context("app", tier, AutonomyLevel.A2, PrincipalType.APPLICATION)
    ctx = ctx.model_copy(
        update={
            "register_entry": ctx.register_entry.model_copy(
                update={"allowed_models": frozenset(ALIASES)}
            )
        }
    )
    h.register_key("k", ctx)
    h.set_budget("org:org-1", budget)
    provider = OpenAICompatibleClient(FixedSecrets(), transport=mock_transport())
    app = create_gateway_app(h.core, provider)
    return h, TestClient(app), {"Authorization": f"Bearer {h._keys['k']}"}  # noqa: SLF001


def chat(
    c: TestClient, hdr: dict[str, str], model: str = "claude-sonnet", text: str = "hello", **kw: Any
) -> Any:
    return c.post(
        "/v1/chat/completions",
        headers=hdr,
        json={
            "model": model,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": 50,
            **kw,
        },
    )


def ledger_types(h: CaptainCoreHarness) -> list[tuple[str, dict[str, Any]]]:
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    return [(t, b) for (_p, t, b) in store.outbox.values()]


def test_core_satisfies_gateway_contract() -> None:
    h, _, _ = gateway()
    assert isinstance(h.core, GatewayCoreAPI)


def test_allowed_call_end_to_end() -> None:
    h, c, hdr = gateway()
    r = chat(c, hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "Mock reply to: hello"
    assert body["model"] == "claude-sonnet"  # alias, not the provider's model id
    assert r.headers["X-Captain-Decision"] == "ALLOW" and r.headers["X-Captain-Risk"]
    settled = [b for t, b in ledger_types(h) if t == "usage.settled"]
    assert (
        settled
        and settled[0]["usage"]["source"] == "PROVIDER_REPORTED"
        and settled[0]["usage"]["cost"] > 0
    )
    assert h.budget("org:org-1")["spent"] == settled[0]["usage"]["cost"]
    assert h.budget("org:org-1")["reserved"] == 0


def test_errors_use_openai_shape() -> None:
    _, c, hdr = gateway()
    r = c.post(
        "/v1/chat/completions",
        json={"model": "claude-sonnet", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 401 and r.json()["error"]["code"] == "AUTH_INVALID"
    r = chat(c, hdr, model="not-in-registry")
    assert r.status_code == 403 and r.json()["error"]["code"] == "MODEL_NOT_REGISTERED"


def test_fallback_to_secondary_on_retryable_error() -> None:
    h, c, hdr = gateway()
    r = chat(c, hdr, model="resilient")
    assert r.status_code == 200 and r.json()["model"] == "resilient"
    assert h.budget("org:org-1")["spent"] > 0


def test_all_routes_down_releases_reservation() -> None:
    h, c, hdr = gateway()
    r = chat(c, hdr, model="always-down")
    assert r.status_code == 502 and r.json()["error"]["code"] == "PROVIDER_ERROR"
    assert h.budget("org:org-1") == {"limit": 10_000_000, "spent": 0, "reserved": 0}
    assert "usage.released" in [t for t, _ in ledger_types(h)]


def test_non_retryable_error_does_not_fall_back() -> None:
    _, c, hdr = gateway()
    assert chat(c, hdr, model="bad-request").status_code == 502


def test_output_secret_blocked_and_recorded() -> None:
    h, c, hdr = gateway(tier=RiskTier.TIER_2)
    r = chat(c, hdr, model="leak")
    assert r.status_code == 403 and r.json()["error"]["code"] == "OUTPUT_BLOCKED"
    assert "AKIA" not in r.text
    refused = [b for t, b in ledger_types(h) if t == "call.refused"]
    assert refused and refused[-1]["reason_code"] == "OUTPUT_BLOCKED"
    assert h.budget("org:org-1")["spent"] > 0  # the provider already charged; Captain records it


def test_pii_is_masked_before_reaching_provider() -> None:
    _, c, hdr = gateway(policy=RedactPolicy())
    r = chat(c, hdr, text="email ravi@example.com")
    assert r.json()["choices"][0]["message"]["content"] == "Mock reply to: email <EMAIL_ADDRESS_1>"
    assert r.headers["X-Captain-Decision"] == "ALLOW_WITH_REDACTION"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            {
                "messages": [
                    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
                ]
            },
            "UNSUPPORTED_CONTENT",
        ),
        ({"messages": [{"role": "user", "content": "hi"}], "n": 2}, "INVALID_REQUEST"),
    ],
)
def test_unsupported_requests_refused(payload: dict[str, Any], code: str) -> None:
    _, c, hdr = gateway()
    r = c.post("/v1/chat/completions", headers=hdr, json={"model": "claude-sonnet", **payload})
    assert r.status_code == 400 and r.json()["error"]["code"] == code


def test_budget_and_price_refusals() -> None:
    _, c, hdr = gateway(budget=10)
    r = chat(c, hdr)
    assert r.status_code == 429 and r.json()["error"]["code"] == "BUDGET_EXHAUSTED"
    _, c, hdr = gateway()
    r = chat(c, hdr, model="unpriced")
    assert r.status_code == 403 and r.json()["error"]["code"] == "MODEL_NOT_PRICED"


def _sse(r: Any) -> list[Any]:
    out = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            d = line[6:]
            out.append(d if d == "[DONE]" else json.loads(d))
    return out


@pytest.mark.parametrize("tier", [RiskTier.TIER_1, RiskTier.TIER_3])
def test_streaming_by_tier(tier: RiskTier) -> None:
    h, c, hdr = gateway(tier=tier)
    r = chat(c, hdr, text="stream please", stream=True, stream_options={"include_usage": True})
    events = _sse(r)
    assert events[-1] == "[DONE]"
    text = "".join(
        e["choices"][0]["delta"].get("content", "")
        for e in events[:-1]
        if isinstance(e, dict) and e.get("choices")
    )
    assert text == "Mock reply to: stream please"
    assert any(isinstance(e, dict) and e.get("usage") for e in events)
    settled = [b for t, b in ledger_types(h) if t == "usage.settled"]
    assert settled[0]["usage"]["source"] == "PROVIDER_REPORTED"
    assert settled[0]["usage"]["time_to_first_token_ms"] is not None


def test_streaming_output_block() -> None:
    _, c, hdr = gateway(tier=RiskTier.TIER_1)  # buffer mode: nothing leaks before the check
    events = _sse(chat(c, hdr, model="leak", stream=True))
    assert any(
        isinstance(e, dict) and e.get("error", {}).get("code") == "OUTPUT_BLOCKED" for e in events
    )
    assert not any(isinstance(e, dict) and "AKIA" in json.dumps(e) for e in events)


def test_streaming_mid_stream_drop_is_charged_as_estimate() -> None:
    h, c, hdr = gateway()
    _sse(chat(c, hdr, model="drop", stream=True))
    settled = [b for t, b in ledger_types(h) if t == "usage.settled"]
    assert settled and settled[0]["usage"]["source"] == "ESTIMATED"  # never presented as exact


def test_models_and_status() -> None:
    _, c, hdr = gateway()
    ids = [m["id"] for m in c.get("/v1/models", headers=hdr).json()["data"]]
    assert "claude-sonnet" in ids
    s = c.get("/v1/governance/status", headers=hdr).json()
    assert s["risk_tier"] == "TIER_3" and s["register_id"] == "AI-app"


def test_official_openai_sdk_works_unchanged() -> None:
    """An app using the OpenAI Python SDK only changes base_url and api_key."""
    from openai import OpenAI, PermissionDeniedError

    _, c, hdr = gateway()
    client = OpenAI(
        base_url="http://testserver/v1", api_key=hdr["Authorization"][7:], http_client=c
    )
    resp = client.chat.completions.create(
        model="claude-sonnet", messages=[{"role": "user", "content": "hi sdk"}], max_tokens=20
    )
    assert resp.choices[0].message.content == "Mock reply to: hi sdk" and resp.usage
    stream = client.chat.completions.create(
        model="claude-sonnet", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    assert (
        "".join(ch.choices[0].delta.content or "" for ch in stream if ch.choices)
        == "Mock reply to: hi"
    )
    with pytest.raises(PermissionDeniedError) as e:
        client.chat.completions.create(
            model="not-in-registry", messages=[{"role": "user", "content": "x"}]
        )
    assert "MODEL_NOT_REGISTERED" in str(e.value)


def test_idempotency_key_prevents_double_charge() -> None:
    h, c, hdr = gateway()
    first = chat(c, {**hdr, "Idempotency-Key": "order-42"})
    spent = h.budget("org:org-1")["spent"]
    again = chat(c, {**hdr, "Idempotency-Key": "order-42"})
    assert first.status_code == 200 and again.status_code == 409
    assert again.json()["error"]["code"] == "IDEMPOTENCY_REPLAY"
    assert h.budget("org:org-1")["spent"] == spent  # not charged twice
    assert chat(c, {**hdr, "Idempotency-Key": "order-43"}).status_code == 200


def test_env_resolver_reads_given_mapping_and_fails_clearly() -> None:
    from captain.integrations.api_gateway.provider import EnvSecretResolver, ProviderError

    r = EnvSecretResolver({"MOCK_KEY": "abc"})
    assert r.secret("env:MOCK_KEY") == "abc"
    with pytest.raises(ProviderError, match="not set"):
        r.secret("env:MISSING")
    with pytest.raises(ProviderError, match="not supported"):
        r.secret("vault:x")


def test_provider_failures_are_logged_for_operators(caplog: pytest.LogCaptureFixture) -> None:
    _, c, hdr = gateway()
    with caplog.at_level("WARNING", logger="captain.gateway"):
        chat(c, hdr, model="always-down")
    assert "provider call failed" in caplog.text and "mock-key" not in caplog.text


def test_ide_continue_config_lists_allowed_models_without_the_key() -> None:
    import yaml

    _, c, hdr = gateway()
    r = c.get("/v1/ide/continue", headers=hdr)
    assert r.status_code == 200
    cfg = yaml.safe_load(r.text)
    assert {m["model"] for m in cfg["models"]} == set(ALIASES)
    assert all(m["provider"] == "openai" and m["apiBase"].endswith("/v1") for m in cfg["models"])
    assert hdr["Authorization"][7:] not in r.text
    assert c.get("/v1/ide/continue").status_code == 401


def test_usage_reports_own_budgets_and_hides_broader_ones() -> None:
    h, c, hdr = gateway()
    h.set_budget("principal:app", 2_000_000)
    assert chat(c, hdr).status_code == 200
    u = c.get("/v1/usage", headers=hdr).json()
    assert u["object"] == "usage" and u["principal"] == "app" and u["currency"] == "USD"
    (b,) = u["budgets"]  # only the caller's own budget is named
    assert b["scope"] == "principal:app" and b["period"] == "monthly"
    assert b["limit"] == 2_000_000 and b["spent"] > 0 and b["reserved"] == 0
    assert b["remaining"] == b["limit"] - b["spent"]
    assert b["percent_used"] == round(100 * b["spent"] / b["limit"], 1)
    # the organisation budget (10,000,000) is larger, so the caller's own is the tightest
    assert u["tightest"] == {"kind": "principal", "remaining": b["remaining"]}
    assert "org:" not in json.dumps(u)


def test_usage_tightest_can_be_a_broader_budget_without_naming_its_totals() -> None:
    h, c, hdr = gateway(budget=1_000_000)
    h.set_budget("principal:app", 5_000_000)
    u = c.get("/v1/usage", headers=hdr).json()
    assert u["tightest"] == {"kind": "org", "remaining": 1_000_000}
    assert all(not b["scope"].startswith("org:") for b in u["budgets"])


def test_usage_needs_a_valid_key() -> None:
    _, c, _ = gateway()
    assert c.get("/v1/usage").status_code == 401
    assert c.get("/v1/usage", headers={"Authorization": "Bearer cpt_live_bad"}).status_code == 401


# ------------------------------------------------------------------ embeddings


def test_embeddings_are_governed_priced_and_masked_like_chat() -> None:
    h, tc, hdr = gateway(budget=100_000_000)
    r = tc.post(
        "/v1/embeddings",
        headers=hdr,
        json={"model": "claude-sonnet", "input": ["first text", "second text"], "dimensions": 4},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["object"] == "list" and len(d["data"]) == 2 and len(d["data"][0]["embedding"]) == 4
    assert d["model"] == "claude-sonnet" and d["usage"]["prompt_tokens"] > 0
    assert r.headers["x-captain-decision"] == "ALLOW" and r.headers["x-captain-request-id"]
    spent = h.budget("org:org-1")
    assert spent["spent"] > 0 and spent["reserved"] == 0  # settled on input tokens, none left held


def test_embeddings_input_is_masked_before_the_provider_sees_it() -> None:
    body = {"model": "claude-sonnet", "input": "mail bob@example.com"}
    _, tc_open, hdr_open = gateway()  # policy allows as is: the raw text is embedded
    _, tc_mask, hdr_mask = gateway(policy=RedactPolicy())
    raw = tc_open.post("/v1/embeddings", headers=hdr_open, json=body)
    masked = tc_mask.post("/v1/embeddings", headers=hdr_mask, json=body)
    assert raw.status_code == masked.status_code == 200
    assert masked.headers["x-captain-decision"] == "ALLOW_WITH_REDACTION"
    # The mock provider's vector depends on the exact text it received: different text in,
    # different vector out, so the address was replaced before the provider call.
    assert raw.json()["data"][0]["embedding"] != masked.json()["data"][0]["embedding"]


def test_embeddings_refuse_bad_input_unknown_model_and_bad_key() -> None:
    _, tc, hdr = gateway()
    post = lambda **kw: tc.post("/v1/embeddings", headers=hdr, json=kw)  # noqa: E731
    assert post(model="claude-sonnet", input=[]).status_code == 400
    assert post(model="claude-sonnet", input=["x"] * 300).status_code == 400
    assert post(model="claude-sonnet", input=[1, 2, 3]).status_code == 422  # token ids: refused
    r = post(model="not-in-registry", input="x")
    assert r.status_code == 403 and r.json()["error"]["code"] == "MODEL_NOT_REGISTERED"
    assert tc.post("/v1/embeddings", json={"model": "m", "input": "x"}).status_code == 401


# --------------------------------------------------------------- browser check


def test_browser_check_allows_masks_and_blocks_and_records_on_the_browser_channel() -> None:
    h, tc, hdr = gateway()
    ok = tc.post("/v1/browser/check", headers=hdr, json={"site": "chatgpt.com", "text": "hello"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["allowed"] is True and ok.json()["decision"] == "ALLOW"
    assert ok.json()["masked_text"] is None and ok.headers["x-captain-request-id"]
    _, tc2, hdr2 = gateway(policy=RedactPolicy())
    m = tc2.post(
        "/v1/browser/check",
        headers=hdr2,
        json={"site": "claude.ai", "text": "mail bob@example.com"},
    ).json()
    assert m["allowed"] is True and m["decision"] == "ALLOW_WITH_REDACTION"
    assert m["masked_text"] and "bob@example.com" not in m["masked_text"]
    from captain.core.identity.killswitch import KillScope

    h.core.kill(KillScope.CHANNEL, "BROWSER", "stop", "admin")
    b = tc.post(
        "/v1/browser/check", headers=hdr, json={"site": "chatgpt.com", "text": "hello"}
    ).json()
    assert b["allowed"] is False and b["decision"] == "BLOCK"
    assert b["reason_code"] == "KILL_SWITCH_ACTIVE" and b["masked_text"] is None
    channels = {
        body.get("channel") for (_p, _t, body) in h.built.infra.ledger_store.outbox.values()
    }  # type: ignore[attr-defined]
    assert "BROWSER" in channels
    assert "hello" not in str(list(h.built.infra.ledger_store.outbox.values()))  # type: ignore[attr-defined]


def test_browser_check_needs_a_key_and_valid_input() -> None:
    _, tc, hdr = gateway()
    assert tc.post("/v1/browser/check", json={"site": "a.com", "text": "x"}).status_code == 401
    assert (
        tc.post("/v1/browser/check", headers=hdr, json={"site": "a b", "text": "x"}).status_code
        == 422
    )
    assert (
        tc.post("/v1/browser/check", headers=hdr, json={"site": "a.com", "text": ""}).status_code
        == 422
    )


def test_streamed_tool_calls_and_finish_reason_pass_through() -> None:
    """Function calling over the OpenAI wire format: the model's tool-call deltas reach the
    caller unchanged and the stream ends with finish_reason tool_calls (not always stop)."""

    def part(delta: dict[str, Any], finish: str | None = None) -> str:
        c: dict[str, Any] = {"index": 0, "delta": delta}
        if finish:
            c["finish_reason"] = finish
        return "data: " + json.dumps({"choices": [c]}) + "\n\n"

    def fn(name: str | None, args: str) -> dict[str, Any]:
        return {"name": name, "arguments": args} if name else {"arguments": args}

    first = {"index": 0, "id": "call_1", "type": "function", "function": fn("get_weather", "")}
    sse = (
        part({"role": "assistant", "tool_calls": [first]})
        + part({"tool_calls": [{"index": 0, "function": fn(None, '{"city":')}]})
        + part({"tool_calls": [{"index": 0, "function": fn(None, '"Oslo"}')}]}, "tool_calls")
        + "data: "
        + json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 7}})
        + "\n\ndata: [DONE]\n\n"
    )
    h, _, hdr = gateway(budget=100_000_000)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=sse.encode()))
    app = create_gateway_app(h.core, OpenAICompatibleClient(FixedSecrets(), transport=transport))
    tc = TestClient(app)
    r = tc.post(
        "/v1/chat/completions",
        headers=hdr,
        json={
            "model": "claude-sonnet",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "weather in Oslo?"}],
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
        },
    )
    assert r.status_code == 200, r.text
    objs = [
        json.loads(x[5:])
        for x in r.text.split("\n\n")
        if x.startswith("data:") and "[DONE]" not in x
    ]
    calls = [
        tc_ for o in objs for c in o["choices"] for tc_ in (c["delta"].get("tool_calls") or [])
    ]
    assert calls[0]["id"] == "call_1" and calls[0]["function"]["name"] == "get_weather"
    assert "".join(c["function"].get("arguments", "") for c in calls) == '{"city":"Oslo"}'
    finishes = [c["finish_reason"] for o in objs for c in o["choices"] if c.get("finish_reason")]
    assert finishes == ["tool_calls"]
    assert h.budget("org:org-1")["spent"] > 0  # the tool-call tokens were charged


# ------------------------------------------------------------------ rate limits


def test_a_request_rate_limit_answers_429_with_the_reason_and_costs_nothing() -> None:
    from captain.core.budget.model import RateLimitDefinition

    h, tc, hdr = gateway(budget=100_000_000)
    h.built.infra.budget_catalog.add_rate(  # type: ignore[attr-defined]
        RateLimitDefinition("principal:app", requests_per_minute=2)
    )
    body = {"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]}
    codes = [tc.post("/v1/chat/completions", headers=hdr, json=body).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]
    refused = tc.post("/v1/chat/completions", headers=hdr, json=body)
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    spent_after_two = h.budget("org:org-1")["spent"]
    tc.post("/v1/chat/completions", headers=hdr, json=body)
    assert h.budget("org:org-1")["spent"] == spent_after_two  # refusals cost nothing
    assert h.budget("org:org-1")["reserved"] == 0  # and hold nothing back


def test_a_token_rate_limit_refuses_a_call_that_could_exceed_it() -> None:
    from captain.core.budget.model import RateLimitDefinition

    h, tc, hdr = gateway(budget=100_000_000)
    h.built.infra.budget_catalog.add_rate(  # type: ignore[attr-defined]
        RateLimitDefinition("principal:app", tokens_per_minute=100)
    )
    big = {"model": "claude-sonnet", "max_tokens": 5000,
           "messages": [{"role": "user", "content": "hi"}]}  # fmt: skip
    r = tc.post("/v1/chat/completions", headers=hdr, json=big)
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    small = {**big, "max_tokens": 10}
    assert tc.post("/v1/chat/completions", headers=hdr, json=small).status_code == 200


def test_the_limit_is_per_scope_so_another_caller_is_not_slowed() -> None:
    from captain.core.budget.model import RateLimitDefinition

    h, tc, hdr = gateway(budget=100_000_000)
    h.built.infra.budget_catalog.add_rate(  # type: ignore[attr-defined]
        RateLimitDefinition("principal:someone-else", requests_per_minute=1)
    )
    body = {"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]}
    assert all(
        tc.post("/v1/chat/completions", headers=hdr, json=body).status_code == 200 for _ in range(3)
    )
