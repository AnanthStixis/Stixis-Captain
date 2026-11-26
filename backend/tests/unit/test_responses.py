"""OpenAI Responses endpoint (/v1/responses): translation, events, refusals, output guard."""

from __future__ import annotations

import json
from typing import Any

from captain.core.identity.killswitch import KillScope
from captain.core.models import RiskTier
from tests.unit.test_messages import USAGE, Rig, text_chunks


def post(r: Rig, **body: Any) -> Any:
    body.setdefault("model", "claude-sonnet")
    body.setdefault("input", "hi")
    return r.c.post("/v1/responses", headers={"Authorization": f"Bearer {r.key}"}, json=body)


def events(resp: Any) -> list[dict[str, Any]]:
    out = []
    for block in resp.text.strip().split("\n\n"):
        lines = block.split("\n")
        data = json.loads(lines[1].removeprefix("data: "))
        assert lines[0] == f"event: {data['type']}"  # event name matches the payload type
        out.append(data)
    return out


WEATHER = {
    "type": "function",
    "name": "weather",
    "description": "d",
    "parameters": {"type": "object"},
}


# ------------------------------------------------------------------ non-stream


def test_text_reply_has_the_response_shape() -> None:
    r = Rig()
    resp = post(r, tools=[WEATHER], max_output_tokens=50)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response" and body["status"] == "completed"
    assert body["id"].startswith("resp_") and body["model"] == "claude-sonnet"
    msg = body["output"][0]
    assert msg["type"] == "message" and msg["role"] == "assistant" and msg["status"] == "completed"
    assert msg["content"] == [{"type": "output_text", "text": "Hello there", "annotations": []}]
    assert body["usage"]["input_tokens"] == USAGE["prompt_tokens"]
    assert body["usage"]["total_tokens"] == 17
    assert body["tools"][0]["name"] == "weather" and body["tool_choice"] == "auto"
    assert body["incomplete_details"] is None and body["error"] is None
    assert resp.headers["X-Captain-Decision"] == "ALLOW"
    assert r.h.budget("org:org-1")["spent"] > 0 and r.h.budget("org:org-1")["reserved"] == 0


def test_request_translation() -> None:
    r = Rig()
    post(
        r,
        instructions="Be brief.",
        input=[
            {"role": "developer", "content": "repo rules"},
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "weather?"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Checking."}],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "weather",
                "arguments": '{"c": "Pune"}',
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "31C"},
        ],
        tools=[WEATHER],
        tool_choice={"type": "function", "name": "weather"},
        max_output_tokens=77,
        temperature=0.3,
        parallel_tool_calls=False,
        store=False,
        reasoning={"effort": "low"},
    )
    sent = r.fake.seen[0]
    assert [m["role"] for m in sent["messages"]] == [
        "system",
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert (
        sent["messages"][0]["content"] == "Be brief."
        and sent["messages"][1]["content"] == "repo rules"
    )
    assistant = sent["messages"][3]  # the call joined the assistant message before it
    assert assistant["content"] == "Checking."
    assert assistant["tool_calls"][0]["function"] == {
        "name": "weather",
        "arguments": '{"c": "Pune"}',
    }
    assert sent["messages"][4] == {"role": "tool", "tool_call_id": "call_1", "content": "31C"}
    assert sent["tools"][0]["function"]["name"] == "weather"
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "weather"}}
    assert sent["max_tokens"] == 77 and sent["temperature"] == 0.3
    assert sent["parallel_tool_calls"] is False
    assert "reasoning" not in sent and "store" not in sent


def test_function_call_output_items() -> None:
    r = Rig()
    r.fake.message = {
        "role": "assistant",
        "content": "Let me check.",
        "tool_calls": [
            {
                "id": "call_9",
                "type": "function",
                "function": {"name": "weather", "arguments": '{"c": "Pune"}'},
            }
        ],
    }
    r.fake.finish = "tool_calls"
    out = post(r, tools=[WEATHER]).json()["output"]
    assert [o["type"] for o in out] == ["message", "function_call"]
    call = out[1]
    assert (call["call_id"], call["name"], call["arguments"]) == (
        "call_9",
        "weather",
        '{"c": "Pune"}',
    )
    assert call["status"] == "completed" and call["id"].startswith("fc_")


def test_length_finish_is_incomplete() -> None:
    r = Rig()
    r.fake.finish = "length"
    body = post(r).json()
    assert body["status"] == "incomplete"
    assert body["incomplete_details"] == {"reason": "max_output_tokens"}


def test_unsupported_input_is_refused_not_dropped() -> None:
    r = Rig()
    cases: list[dict[str, Any]] = [
        {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]},
        {"input": [{"type": "reasoning", "summary": []}]},
        {"input": "hi", "tools": [{"type": "web_search"}]},
        {"input": "hi", "previous_response_id": "resp_old"},
        {"input": "hi", "text": {"format": {"type": "json_schema", "name": "n", "schema": {}}}},
        {"input": []},
    ]
    for case in cases:
        resp = post(r, **case)
        assert resp.status_code == 400, case
        assert resp.json()["error"]["type"] == "invalid_request_error"
    assert r.fake.seen == []


def test_errors_use_the_openai_envelope() -> None:
    r = Rig()
    none = r.c.post("/v1/responses", json={"model": "claude-sonnet", "input": "x"})
    assert none.status_code == 401 and none.json()["error"]["type"] == "authentication_error"
    unknown = post(r, model="not-in-registry")
    assert unknown.status_code == 403 and unknown.json()["error"]["code"] == "MODEL_NOT_REGISTERED"
    r.h.core.kill(KillScope.CHANNEL, "IDE", "stop", "admin")
    assert post(r).status_code == 403 and r.fake.seen == []


def test_secret_in_reply_is_blocked_at_tier_2() -> None:
    r = Rig(RiskTier.TIER_2)
    r.fake.message = {"role": "assistant", "content": "key AKIAIOSFODNN7EXAMPLE"}
    resp = post(r)
    assert resp.status_code == 403 and "AKIA" not in resp.text


# ------------------------------------------------------------------- streaming


def test_text_stream_events() -> None:
    r = Rig()
    r.fake.chunks = text_chunks("Hel", "lo ", "there")
    resp = post(r, stream=True)
    assert resp.headers["content-type"].startswith("text/event-stream")
    ev = events(resp)
    assert [e["sequence_number"] for e in ev] == list(range(len(ev)))
    assert [e["type"] for e in ev if "delta" not in e["type"]] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert ev[0]["response"]["status"] == "in_progress" and ev[0]["response"]["output"] == []
    assert (
        "".join(e["delta"] for e in ev if e["type"] == "response.output_text.delta")
        == "Hello there"
    )
    done = next(e for e in ev if e["type"] == "response.output_text.done")
    assert done["text"] == "Hello there" and done["logprobs"] == []
    final = ev[-1]["response"]
    assert final["status"] == "completed" and final["usage"]["total_tokens"] == 17
    assert final["output"][0]["content"][0]["text"] == "Hello there"
    item_ids = {e.get("item_id") for e in ev if "item_id" in e}
    assert item_ids == {final["output"][0]["id"]}
    assert r.h.budget("org:org-1")["spent"] > 0 and r.h.budget("org:org-1")["reserved"] == 0


def test_function_call_stream() -> None:
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
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"c":'}}]}}
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
    ev = events(post(r, stream=True, tools=[WEATHER]))
    added = [
        (e["output_index"], e["item"]["type"])
        for e in ev
        if e["type"] == "response.output_item.added"
    ]
    assert added == [(0, "message"), (1, "function_call")]
    args_done = next(e for e in ev if e["type"] == "response.function_call_arguments.done")
    assert json.loads(args_done["arguments"]) == {"c": "Pune"} and args_done["output_index"] == 1
    joined = "".join(
        e["delta"] for e in ev if e["type"] == "response.function_call_arguments.delta"
    )
    assert joined == args_done["arguments"]
    item_done = [e for e in ev if e["type"] == "response.output_item.done"]
    assert [e["item"]["status"] for e in item_done] == ["completed", "completed"]
    assert item_done[1]["item"]["call_id"] == "call_1"
    final = ev[-1]["response"]
    assert [o["type"] for o in final["output"]] == ["message", "function_call"]


def test_stream_stopped_by_output_guard_ends_with_response_failed() -> None:
    r = Rig(RiskTier.TIER_2)
    r.fake.chunks = text_chunks("here is the key AKIAIOSFODNN7EXAMPLE. ", "and more text after it.")
    resp = post(r, stream=True)
    assert "AKIA" not in resp.text
    ev = events(resp)
    assert ev[-1]["type"] == "response.failed" and ev[-1]["response"]["status"] == "failed"
    assert "response.completed" not in [e["type"] for e in ev]
    assert r.h.budget("org:org-1")["reserved"] == 0
