"""Google Gemini native wire format (`generateContent` / `streamGenerateContent`) <-> the
gateway's internal OpenAI chat shape, so Gemini SDK users can point at Captain and get the
same governance as every other route.

Wire format source: Google's public API reference for `models.generateContent` (request
fields contents / systemInstruction / generationConfig / tools / toolConfig, response fields
candidates / finishReason / usageMetadata). Both camelCase and snake_case request keys are
accepted because Google's own REST examples use both.

Honest limits:
- Streaming uses `?alt=sse`. The reference does not spell out each event's content; each
  event here is a full GenerateContentResponse-shaped chunk, which is what the examples read.
  NOT verified against the real Google SDK.
- Only text, functionCall and functionResponse parts are supported. Images, audio, files,
  code execution, grounding, cached content and similar are refused with a clear 400, never
  silently dropped: Captain can only govern what it can read.
- Only one candidate; the API key is taken from the `x-goog-api-key` header or
  `Authorization: Bearer`, never from the `?key=` query string (URLs end up in logs).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class GeminiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ABORTED",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    501: "UNIMPLEMENTED",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}
_FINISH = {"stop": "STOP", "tool_calls": "STOP", "length": "MAX_TOKENS", "content_filter": "SAFETY"}
IGNORED = {"safetySettings", "labels", "serviceTier", "store", "continuationToken"}


def error_body(status: int, message: str) -> dict[str, Any]:
    return {"error": {"code": status, "message": message, "status": _STATUS.get(status, "UNKNOWN")}}


def _snake(name: str) -> str:
    return "".join("_" + c.lower() if c.isupper() else c for c in name)


def _get(d: dict[str, Any], name: str) -> Any:
    """Read a field by its camelCase name, or its snake_case spelling."""
    return d[name] if name in d else d.get(_snake(name))


@dataclass
class Translated:
    model: str
    messages: list[dict[str, Any]]
    passthrough: dict[str, Any]
    max_tokens: int | None
    stream: bool
    names: set[str] = field(default_factory=set)


def _parts(content: Any, where: str) -> list[dict[str, Any]]:
    parts = _get(content, "parts") if isinstance(content, dict) else None
    if not isinstance(parts, list) or not all(isinstance(p, dict) for p in parts):
        raise GeminiError(f"{where} needs a parts list")
    return parts


def _text_parts(content: Any, where: str) -> str:
    texts = []
    for p in _parts(content, where):
        if "text" not in p:
            raise GeminiError(f"{where} may only contain text through Captain")
        texts.append(str(p["text"]))
    return "\n".join(texts)


def to_openai(body: dict[str, Any], model: str) -> Translated:
    contents = body.get("contents")
    if not isinstance(contents, list) or not contents:
        raise GeminiError("contents must be a non-empty list")
    if _get(body, "cachedContent"):
        raise GeminiError("cachedContent is not supported through Captain (content cannot be read)")
    out: list[dict[str, Any]] = []
    system = _get(body, "systemInstruction")
    if system:
        out.append({"role": "system", "content": _text_parts(system, "systemInstruction")})
    pending: list[tuple[str, str]] = []  # (call id, name) of calls not yet answered
    counter = 0
    for c in contents:
        if not isinstance(c, dict):
            raise GeminiError("each contents entry must be an object")
        role = c.get("role") or "user"
        if role not in ("user", "model"):
            raise GeminiError(f"unsupported role: {role!r}")
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for p in _parts(c, "contents entry"):
            if p.get("thought"):
                continue  # model thinking summaries are not part of the conversation
            if "text" in p:
                texts.append(str(p["text"]))
            elif _get(p, "functionCall") is not None:
                calls.append(_get(p, "functionCall"))
            elif _get(p, "functionResponse") is not None:
                results.append(_get(p, "functionResponse"))
            else:
                kinds = ", ".join(sorted(k for k in p if k != "thoughtSignature"))
                raise GeminiError(
                    f"unsupported content part ({kinds}); Captain accepts text and function calls"
                )
        if role == "model":
            msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
            if calls:
                tcs = []
                for fc in calls:
                    counter += 1
                    cid = str(fc.get("id") or f"call_{counter}")
                    name = str(fc.get("name", ""))
                    pending.append((cid, name))
                    tcs.append({"id": cid, "type": "function", "function": {
                        "name": name, "arguments": json.dumps(fc.get("args") or {}),
                    }})  # fmt: skip
                msg["tool_calls"] = tcs
            out.append(msg)
        else:
            for r in results:
                name = str(r.get("name", ""))
                cid = str(r.get("id") or "")
                if not cid:
                    match = next((x for x in pending if x[1] == name), None)
                    cid = match[0] if match else f"call_{name}"
                pending = [x for x in pending if x[0] != cid]
                out.append({"role": "tool", "tool_call_id": cid,
                            "content": json.dumps(r.get("response") or {})})  # fmt: skip
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})

    extra: dict[str, Any] = {}
    names: set[str] = set()
    tools = body.get("tools")
    if tools:
        fns = []
        for t in tools:
            decls = _get(t, "functionDeclarations") if isinstance(t, dict) else None
            if (
                not isinstance(t, dict)
                or decls is None
                or set(t) - {"functionDeclarations", "function_declarations"}
            ):
                raise GeminiError("only functionDeclarations tools are supported through Captain")
            for fd in decls:
                names.add(str(fd.get("name")))
                params = (
                    _get(fd, "parametersJsonSchema") or fd.get("parameters") or {"type": "object"}
                )
                fns.append({"type": "function", "function": {
                    "name": fd.get("name"), "description": fd.get("description", ""),
                    "parameters": params,
                }})  # fmt: skip
        extra["tools"] = fns
    fcc = (_get(_get(body, "toolConfig") or {}, "functionCallingConfig")) or {}
    mode = fcc.get("mode")
    allowed = _get(fcc, "allowedFunctionNames") or []
    if mode == "NONE":
        extra["tool_choice"] = "none"
    elif mode == "ANY":
        extra["tool_choice"] = (
            {"type": "function", "function": {"name": allowed[0]}}
            if len(allowed) == 1
            else "required"
        )
    elif mode == "AUTO":
        extra["tool_choice"] = "auto"
    gc = _get(body, "generationConfig") or {}
    if not isinstance(gc, dict):
        raise GeminiError("generationConfig must be an object")
    if int(_get(gc, "candidateCount") or 1) != 1:
        raise GeminiError("candidateCount greater than 1 is not supported (cost cannot be bounded)")
    knobs = (
        ("temperature", "temperature"),
        ("topP", "top_p"),
        ("seed", "seed"),
        ("presencePenalty", "presence_penalty"),
        ("frequencyPenalty", "frequency_penalty"),
    )
    for src, dst in knobs:
        v = _get(gc, src)
        if isinstance(v, int | float) and not isinstance(v, bool):
            extra[dst] = v
    if _get(gc, "stopSequences"):
        extra["stop"] = _get(gc, "stopSequences")
    if _get(gc, "responseMimeType") == "application/json":
        extra["response_format"] = {"type": "json_object"}
    mt = _get(gc, "maxOutputTokens")
    return Translated(
        model=model,
        messages=out,
        passthrough=extra,
        max_tokens=mt if isinstance(mt, int) and mt > 0 else None,
        stream=False,
        names=names,
    )


def usage_metadata(raw: dict[str, Any] | None, in_chars: int, out_chars: int) -> dict[str, int]:
    if raw and "prompt_tokens" in raw:
        p, c = int(raw.get("prompt_tokens") or 0), int(raw.get("completion_tokens") or 0)
    else:
        p, c = in_chars // 4, max(out_chars // 4, 1)
    return {"promptTokenCount": p, "candidatesTokenCount": c, "totalTokenCount": p + c}


def _call_part(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError as e:
        raise GeminiError("the model returned invalid tool arguments", 502) from e
    if not isinstance(args, dict):
        raise GeminiError("the model returned invalid tool arguments", 502)
    return {"functionCall": {"name": fn.get("name", ""), "args": args, "id": call.get("id", "")}}


def to_response(
    data: dict[str, Any], alias: str, rid: str, text: str, in_chars: int
) -> dict[str, Any]:
    choice = (data.get("choices") or [{}])[0]
    parts: list[dict[str, Any]] = [{"text": text}] if text else []
    out_chars = len(text)
    for call in (choice.get("message") or {}).get("tool_calls") or []:
        part = _call_part(call)
        out_chars += len(json.dumps(part["functionCall"]["args"]))
        parts.append(part)
    return {
        "candidates": [{
            "content": {"role": "model", "parts": parts or [{"text": ""}]},
            "finishReason": _FINISH.get(choice.get("finish_reason") or "stop", "OTHER"),
            "index": 0,
        }],
        "usageMetadata": usage_metadata(data.get("usage"), in_chars, out_chars),
        "modelVersion": alias,
        "responseId": rid,
    }  # fmt: skip


def sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj)}\n\n"


class GeminiStream:
    """OpenAI chunks -> Gemini SSE events (the interface `_stream_translated` drives).

    Text goes out as it arrives. Gemini returns a function call as one complete part, so tool
    call fragments are buffered per call and sent together in the final event."""

    def __init__(self, alias: str, rid: str) -> None:
        self._alias, self._rid = alias, rid
        self._calls: dict[int, dict[str, Any]] = {}
        self.finish: str | None = None

    def _event(self, parts: list[dict[str, Any]], **extra: Any) -> str:
        cand: dict[str, Any] = {"content": {"role": "model", "parts": parts}, "index": 0}
        cand.update(extra.pop("candidate", {}))
        return sse({"candidates": [cand], "modelVersion": self._alias,
                    "responseId": self._rid, **extra})  # fmt: skip

    def start(self) -> str:
        return ""  # Gemini streams have no opening event

    def text(self, piece: str) -> list[str]:
        return [self._event([{"text": piece}])]

    def tool_call(self, tc: dict[str, Any]) -> list[str]:
        slot = self._calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        slot["name"] += fn.get("name") or ""
        slot["args"] += fn.get("arguments") or ""
        return []

    def end(self, usage: dict[str, int]) -> list[str]:
        parts: list[dict[str, Any]] = []
        for _, c in sorted(self._calls.items()):
            parts.append(
                _call_part({"id": c["id"], "function": {"name": c["name"], "arguments": c["args"]}})
            )
        reason = _FINISH.get(self.finish or "stop", "OTHER")
        total = usage["input_tokens"] + usage["output_tokens"]
        meta = {
            "promptTokenCount": usage["input_tokens"],
            "candidatesTokenCount": usage["output_tokens"],
            "totalTokenCount": total,
        }
        return [self._event(parts, candidate={"finishReason": reason}, usageMetadata=meta)]

    def error(self, message: str) -> str:
        return sse(error_body(502, message))


# ------------------------------------------------------------------ embeddings and counting


def embed_request(body: dict[str, Any], single: bool) -> tuple[list[str], int | None]:
    """(texts, output dimensionality) from `embedContent` ({content}) or `batchEmbedContents`
    ({requests: [{content}, ...]}). Text only; every item must ask for the same dimensionality."""
    items = [body] if single else body.get("requests")
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
        raise GeminiError("batchEmbedContents needs a non-empty requests list")
    texts: list[str] = []
    dims: set[int | None] = set()
    for item in items:
        if item.get("content") is None:
            raise GeminiError("each embedding request needs content")
        texts.append(_text_parts(item["content"], "content"))
        d = _get(item, "outputDimensionality")
        dims.add(int(d) if isinstance(d, int) and not isinstance(d, bool) and d > 0 else None)
    if len(dims) > 1:
        raise GeminiError("all requests in a batch must use the same outputDimensionality")
    return texts, next(iter(dims))


def embed_response(data: dict[str, Any], single: bool) -> dict[str, Any]:
    vectors = [{"values": d.get("embedding", [])} for d in data.get("data", [])]
    if single:
        return {"embedding": vectors[0] if vectors else {"values": []}}
    return {"embeddings": vectors}


def count_tokens(body: dict[str, Any], model: str) -> dict[str, int]:
    """Approximate (4 characters per token); no model is called and nothing is charged."""
    t = to_openai(
        {"contents": body.get("contents"), "systemInstruction": _get(body, "systemInstruction")},
        model,
    )
    chars = sum(len(str(m.get("content") or "")) for m in t.messages)
    chars += sum(
        len(str((tc.get("function") or {}).get("arguments") or ""))
        for m in t.messages
        for tc in m.get("tool_calls") or []
    )
    return {"totalTokens": max(chars // 4, 1)}
