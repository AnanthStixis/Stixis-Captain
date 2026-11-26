"""Mock vendor usage API for development: shaped like Anthropic's Claude Code analytics
endpoint (`GET /v1/organizations/usage_report/claude_code`). Never expose it.

    uvicorn captain.devvendor:app --host 127.0.0.1 --port 8320

Serves two developers per day over two pages (to exercise pagination) and counts the
calls it receives at GET /_calls, so a scheduled import can be seen not to re-fetch days.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Query

app = FastAPI(title="Dev vendor API")
CALLS: list[str] = []


def _item(day: str, email: str, sessions: int, cents: int) -> dict[str, object]:
    return {
        "date": f"{day}T00:00:00Z",
        "actor": {"type": "user_actor", "email_address": email},
        "customer_type": "api",
        "terminal_type": "vscode",
        "subscription_type": None,
        "is_remote": False,
        "core_metrics": {
            "num_sessions": sessions,
            "lines_of_code": {"added": 100 * sessions, "removed": 10 * sessions},
            "commits_by_claude_code": sessions,
            "pull_requests_by_claude_code": 0,
        },
        "tool_actions": {"edit_tool": {"accepted": 3 * sessions, "rejected": 1}},
        "model_breakdown": [
            {
                "model": "claude-sonnet-5-5",
                "tokens": {
                    "input": 10_000 * sessions,
                    "output": 2_000 * sessions,
                    "cache_read": 500,
                    "cache_creation": 100,
                },
                "estimated_cost": {"currency": "USD", "amount": cents},
            }
        ],
    }


@app.get("/v1/organizations/usage_report/claude_code")
def report(
    starting_at: str,
    limit: int = Query(20, le=1000),
    page: str | None = None,
    x_api_key: str | None = Header(default=None),
) -> dict[str, object]:
    if not x_api_key:
        raise HTTPException(401, "missing key")
    CALLS.append(f"{starting_at}|{page or 'first'}")
    people = [("asha@example.com", 3, 250), ("ravi@example.com", 1, 90)]
    if page is None:
        return {"data": [_item(starting_at, *people[0])], "has_more": True, "next_page": "p2"}
    return {"data": [_item(starting_at, *people[1])], "has_more": False, "next_page": None}


@app.get("/_calls")
def calls() -> list[str]:
    return CALLS
