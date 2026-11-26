"""Request body size limits: a declared size, a chunked body and a lying client all stop at the
limit without the service ever buffering the whole thing."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from captain.api.limits import BodyLimitMiddleware
from tests.unit.test_gateway import gateway


def tiny_app(limit: int = 1000) -> tuple[TestClient, list[int]]:
    seen: list[int] = []
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, max_bytes=limit)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        n = len(await request.body())
        seen.append(n)
        return {"bytes": n}

    @app.post("/model")
    async def model(body: dict) -> dict[str, int]:  # type: ignore[type-arg]
        seen.append(len(body))
        return {"keys": len(body)}

    return TestClient(app), seen


def test_a_body_within_the_limit_passes_and_one_over_is_refused_before_it_is_read() -> None:
    c, seen = tiny_app(1000)
    assert c.post("/echo", content=b"x" * 1000).json() == {"bytes": 1000}
    big = c.post("/echo", content=b"x" * 1001)
    assert big.status_code == 413 and big.json()["error"]["code"] == "CONTENT_TOO_LARGE"
    assert seen == [1000]  # the handler never saw the oversized request
    assert "x" * 20 not in big.text  # the refusal never echoes the request


def test_a_chunked_body_with_no_declared_length_is_cut_off_at_the_limit() -> None:
    c, seen = tiny_app(1000)

    def chunks() -> Iterator[bytes]:
        for _ in range(50):  # 50 x 100 bytes, far over the limit, no Content-Length
            yield b"y" * 100

    r = c.post("/echo", content=chunks())
    assert r.status_code == 413 and seen == []


def test_the_service_stops_reading_once_the_limit_is_passed() -> None:
    """Raw ASGI: count how many chunks the middleware pulls from a never-ending upload."""
    import asyncio

    pulled = 0
    sent: list[dict] = []  # type: ignore[type-arg]

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        while True:  # a handler that tries to buffer everything
            msg = await receive()
            if not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():  # type: ignore[no-untyped-def]
        nonlocal pulled
        pulled += 1
        return {"type": "http.request", "body": b"z" * 100, "more_body": True}  # never ends

    async def send(msg):  # type: ignore[no-untyped-def]
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    asyncio.run(BodyLimitMiddleware(inner, max_bytes=1000)(scope, receive, send))
    assert pulled == 11  # 1,100 bytes were enough to pass the 1,000-byte limit; then it stopped
    assert sent[0]["status"] == 413


def test_small_chunked_bodies_still_work() -> None:
    c, seen = tiny_app(1000)

    def chunks() -> Iterator[bytes]:
        yield b"a" * 300
        yield b"b" * 300

    assert c.post("/echo", content=chunks()).json() == {"bytes": 600}


def test_the_gateway_refuses_an_oversized_chat_request_but_serves_a_normal_one() -> None:
    from captain.devprovider import mock_transport
    from captain.integrations.api_gateway.app import create_gateway_app
    from captain.integrations.api_gateway.provider import OpenAICompatibleClient
    from tests.unit.test_gateway import FixedSecrets

    h, tc, hdr = gateway()
    provider = OpenAICompatibleClient(FixedSecrets(), transport=mock_transport())
    small_app = create_gateway_app(h.core, provider, max_body_bytes=2000)
    c = TestClient(small_app)
    huge = {"model": "claude-sonnet", "messages": [{"role": "user", "content": "z" * 5000}]}
    r = c.post("/v1/chat/completions", headers=hdr, json=huge)
    assert r.status_code == 413 and r.json()["error"]["code"] == "CONTENT_TOO_LARGE"
    ok = tc.post(
        "/v1/chat/completions",
        headers=hdr,
        json={"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert ok.status_code == 200  # the normal gateway (default 10 MB limit) is unaffected


def test_a_chunked_overflow_on_a_typed_body_route_is_413_not_a_generic_400() -> None:
    """FastAPI turns an error raised while reading a typed body into its own 400. The limit
    must answer 413 itself, or the caller is told the wrong thing."""
    c, seen = tiny_app(1000)

    def chunks() -> Iterator[bytes]:
        yield b'{"k": "' + b"v" * 600
        yield b"w" * 600 + b'"}'

    r = c.post("/model", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "CONTENT_TOO_LARGE"
    assert seen == []
    ok = c.post("/model", json={"k": "small"})
    assert ok.status_code == 200 and ok.json() == {"keys": 1}
