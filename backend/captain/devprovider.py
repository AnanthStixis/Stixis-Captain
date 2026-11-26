"""Mock OpenAI-compatible model provider for tests and local runs (no real spend).

    python -m uvicorn captain.devprovider:app --port 8090

Behaviour (deterministic):
- reply = "Mock reply to: <start of the last user message>"
- usage = prompt tokens ≈ chars/4, completion tokens = words in the reply
- model names starting with "fail-503" / "fail-400" return that HTTP error
- model "leak-secret" replies with a fake AWS key (to test output guardrails)
- model "drop-stream" stops a stream midway (to test mid-stream failure handling)
- model "mock-judge" answers like a live-evaluation judge (JSON scores)
- POST /v1/classify is a mock guard model (flags "ignore previous instructions")
- streaming sends word chunks, then a usage chunk, then [DONE] (OpenAI format)
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

FAKE_SECRET = "AKIAABCDEFGHIJKLMNOP"  # noqa: S105 — fake test key (allowlisted in .gitleaks.toml)


def _judge(body: dict[str, Any]) -> str:
    """Mock evaluation judge (model "mock-judge"): good scores, unless the conversation it
    is asked to score contains the word LOWSCORE (to test the low-score alert)."""
    text = " ".join(str(m.get("content", "")) for m in body.get("messages", []))
    if "LOWSCORE" in text:
        return '{"grounded": 0.3, "policy": 0.9, "safety": 1.0}'
    return '{"grounded": 0.92, "policy": 0.95, "safety": 1.0}'


def _reply(body: dict[str, Any]) -> str:
    if body.get("model") == "mock-judge":
        return _judge(body)
    if body.get("model") == "leak-secret":
        return f"Here is the key you asked for: {FAKE_SECRET}"
    msgs = [m for m in body.get("messages", []) if m.get("role") == "user"]
    last = msgs[-1]["content"] if msgs else ""
    if isinstance(last, list):  # content parts
        last = " ".join(p.get("text", "") for p in last if isinstance(p, dict))
    return f"Mock reply to: {str(last)[:60]}"


def _usage(body: dict[str, Any], reply: str) -> dict[str, int]:
    chars = sum(len(str(m.get("content", ""))) for m in body.get("messages", []))
    p, c = max(chars // 4, 1), len(reply.split())
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _error(body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    m = str(body.get("model", ""))
    for code in (503, 400):
        if m.startswith(f"fail-{code}"):
            return code, {"error": {"message": f"mock failure {code}", "type": "mock"}}
    return None


def completion(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if err := _error(body):
        return err
    reply = _reply(body)
    return 200, {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": _usage(body, reply),
    }


def stream_lines(body: dict[str, Any]) -> Iterator[str]:
    reply = _reply(body)
    words = reply.split(" ")
    for i, w in enumerate(words):
        if body.get("model") == "drop-stream" and i == 2:
            raise ConnectionError("mock stream dropped")
        chunk = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "model": body.get("model"),
            "choices": [
                {"index": 0, "delta": {"content": w + (" " if i < len(words) - 1 else "")}}
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
    end = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "model": body.get("model"),
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(end)}\n\n"
    if (body.get("stream_options") or {}).get("include_usage"):
        u = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "choices": [],
            "usage": _usage(body, reply),
        }
        yield f"data: {json.dumps(u)}\n\n"
    yield "data: [DONE]\n\n"


def embeddings(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Deterministic fake vectors: the same text always gives the same numbers."""
    if err := _error(body):
        return err
    raw = body.get("input")
    texts = [raw] if isinstance(raw, str) else list(raw or [])
    dims = int(body.get("dimensions") or 8)
    data = []
    for i, t in enumerate(texts):
        h = hashlib.sha256(str(t).encode()).digest()
        data.append(
            {"object": "embedding", "index": i, "embedding": [h[j % 32] / 255 for j in range(dims)]}
        )
    toks = max(sum(len(str(t)) for t in texts) // 4, 1)
    return 200, {
        "object": "list",
        "data": data,
        "model": body.get("model"),
        "usage": {"prompt_tokens": toks, "total_tokens": toks},
    }


def mock_transport() -> httpx.MockTransport:
    """In-process transport for tests (no server needed)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"error": {"message": "missing key"}})
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            code, payload = embeddings(body)
            return httpx.Response(code, json=payload)
        if body.get("stream"):
            if err := _error(body):
                return httpx.Response(err[0], json=err[1])
            return httpx.Response(
                200, content=b"".join(s.encode() for s in _safe(stream_lines(body)))
            )
        code, payload = completion(body)
        return httpx.Response(code, json=payload)

    return httpx.MockTransport(handle)


def _safe(it: Iterator[str]) -> Iterator[str]:
    """MockTransport buffers the whole body; a mid-stream drop becomes a truncated body."""
    try:
        yield from it
    except ConnectionError:
        return


app = FastAPI(title="Captain mock provider")


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Any:
    body = await request.json()
    if body.get("stream"):
        if err := _error(body):
            return JSONResponse(err[1], status_code=err[0])
        return StreamingResponse(_safe(stream_lines(body)), media_type="text/event-stream")
    code, payload = completion(body)
    return JSONResponse(payload, status_code=code)


@app.post("/v1/classify")
async def classify(request: Request) -> Any:
    """Mock guard model (CAPTAIN_GUARD_URLS=http://127.0.0.1:8090): flags a classic
    prompt-injection phrase, otherwise scores everything as safe."""
    text = str((await request.json()).get("text", "")).lower()
    hit = "ignore previous instructions" in text or "ignore all previous instructions" in text
    return {"injection": 0.97 if hit else 0.02, "unsafe": 0.01, "model": "mock-guard"}


@app.post("/v1/embeddings")
async def embed(request: Request) -> Any:
    code, payload = embeddings(await request.json())
    return JSONResponse(payload, status_code=code)
