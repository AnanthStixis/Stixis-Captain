# ruff: noqa: E501
"""Gemini native wire format (generateContent / streamGenerateContent) through the gateway."""

from __future__ import annotations

import json
from typing import Any

from tests.unit.test_messages import USAGE, Rig, text_chunks

U = "/v1beta/models/claude-sonnet:generateContent"
S = "/v1beta/models/claude-sonnet:streamGenerateContent?alt=sse"


def gem(r: Rig, url: str = U, **body: Any) -> Any:
    body.setdefault("contents", [{"role": "user", "parts": [{"text": "hi"}]}])
    return r.c.post(url, headers={"x-goog-api-key": r.key}, json=body)


def events(resp: Any) -> list[dict[str, Any]]:
    return [json.loads(b.removeprefix("data: ")) for b in resp.text.strip().split("\n\n") if b]


def test_text_reply_has_the_generate_content_shape() -> None:
    r = Rig()
    resp = gem(r, generationConfig={"maxOutputTokens": 50, "temperature": 0.2})
    assert resp.status_code == 200, resp.text
    b = resp.json()
    cand = b["candidates"][0]
    assert cand["content"] == {"role": "model", "parts": [{"text": "Hello there"}]}
    assert cand["finishReason"] == "STOP" and cand["index"] == 0
    assert b["usageMetadata"] == {
        "promptTokenCount": 12, "candidatesTokenCount": 5, "totalTokenCount": 17,
    }  # fmt: skip
    assert b["modelVersion"] == "claude-sonnet" and b["responseId"].startswith("resp_")
    assert resp.headers["X-Captain-Decision"] == "ALLOW"
    sent = r.fake.seen[0]
    assert sent["max_tokens"] == 50 and sent["temperature"] == 0.2
    assert r.h.budget("org:org-1")["spent"] > 0 and r.h.budget("org:org-1")["reserved"] == 0


def test_request_translation_system_roles_snake_case_and_function_round_trip() -> None:
    r = Rig()
    gem(
        r,
        system_instruction={"parts": [{"text": "Be brief."}]},
        contents=[
            {"role": "user", "parts": [{"text": "weather in Oslo?"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "Oslo"}}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "get_weather", "response": {"c": 3}}}]},
        ],
        tools=[{"functionDeclarations": [{"name": "get_weather", "description": "d", "parameters": {"type": "object"}}]}],
        toolConfig={"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["get_weather"]}},
    )  # fmt: skip
    sent = r.fake.seen[0]
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    call = sent["messages"][2]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather" and json.loads(
        call["function"]["arguments"]
    ) == {"city": "Oslo"}
    assert sent["messages"][3]["tool_call_id"] == call["id"]  # matched by name: no id was sent
    assert json.loads(sent["messages"][3]["content"]) == {"c": 3}
    assert sent["tools"][0]["function"]["name"] == "get_weather"
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}


def test_a_function_call_comes_back_as_a_function_call_part() -> None:
    r = Rig()
    r.fake.message = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_9", "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Oslo"}'}}],
    }  # fmt: skip
    r.fake.finish = "tool_calls"
    b = gem(r).json()
    part = b["candidates"][0]["content"]["parts"][0]
    assert part == {
        "functionCall": {"name": "get_weather", "args": {"city": "Oslo"}, "id": "call_9"}
    }
    assert b["candidates"][0]["finishReason"] == "STOP"


def test_unsupported_content_is_refused_not_dropped() -> None:
    r = Rig()
    img = gem(
        r,
        contents=[
            {"role": "user", "parts": [{"inlineData": {"mimeType": "image/png", "data": "AA"}}]}
        ],
    )
    assert img.status_code == 400 and img.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert "inlineData" in img.json()["error"]["message"]
    assert gem(r, tools=[{"googleSearch": {}}]).status_code == 400
    assert gem(r, cachedContent="cachedContents/x").status_code == 400
    assert gem(r, generationConfig={"candidateCount": 2}).status_code == 400
    assert gem(r, contents=[]).status_code == 400
    assert r.fake.seen == []  # nothing reached the model


def test_auth_is_header_only_and_errors_use_google_envelope() -> None:
    r = Rig()
    body = {"contents": [{"parts": [{"text": "hi"}]}]}
    bad = r.c.post(U, headers={"x-goog-api-key": "cpt_live_wrong"}, json=body)
    assert bad.status_code == 401 and bad.json()["error"]["status"] == "UNAUTHENTICATED"
    q = r.c.post(U + f"?key={r.key}", json=body)
    assert q.status_code == 401 and "header" in q.json()["error"]["message"]
    bearer = r.c.post(U, headers={"Authorization": f"Bearer {r.key}"}, json=body)
    assert bearer.status_code == 200
    assert gem(r, "/v1beta/models/not-in-registry:generateContent").status_code == 403
    assert gem(r, "/v1beta/models/claude-sonnet:generateAnswer").status_code == 404


def test_streaming_text_then_final_event_with_usage() -> None:
    r = Rig()
    r.fake.chunks = text_chunks("Hel", "lo")
    resp = gem(r, S)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    ev = events(resp)
    texts = [
        e["candidates"][0]["content"]["parts"][0]["text"]
        for e in ev
        if e["candidates"][0]["content"]["parts"]
    ]
    assert "".join(texts) == "Hello"
    last = ev[-1]
    assert last["candidates"][0]["finishReason"] == "STOP"
    assert last["usageMetadata"]["totalTokenCount"] == USAGE["total_tokens"]
    assert (
        gem(r, "/v1beta/models/claude-sonnet:streamGenerateContent").status_code == 400
    )  # no alt=sse


def test_streamed_function_call_fragments_are_sent_as_one_complete_part() -> None:
    r = Rig()
    r.fake.chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "get_weather", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"Oslo"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": USAGE},
    ]  # fmt: skip
    ev = events(gem(r, S))
    parts = [p for e in ev for p in e["candidates"][0]["content"]["parts"]]
    assert parts == [
        {"functionCall": {"name": "get_weather", "args": {"city": "Oslo"}, "id": "c1"}}
    ]
    assert ev[-1]["candidates"][0]["finishReason"] == "STOP"


def test_both_api_versions_are_served() -> None:
    """The official SDK defaults to /v1 (percent-encoding the colon); Google's REST examples
    use /v1beta."""
    r = Rig()
    body = {"contents": [{"parts": [{"text": "hi"}]}]}
    for prefix in ("/v1", "/v1beta"):
        ok = r.c.post(
            f"{prefix}/models/claude-sonnet%3AgenerateContent",
            headers={"x-goog-api-key": r.key},
            json=body,
        )
        assert ok.status_code == 200, (prefix, ok.text)


# ------------------------------------------------------------ embeddings and counting


class EmbedProvider:
    """A FakeProvider that can also embed (what the gateway needs for the embedding routes)."""

    def __init__(self, base: Any) -> None:
        self._b = base
        self.embedded: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._b, name)

    def embed(self, target: Any, body: dict[str, Any]) -> dict[str, Any]:
        self.embedded.append(body)
        dims = int(body.get("dimensions") or 3)
        return {
            "data": [{"index": i, "embedding": [0.5] * dims} for i, _ in enumerate(body["input"])],
            "usage": {"prompt_tokens": 7},
        }


def embedding_rig() -> tuple[Rig, EmbedProvider]:
    from fastapi.testclient import TestClient

    from captain.integrations.api_gateway.app import create_gateway_app

    r = Rig()
    prov = EmbedProvider(r.fake)
    r.c = TestClient(create_gateway_app(r.h.core, prov))  # type: ignore[arg-type]
    return r, prov


def test_batch_embed_and_embed_content_return_google_shapes_and_are_governed() -> None:
    r, prov = embedding_rig()
    base = "/v1/models/claude-sonnet"
    batch = r.c.post(
        f"{base}:batchEmbedContents",
        headers={"x-goog-api-key": r.key},
        json={"requests": [
            {"content": {"parts": [{"text": "a"}], "role": "user"}, "outputDimensionality": 4},
            {"content": {"parts": [{"text": "b"}]}, "outputDimensionality": 4},
        ]},
    )  # fmt: skip
    assert batch.status_code == 200, batch.text
    assert batch.json() == {"embeddings": [{"values": [0.5] * 4}, {"values": [0.5] * 4}]}
    assert prov.embedded[0] == {"input": ["a", "b"], "dimensions": 4}
    assert batch.headers["x-captain-decision"] == "ALLOW"
    assert r.h.budget("org:org-1")["spent"] > 0 and r.h.budget("org:org-1")["reserved"] == 0
    one = r.c.post(
        f"{base}:embedContent",
        headers={"x-goog-api-key": r.key},
        json={"content": {"parts": [{"text": "hello"}]}},
    )
    assert one.status_code == 200 and one.json() == {"embedding": {"values": [0.5] * 3}}


def test_embedding_requests_refuse_what_cannot_be_read_or_is_inconsistent() -> None:
    r, _ = embedding_rig()
    url, hdr = "/v1/models/claude-sonnet:batchEmbedContents", {"x-goog-api-key": r.key}
    img = {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": "AA"}}]}}
    assert r.c.post(url, headers=hdr, json={"requests": [img]}).status_code == 400
    mixed = {"requests": [{"content": {"parts": [{"text": "a"}]}, "outputDimensionality": 4},
                          {"content": {"parts": [{"text": "b"}]}, "outputDimensionality": 8}]}  # fmt: skip
    assert r.c.post(url, headers=hdr, json=mixed).status_code == 400
    assert r.c.post(url, headers=hdr, json={"requests": []}).status_code == 400
    assert r.c.post(url, json={"requests": []}).status_code == 401
    bad = r.c.post("/v1/models/not-in-registry:batchEmbedContents", headers=hdr,
                   json={"requests": [{"content": {"parts": [{"text": "a"}]}}]})  # fmt: skip
    assert bad.status_code == 403


def test_count_tokens_is_approximate_requires_a_key_and_calls_no_model() -> None:
    r = Rig()
    url = "/v1/models/claude-sonnet:countTokens"
    body = {"contents": [{"role": "user", "parts": [{"text": "x" * 40}]}]}
    ok = r.c.post(url, headers={"x-goog-api-key": r.key}, json=body)
    assert ok.status_code == 200 and ok.json() == {"totalTokens": 10}
    assert r.fake.seen == [] and r.h.budget("org:org-1")["spent"] == 0
    assert r.c.post(url, json=body).status_code == 401
