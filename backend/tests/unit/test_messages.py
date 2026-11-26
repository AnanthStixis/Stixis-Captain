"""Anthropic Messages endpoint (/v1/messages): translation, streaming events, refusals,
output guard, budget. A fake OpenAI-compatible provider stands in for the model."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from fastapi.testclient import TestClient

from captain.core.guardrails.service import GuardrailService
from captain.core.models import AutonomyLevel, PrincipalType, ProviderTarget, RiskTier
from captain.integrations.api_gateway.app import create_gateway_app
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness
from tests.unit.test_engine import Guard
from tests.unit.test_gateway import ALIASES, prices, registry

USAGE = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}


class FakeProvider:
    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.message: dict[str, Any] = {"role": "assistant", "content": "Hello there"}
        self.finish = "stop"
        self.chunks: list[dict[str, Any]] = []

    def complete(self, target: ProviderTarget, body: dict[str, Any]) -> dict[str, Any]:
        self.seen.append(body)
        return {
            "choices": [{"index": 0, "message": dict(self.message), "finish_reason": self.finish}],
            "usage": USAGE,
        }

    def stream(self, target: ProviderTarget, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        self.seen.append(body)
        yield from self.chunks


def text_chunks(*pieces: str) -> list[dict[str, Any]]:
    return [
        *({"choices": [{"delta": {"content": p}}]} for p in pieces),
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": USAGE},
    ]


class Rig:
    def __init__(self, tier: RiskTier = RiskTier.TIER_3) -> None:
        self.h = CaptainCoreHarness()
        m = self.h.core.m
        m.registry, m.prices = registry(), prices()
        m.guardrails = GuardrailService(Guard())
        ctx = make_context("dev", tier, AutonomyLevel.A2, PrincipalType.USER)
        ctx = ctx.model_copy(
            update={
                "register_entry": ctx.register_entry.model_copy(
                    update={"allowed_models": frozenset(ALIASES)}
                )
            }
        )
        self.h.register_key("k", ctx)
        self.h.set_budget("org:org-1", 10_000_000)
        self.fake = FakeProvider()
        self.c = TestClient(create_gateway_app(self.h.core, self.fake))
        self.key = self.h._keys["k"]  # noqa: SLF001
        self.hdr = {"x-api-key": self.key, "anthropic-version": "2023-06-01"}

    def post(self, **body: Any) -> Any:
        body.setdefault("model", "claude-sonnet")
        body.setdefault("max_tokens", 100)
        body.setdefault("messages", [{"role": "user", "content": "hi"}])
        return self.c.post("/v1/messages", headers=self.hdr, json=body)


def events(resp: Any) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in resp.text.strip().split("\n\n"):
        lines = block.split("\n")
        name = lines[0].removeprefix("event: ")
        out.append((name, json.loads(lines[1].removeprefix("data: "))))
    return out


# ------------------------------------------------------------------ non-stream


def test_text_reply_has_the_messages_shape() -> None:
    r = Rig()
    resp = r.post()
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message" and body["role"] == "assistant"
    assert body["model"] == "claude-sonnet" and body["id"].startswith("msg_")
    assert body["content"] == [{"type": "text", "text": "Hello there"}]
    assert body["stop_reason"] == "end_turn" and body["stop_sequence"] is None
    assert body["usage"] == {"input_tokens": 12, "output_tokens": 5}
    assert resp.headers["X-Captain-Decision"] == "ALLOW"
    assert r.h.budget("org:org-1")["spent"] > 0 and r.h.budget("org:org-1")["reserved"] == 0


def test_both_credential_styles_and_envelope_errors() -> None:
    r = Rig()
    bearer = {"Authorization": f"Bearer {r.key}"}
    ok = r.c.post(
        "/v1/messages",
        headers=bearer,
        json={
            "model": "claude-sonnet",
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert ok.status_code == 200
    no_key = r.c.post("/v1/messages", json={"model": "claude-sonnet", "messages": []})
    assert no_key.status_code == 401
    assert no_key.json()["type"] == "error"
    assert no_key.json()["error"]["type"] == "authentication_error"
    unknown = r.post(model="not-in-registry")
    assert unknown.status_code == 403 and unknown.json()["error"]["type"] == "permission_error"
    assert unknown.json()["request_id"].startswith("req_")
    assert r.post(messages=[]).json()["error"]["type"] == "invalid_request_error"
    bad_json = r.c.post("/v1/messages", headers=r.hdr, content=b"{nope")
    assert bad_json.status_code == 400


def test_request_translation() -> None:
    r = Rig()
    r.post(
        system=[{"type": "text", "text": "Be brief."}, {"type": "text", "text": "No emoji."}],
        messages=[
            {"role": "system", "content": "extra rule"},
            {"role": "user", "content": "weather in Pune?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "weather",
                        "input": {"city": "Pune"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "31C",
                        "is_error": False,
                    },
                    {"type": "text", "text": "thanks"},
                ],
            },
        ],
        tools=[{"name": "weather", "description": "d", "input_schema": {"type": "object"}}],
        tool_choice={"type": "any", "disable_parallel_tool_use": True},
        stop_sequences=["END"],
        temperature=0.2,
        thinking={"type": "adaptive"},  # an ignorable hint
        metadata={"user_id": "x"},
    )
    sent = r.fake.seen[0]
    assert [m["role"] for m in sent["messages"]] == [
        "system", "system", "user", "assistant", "tool", "user",
    ]  # fmt: skip
    assert sent["messages"][0]["content"] == "Be brief.\nNo emoji."
    assistant = sent["messages"][3]
    assert assistant["content"] == "Checking."
    assert assistant["tool_calls"][0]["function"] == {
        "name": "weather",
        "arguments": '{"city": "Pune"}',
    }
    assert sent["messages"][4] == {"role": "tool", "tool_call_id": "toolu_1", "content": "31C"}
    assert sent["tools"][0]["function"]["name"] == "weather"
    assert sent["tool_choice"] == "required" and sent["parallel_tool_calls"] is False
    assert sent["stop"] == ["END"] and sent["temperature"] == 0.2
    assert "thinking" not in sent and "metadata" not in sent


def test_tool_use_reply() -> None:
    r = Rig()
    r.fake.message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_9",
                "type": "function",
                "function": {"name": "weather", "arguments": '{"city": "Pune"}'},
            }
        ],
    }
    r.fake.finish = "tool_calls"
    body = r.post(tools=[{"name": "weather", "input_schema": {"type": "object"}}]).json()
    assert body["stop_reason"] == "tool_use"
    assert body["content"] == [
        {"type": "tool_use", "id": "call_9", "name": "weather", "input": {"city": "Pune"}}
    ]
    r.fake.message["tool_calls"][0]["function"]["arguments"] = "not json"
    bad = r.post(tools=[{"name": "weather", "input_schema": {"type": "object"}}])
    assert bad.status_code == 502 and bad.json()["error"]["type"] == "api_error"


def test_unsupported_content_is_refused_not_dropped() -> None:
    r = Rig()
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
    }
    for kwargs in [
        {"messages": [{"role": "user", "content": [image]}]},
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "x", "signature": "s"}],
                }
            ]
        },
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
    ]:
        resp = r.post(**kwargs)
        assert resp.status_code == 400 and resp.json()["error"]["type"] == "invalid_request_error"
    assert r.fake.seen == []  # nothing reached the provider


def test_policy_refusal_uses_the_error_envelope() -> None:
    r = Rig()
    from captain.core.identity.killswitch import KillScope

    r.h.core.kill(KillScope.CHANNEL, "IDE", "stop all IDE traffic", "admin")
    resp = r.post()
    assert resp.status_code == 403 and resp.json()["error"]["type"] == "permission_error"
    assert "Kill switch" in resp.json()["error"]["message"]
    assert r.fake.seen == []


def test_secret_in_reply_is_blocked_at_tier_2() -> None:
    r = Rig(RiskTier.TIER_2)
    r.fake.message = {"role": "assistant", "content": "key AKIAIOSFODNN7EXAMPLE"}
    resp = r.post()
    assert resp.status_code == 403 and "AKIA" not in resp.text
    assert r.h.budget("org:org-1")["reserved"] == 0  # the provider call was charged, not held


def test_count_tokens_is_approximate_and_needs_a_key() -> None:
    r = Rig()
    body = {"model": "claude-sonnet", "messages": [{"role": "user", "content": "x" * 400}]}
    resp = r.c.post("/v1/messages/count_tokens", headers=r.hdr, json=body)
    assert resp.status_code == 200 and 90 <= resp.json()["input_tokens"] <= 110
    assert r.c.post("/v1/messages/count_tokens", json=body).status_code == 401
    assert r.fake.seen == []  # no model call, nothing reserved


# ------------------------------------------------------------------- streaming


def test_text_stream_event_sequence() -> None:
    r = Rig()
    r.fake.chunks = text_chunks("Hel", "lo ", "there")
    resp = r.post(stream=True)
    assert resp.headers["content-type"].startswith("text/event-stream")
    ev = events(resp)
    names = [n for n, _ in ev]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    assert names[1] == "content_block_start" and "content_block_stop" in names
    assert ev[0][1]["message"]["model"] == "claude-sonnet" and ev[0][1]["message"]["content"] == []
    text = "".join(d["delta"]["text"] for n, d in ev if n == "content_block_delta")
    assert text == "Hello there"
    delta = ev[-2][1]
    assert delta["delta"]["stop_reason"] == "end_turn" and delta["usage"] == {"output_tokens": 5}
    assert r.h.budget("org:org-1")["spent"] > 0 and r.h.budget("org:org-1")["reserved"] == 0


def test_tool_use_stream() -> None:
    r = Rig()
    r.fake.chunks = [
        {"choices": [{"delta": {"content": "Checking."}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "weather", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },  # fmt: skip
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}}
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "Pune"}'}}]}}
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": USAGE},
    ]
    ev = events(
        r.post(stream=True, tools=[{"name": "weather", "input_schema": {"type": "object"}}])
    )
    starts = [(d["index"], d["content_block"]["type"]) for n, d in ev if n == "content_block_start"]
    assert starts == [(0, "text"), (1, "tool_use")]
    stops = [d["index"] for n, d in ev if n == "content_block_stop"]
    assert stops == [0, 1]  # each block closed before the next opens / the message ends
    tool_start = next(d for n, d in ev if n == "content_block_start" and d["index"] == 1)
    assert tool_start["content_block"] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "weather",
        "input": {},
    }
    partial = "".join(
        d["delta"]["partial_json"] for n, d in ev if n == "content_block_delta" and d["index"] == 1
    )
    assert json.loads(partial) == {"city": "Pune"}
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_stream_is_stopped_on_a_secret_at_tier_2() -> None:
    r = Rig(RiskTier.TIER_2)
    r.fake.chunks = text_chunks(
        "here is the key AKIAIOSFODNN7EXAMPLE. ", "more text follows after it."
    )
    resp = r.post(stream=True)
    assert "AKIA" not in resp.text
    ev = events(resp)
    assert ev[-1][0] == "error" and ev[-1][1]["error"]["type"] == "api_error"
    assert "message_stop" not in [n for n, _ in ev]
    assert r.h.budget("org:org-1")["reserved"] == 0
