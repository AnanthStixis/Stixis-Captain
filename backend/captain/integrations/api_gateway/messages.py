"""Anthropic Messages wire format <-> the gateway's OpenAI-compatible pipeline.

Claude Code (and any Anthropic SDK client) speaks `POST /v1/messages`. The gateway keeps
ONE governed pipeline; this module only translates the request into an OpenAI-style
chat payload and the provider's reply back into Messages responses and SSE events.

Shapes follow Anthropic's published Messages and streaming references (checked
2026-11). Anything we cannot represent faithfully is refused with
`invalid_request_error` instead of being dropped silently:
- image / document / search-result blocks, thinking and redacted_thinking blocks,
- server tools (tools that carry a `type` other than "custom").
Optional hints that do not change the meaning of the call are accepted and ignored:
thinking, context_management, output_config, metadata, service_tier, speed,
cache_control, top_k, safeguards. The model never returns thinking blocks here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class MessagesError(Exception):
    def __init__(self, message: str, etype: str = "invalid_request_error", status: int = 400):
        super().__init__(message)
        self.message, self.etype, self.status = message, etype, status


IGNORED_HINTS = {
    "thinking",
    "context_management",
    "output_config",
    "metadata",
    "service_tier",
    "speed",
    "top_k",
    "safeguards",
    "container",
    "inference_geo",
    "diagnostics",
    "cache_control",
}
_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}


@dataclass
class Translated:
    model: str
    messages: list[dict[str, Any]]
    passthrough: dict[str, Any]
    max_tokens: int | None
    stream: bool
    tool_names: set[str] = field(default_factory=set)


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list) and all(isinstance(b, dict) for b in content):
        return content
    raise MessagesError("message content must be a string or a list of blocks")


def _text_of(blocks: list[dict[str, Any]], where: str) -> str:
    out = []
    for b in blocks:
        if b.get("type") != "text":
            raise MessagesError(
                f"{where}: '{b.get('type')}' blocks are not supported through Captain yet; "
                "only text is governed."
            )
        out.append(str(b.get("text", "")))
    return "\n".join(out)


def to_openai(body: dict[str, Any]) -> Translated:
    model = body.get("model")
    msgs = body.get("messages")
    if not isinstance(model, str) or not model:
        raise MessagesError("model is required")
    if not isinstance(msgs, list) or not msgs:
        raise MessagesError("messages must be a non-empty list")
    out: list[dict[str, Any]] = []
    system = body.get("system")
    if system:
        out.append({"role": "system", "content": _text_of(_blocks(system), "system")})
    for m in msgs:
        if not isinstance(m, dict):
            raise MessagesError("each message must be an object")
        role, blocks = m.get("role"), _blocks(m.get("content"))
        if role == "system":  # Claude Code sends system-role entries inside messages
            out.append({"role": "system", "content": _text_of(blocks, "system message")})
        elif role == "user":
            results = [b for b in blocks if b.get("type") == "tool_result"]
            rest = [b for b in blocks if b.get("type") != "tool_result"]
            for r in results:
                text = _text_of(_blocks(r.get("content", "")), "tool_result")
                if r.get("is_error"):
                    text = "Error: " + text
                out.append(
                    {"role": "tool", "tool_call_id": str(r.get("tool_use_id", "")), "content": text}
                )
            if rest:
                out.append({"role": "user", "content": _text_of(rest, "user message")})
        elif role == "assistant":
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            text = _text_of([b for b in blocks if b.get("type") != "tool_use"], "assistant message")
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if uses:
                msg["tool_calls"] = [
                    {
                        "id": str(u.get("id", "")),
                        "type": "function",
                        "function": {
                            "name": str(u.get("name", "")),
                            "arguments": json.dumps(u.get("input") or {}),
                        },
                    }
                    for u in uses
                ]
            out.append(msg)
        else:
            raise MessagesError(f"unsupported message role: {role!r}")

    extra: dict[str, Any] = {}
    names: set[str] = set()
    tools = body.get("tools")
    if tools:
        fns = []
        for t in tools:
            if not isinstance(t, dict) or t.get("type", "custom") != "custom":
                raise MessagesError("only custom tools are supported through Captain")
            names.add(str(t.get("name")))
            fns.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema") or {"type": "object"},
                    },
                }
            )
        extra["tools"] = fns
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        kind = tc.get("type")
        if kind == "tool":
            extra["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}
        elif kind in ("auto", "none"):
            extra["tool_choice"] = kind
        elif kind == "any":
            extra["tool_choice"] = "required"
        if tc.get("disable_parallel_tool_use"):
            extra["parallel_tool_calls"] = False
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if isinstance(body.get(src), int | float):
            extra[dst] = body[src]
    if body.get("stop_sequences"):
        extra["stop"] = body["stop_sequences"]
    mt = body.get("max_tokens")
    return Translated(
        model=model,
        messages=out,
        passthrough=extra,
        max_tokens=mt if isinstance(mt, int) and mt > 0 else None,
        stream=bool(body.get("stream")),
        tool_names=names,
    )


def stop_reason(finish: str | None) -> str:
    return _STOP.get(finish or "", "end_turn")


def usage_of(raw: dict[str, Any] | None, in_chars: int, out_chars: int) -> dict[str, int]:
    """Provider-reported tokens when present, else an estimate (4 characters per token)."""
    if raw and "prompt_tokens" in raw:
        return {
            "input_tokens": int(raw.get("prompt_tokens") or 0),
            "output_tokens": int(raw.get("completion_tokens") or 0),
        }
    return {"input_tokens": in_chars // 4, "output_tokens": max(out_chars // 4, 1)}


def to_message(
    data: dict[str, Any], alias: str, msg_id: str, text: str, in_chars: int
) -> dict[str, Any]:
    choice = (data.get("choices") or [{}])[0]
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    out_chars = len(text)
    for call in (choice.get("message") or {}).get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(args)
        except ValueError as e:
            raise MessagesError(
                "the model returned invalid tool arguments", "api_error", 502
            ) from e
        if not isinstance(parsed, dict):
            raise MessagesError("the model returned invalid tool arguments", "api_error", 502)
        out_chars += len(args)
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": fn.get("name", ""),
                "input": parsed,
            }
        )
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": alias,
        "content": content,
        "stop_reason": stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": usage_of(data.get("usage"), in_chars, out_chars),
    }


def sse(event: str, obj: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(obj)}\n\n"


class StreamTranslator:
    """OpenAI chunks -> Messages events: message_start, then per content block
    content_block_start / content_block_delta* / content_block_stop, then message_delta
    and message_stop. One block is open at a time; a text block is closed before a tool
    block starts and vice versa."""

    def __init__(self, alias: str, msg_id: str, in_tokens: int) -> None:
        self._alias, self._id, self._in = alias, msg_id, in_tokens
        self._index = -1
        self._open: str | None = None  # "text" | "tool"
        self._tool_slot: int | None = None  # provider's tool_calls[].index of the open block
        self.finish: str | None = None

    def start(self) -> str:
        return sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self._id,
                    "type": "message",
                    "role": "assistant",
                    "model": self._alias,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": self._in, "output_tokens": 1},
                },
            },
        )

    def _close(self) -> list[str]:
        if self._open is None:
            return []
        ev = [sse("content_block_stop", {"type": "content_block_stop", "index": self._index})]
        self._open, self._tool_slot = None, None
        return ev

    def text(self, piece: str) -> list[str]:
        ev: list[str] = []
        if self._open != "text":
            ev += self._close()
            self._index += 1
            self._open = "text"
            ev.append(
                sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self._index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        ev.append(
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._index,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )
        )
        return ev

    def tool_call(self, tc: dict[str, Any]) -> list[str]:
        ev: list[str] = []
        slot = tc.get("index", 0)
        fn = tc.get("function") or {}
        if self._open != "tool" or (tc.get("id") and slot != self._tool_slot):
            ev += self._close()
            self._index += 1
            self._open, self._tool_slot = "tool", slot
            ev.append(
                sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self._index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": {},
                        },
                    },
                )
            )
        if fn.get("arguments"):
            ev.append(
                sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._index,
                        "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                    },
                )
            )
        return ev

    def error(self, message: str) -> str:
        return sse("error", error_body("api_error", message))

    def end(self, usage: dict[str, int]) -> list[str]:
        out_tokens = usage["output_tokens"]
        ev = self._close()
        ev.append(
            sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason(self.finish), "stop_sequence": None},
                    "usage": {"output_tokens": out_tokens},
                },
            )
        )
        ev.append(sse("message_stop", {"type": "message_stop"}))
        return ev


def error_body(etype: str, message: str, request_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "error", "error": {"type": etype, "message": message}}
    if request_id:
        body["request_id"] = request_id
    return body


def error_type(status: int) -> str:
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        409: "conflict_error",
        413: "request_too_large",
        429: "rate_limit_error",
        504: "timeout_error",
        529: "overloaded_error",
    }.get(status, "api_error")
