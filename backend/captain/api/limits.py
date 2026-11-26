"""Request body size limit for every Captain HTTP service.

A declared Content-Length over the limit is refused before the body is read. A body sent
in chunks (no Content-Length, or a lying one) is counted as it arrives and cut off the moment
it passes the limit, so a caller cannot make a service buffer an unbounded request. Pure ASGI,
so it also works for streaming responses and does not wait for the whole body.

The refusal is HTTP 413 with a small JSON body; it never echoes any of the request.
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_MAX_BODY_BYTES = 10_000_000  # chat contexts with large code are the biggest legit case


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app: Any, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def _refuse(self, send: Any) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": "CONTENT_TOO_LARGE",
                    "message": f"request body exceeds the {self.max_bytes}-byte limit",
                    "type": "captain_governance",
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._refuse(send)
            return
        seen = 0
        started = False
        refused = False

        async def counting_receive() -> Any:
            nonlocal seen, refused
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > self.max_bytes:
                    # Answer 413 NOW, from here: frameworks (FastAPI) turn an error raised while
                    # reading a body into their own generic 400, which would hide the real reason.
                    if not started and not refused:
                        refused = True
                        await self._refuse(send)
                    raise _TooLarge
            return msg

        async def tracking_send(msg: Any) -> None:
            nonlocal started
            if refused:
                return  # our 413 is already out; ignore whatever the app tries to send now
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if not started and not refused:
                await self._refuse(send)
            # If a response is already under way there is nothing safe left to send.
