"""OpenAI Responses wire format <-> the gateway's governed chat pipeline.

Codex (with `wire_api = "responses"`) and recent OpenAI SDKs call `POST /v1/responses`.
Like `messages.py`, this module only translates; the governed pipeline is shared.
Shapes follow the official OpenAI Python SDK's types (openai 3.59: Response, output
items, usage, and the response.* stream events), which is the reference used here
because the web reference pages could not be fetched.

Stateless: Captain does not store responses, so `previous_response_id` is refused (send
the full `input`); `store` is ignored. Refused with 400 instead of dropped: images and
files, reasoning items, built-in or custom tools (only `function` tools), structured
output formats. Optional hints are accepted and ignored.
"""

from __future__ import annotations

import json
import time
from typing import Any

from captain.integrations.api_gateway.messages import Translated

IGNORED_HINTS = {
    "store",
    "metadata",
    "include",
    "reasoning",
    "truncation",
    "service_tier",
    "prompt_cache_key",
    "prompt_cache_retention",
    "safety_identifier",
    "user",
    "background",
    "top_logprobs",
    "max_tool_calls",
    "stream_options",
    "conversation",
}


class ResponsesError(Exception):
    def __init__(self, message: str, code: str = "invalid_request", status: int = 400) -> None:
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def _parts_text(content: Any, where: str) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ResponsesError(f"{where}: content must be a string or a list of parts")
    out = []
    for p in content:
        kind = p.get("type") if isinstance(p, dict) else None
        if kind in ("input_text", "output_text", "text"):
            out.append(str(p.get("text", "")))
        elif kind == "refusal":
            out.append(str(p.get("refusal", "")))
        else:
            raise ResponsesError(
                f"{where}: '{kind}' content is not supported through Captain yet; "
                "only text is governed."
            )
    return "\n".join(out)


def to_openai(body: dict[str, Any]) -> tuple[Translated, list[dict[str, Any]], Any]:
    """Returns the translated request plus the tools and tool_choice to echo back."""
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ResponsesError("model is required")
    if body.get("previous_response_id"):
        raise ResponsesError(
            "previous_response_id is not supported: Captain keeps no response state; "
            "send the full input"
        )
    fmt = (body.get("text") or {}).get("format") if isinstance(body.get("text"), dict) else None
    if isinstance(fmt, dict) and fmt.get("type", "text") != "text":
        raise ResponsesError("structured output formats are not supported through Captain yet")
    out: list[dict[str, Any]] = []
    if body.get("instructions"):
        out.append({"role": "system", "content": str(body["instructions"])})
    items = body.get("input")
    if isinstance(items, str):
        out.append({"role": "user", "content": items})
    elif isinstance(items, list) and items:
        for it in items:
            if not isinstance(it, dict):
                raise ResponsesError("each input item must be an object")
            kind = it.get("type", "message" if "role" in it else None)
            if kind == "message":
                role = {"developer": "system"}.get(str(it.get("role")), str(it.get("role")))
                if role not in ("user", "assistant", "system"):
                    raise ResponsesError(f"unsupported message role: {it.get('role')!r}")
                out.append({"role": role, "content": _parts_text(it.get("content", ""), role)})
            elif kind == "function_call":
                call = {
                    "id": str(it.get("call_id", "")),
                    "type": "function",
                    "function": {
                        "name": str(it.get("name", "")),
                        "arguments": str(it.get("arguments") or "{}"),
                    },
                }
                if out and out[-1]["role"] == "assistant":
                    out[-1].setdefault("tool_calls", []).append(call)
                else:
                    out.append({"role": "assistant", "content": None, "tool_calls": [call]})
            elif kind == "function_call_output":
                text = _parts_text(it.get("output", ""), "function_call_output")
                out.append(
                    {"role": "tool", "tool_call_id": str(it.get("call_id", "")), "content": text}
                )
            else:
                raise ResponsesError(f"input item type {kind!r} is not supported through Captain")
    else:
        raise ResponsesError("input must be a string or a non-empty list of items")

    extra: dict[str, Any] = {}
    echo_tools: list[dict[str, Any]] = []
    tools = body.get("tools")
    if tools:
        fns = []
        for t in tools:
            if not isinstance(t, dict) or t.get("type") != "function":
                raise ResponsesError("only function tools are supported through Captain")
            params = t.get("parameters") or {"type": "object", "properties": {}}
            echo_tools.append(
                {
                    "type": "function",
                    "name": t.get("name"),
                    "description": t.get("description"),
                    "parameters": params,
                    "strict": bool(t.get("strict", False)),
                }
            )
            fns.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name"),
                        "description": t.get("description") or "",
                        "parameters": params,
                    },
                }
            )
        extra["tools"] = fns
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, dict):
        if choice.get("type") != "function":
            raise ResponsesError("only function tool_choice is supported through Captain")
        extra["tool_choice"] = {"type": "function", "function": {"name": choice.get("name")}}
    elif choice in ("auto", "none", "required"):
        extra["tool_choice"] = choice
    else:
        raise ResponsesError("unsupported tool_choice")
    if body.get("parallel_tool_calls") is False:
        extra["parallel_tool_calls"] = False
    for k in ("temperature", "top_p"):
        if isinstance(body.get(k), int | float):
            extra[k] = body[k]
    mt = body.get("max_output_tokens")
    return (
        Translated(
            model=model,
            messages=out,
            passthrough=extra,
            max_tokens=mt if isinstance(mt, int) and mt > 0 else None,
            stream=bool(body.get("stream")),
        ),
        echo_tools,
        choice,
    )


def usage_dict(u: dict[str, int]) -> dict[str, Any]:
    return {
        "input_tokens": u["input_tokens"],
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": u["output_tokens"],
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": u["input_tokens"] + u["output_tokens"],
    }


def _status(finish: str | None) -> tuple[str, dict[str, str] | None]:
    if finish == "length":
        return "incomplete", {"reason": "max_output_tokens"}
    if finish == "content_filter":
        return "incomplete", {"reason": "content_filter"}
    return "completed", None


def response_object(
    rid: str,
    alias: str,
    created: float,
    output: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: Any,
    status: str = "completed",
    usage: dict[str, int] | None = None,
    incomplete: dict[str, str] | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "id": rid,
        "object": "response",
        "created_at": created,
        "status": status,
        "model": alias,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": tool_choice,
        "tools": tools,
        "error": error,
        "incomplete_details": incomplete,
        "usage": usage_dict(usage) if usage else None,
    }


def message_item(item_id: str, text: str, status: str = "completed") -> dict[str, Any]:
    content = [{"type": "output_text", "text": text, "annotations": []}] if text else []
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": status,
        "content": content,
    }


def call_item(
    item_id: str, call_id: str, name: str, args: str, status: str = "completed"
) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": args,
        "status": status,
    }


def to_response(
    data: dict[str, Any],
    alias: str,
    rid: str,
    text: str,
    in_chars: int,
    tools: list[dict[str, Any]],
    tool_choice: Any,
) -> dict[str, Any]:
    from captain.integrations.api_gateway.messages import usage_of

    choice = (data.get("choices") or [{}])[0]
    out: list[dict[str, Any]] = []
    out_chars = len(text)
    if text:
        out.append(message_item(f"msg_{rid[5:]}0", text))
    for i, call in enumerate((choice.get("message") or {}).get("tool_calls") or []):
        fn = call.get("function") or {}
        args = fn.get("arguments") or "{}"
        out_chars += len(args)
        out.append(call_item(f"fc_{rid[5:]}{i + 1}", call.get("id", ""), fn.get("name", ""), args))
    status, incomplete = _status(choice.get("finish_reason"))
    return response_object(
        rid,
        alias,
        time.time(),
        out,
        tools,
        tool_choice,
        status,
        usage_of(data.get("usage"), in_chars, out_chars),
        incomplete,
    )


def sse(obj: dict[str, Any]) -> str:
    return f"event: {obj['type']}\ndata: {json.dumps(obj)}\n\n"


class StreamTranslator:
    """OpenAI chunks -> response.* events. One output item is open at a time; every event
    carries a rising sequence_number."""

    def __init__(
        self,
        alias: str,
        rid: str,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        in_tokens: int,
    ) -> None:
        self._alias, self._rid, self._tools, self._choice = alias, rid, tools, tool_choice
        self._created = time.time()
        self._in = in_tokens
        self._seq = -1
        self._items: list[dict[str, Any]] = []
        self._open: dict[str, Any] | None = None
        self.finish: str | None = None

    def _ev(self, type_: str, **fields: Any) -> str:
        self._seq += 1
        return sse({"type": type_, "sequence_number": self._seq, **fields})

    def _resp(self, status: str, **kw: Any) -> dict[str, Any]:
        return response_object(
            self._rid,
            self._alias,
            self._created,
            kw.pop("output", []),
            self._tools,
            self._choice,
            status,
            **kw,
        )

    def start(self) -> str:
        r = self._resp("in_progress")
        return self._ev("response.created", response=r) + self._ev(
            "response.in_progress", response=r
        )

    def _close(self) -> list[str]:
        o = self._open
        if o is None:
            return []
        self._open = None
        ev: list[str] = []
        if o["kind"] == "text":
            item = message_item(o["id"], o["text"])
            part = (
                item["content"][0]
                if o["text"]
                else {"type": "output_text", "text": "", "annotations": []}
            )
            ev.append(
                self._ev(
                    "response.output_text.done",
                    content_index=0,
                    item_id=o["id"],
                    logprobs=[],
                    output_index=o["index"],
                    text=o["text"],
                )
            )
            ev.append(
                self._ev(
                    "response.content_part.done",
                    content_index=0,
                    item_id=o["id"],
                    output_index=o["index"],
                    part=part,
                )
            )
        else:
            item = call_item(o["id"], o["call_id"], o["name"], o["args"])
            ev.append(
                self._ev(
                    "response.function_call_arguments.done",
                    arguments=o["args"],
                    item_id=o["id"],
                    output_index=o["index"],
                )
            )
        ev.append(self._ev("response.output_item.done", item=item, output_index=o["index"]))
        self._items.append(item)
        return ev

    def text(self, piece: str) -> list[str]:
        ev: list[str] = []
        o = self._open
        if o is None or o["kind"] != "text":
            ev += self._close()
            idx = len(self._items)
            o = self._open = {
                "kind": "text",
                "index": idx,
                "id": f"msg_{self._rid[5:]}{idx}",
                "text": "",
            }
            ev.append(
                self._ev(
                    "response.output_item.added",
                    item=message_item(o["id"], "", "in_progress"),
                    output_index=idx,
                )
            )
            ev.append(
                self._ev(
                    "response.content_part.added",
                    content_index=0,
                    item_id=o["id"],
                    output_index=idx,
                    part={"type": "output_text", "text": "", "annotations": []},
                )
            )
        o["text"] += piece
        ev.append(
            self._ev(
                "response.output_text.delta",
                content_index=0,
                delta=piece,
                item_id=o["id"],
                logprobs=[],
                output_index=o["index"],
            )
        )
        return ev

    def tool_call(self, tc: dict[str, Any]) -> list[str]:
        ev: list[str] = []
        fn = tc.get("function") or {}
        slot = tc.get("index", 0)
        o = self._open
        if o is None or o["kind"] != "tool" or (tc.get("id") and slot != o["slot"]):
            ev += self._close()
            idx = len(self._items)
            o = self._open = {
                "kind": "tool",
                "index": idx,
                "id": f"fc_{self._rid[5:]}{idx}",
                "slot": slot,
                "call_id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "args": "",
            }
            ev.append(
                self._ev(
                    "response.output_item.added",
                    item=call_item(o["id"], o["call_id"], o["name"], "", "in_progress"),
                    output_index=idx,
                )
            )
        if fn.get("arguments"):
            o["args"] += fn["arguments"]
            ev.append(
                self._ev(
                    "response.function_call_arguments.delta",
                    delta=fn["arguments"],
                    item_id=o["id"],
                    output_index=o["index"],
                )
            )
        return ev

    def end(self, usage: dict[str, int]) -> list[str]:
        ev = self._close()
        status, incomplete = _status(self.finish)
        final = self._resp(status, output=self._items, usage=usage, incomplete=incomplete)
        ev.append(
            self._ev(
                "response.completed" if status == "completed" else "response.incomplete",
                response=final,
            )
        )
        return ev

    def error(self, message: str) -> str:
        failed = self._resp(
            "failed", output=self._items, error={"code": "server_error", "message": message}
        )
        return self._ev("response.failed", response=failed)


def error_type(status: int) -> str:
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        429: "rate_limit_error",
    }.get(status, "server_error")
