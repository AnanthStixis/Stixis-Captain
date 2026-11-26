"""Anthropic Claude Code analytics importer (per-user daily usage).

Source: Anthropic's Admin API `GET /v1/organizations/usage_report/claude_code`
(documentation checked 2026-11). Per the documentation:
- `starting_at` is one UTC day (YYYY-MM-DD); `limit` default 20, max 1000; cursor
  pagination with `has_more` / `next_page` (sent back as `page`).
- Data appears with up to a 1 hour delay and only covers usage authenticated against a
  Console organisation (not Claude subscription seats).
- Each item is one actor on one day: `actor` is a `user_actor` (email_address) or an
  `api_actor` (api_key_name); `core_metrics`, `tool_actions`, and a `model_breakdown`
  with tokens and `estimated_cost.amount` in CENTS of USD.
The documentation's examples use `x-api-key` with an Admin key and `anthropic-version`
2026-10-21; its reference pages show a Bearer header instead. We send `x-api-key`; set
`auth_header` if your organisation needs the other form.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx

from captain.core.models import TokenUsage
from captain.integrations.telemetry.base import Facts, ImportFailure, ObservedRecord

VENDOR = "anthropic_claude_code"
CENTS_TO_MICRO = 10_000  # 1 cent = 0.01 USD = 10,000 millionths


def _num(v: Any) -> int:
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0


class ClaudeCodeAnalytics:
    vendor = VENDOR

    def __init__(
        self,
        admin_key: str,
        *,
        base_url: str = "https://api.anthropic.com",
        version: str = "2026-10-21",
        auth_header: str = "x-api-key",
        transport: httpx.BaseTransport | None = None,
        max_pages: int = 200,
    ) -> None:
        if not base_url.startswith("https://") and "127.0.0.1" not in base_url:
            raise ValueError("the Anthropic Admin API must be reached over https")
        self._base = base_url.rstrip("/")
        self._headers = {auth_header: admin_key, "anthropic-version": version}
        self._max_pages = max_pages
        self._c = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=False, transport=transport
        )

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self._c.get(
                f"{self._base}/v1/organizations/usage_report/claude_code",
                params=params,
                headers=self._headers,
            )
        except httpx.HTTPError as e:
            raise ImportFailure(f"Anthropic Admin API unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            raise ImportFailure(f"Anthropic Admin API answered HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise ImportFailure("Anthropic Admin API sent invalid JSON") from e
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ImportFailure("Anthropic Admin API sent an unexpected response")
        return body

    def fetch(self, day: date) -> Iterator[ObservedRecord]:
        params: dict[str, Any] = {"starting_at": day.isoformat(), "limit": 1000}
        for _ in range(self._max_pages):
            body = self._get(params)
            for item in body["data"]:
                if isinstance(item, dict):
                    yield from self._records(day, item)
            if not body.get("has_more") or not body.get("next_page"):
                return
            params = {**params, "page": body["next_page"]}
        raise ImportFailure("too many pages; stopping")

    @staticmethod
    def _records(day: date, item: dict[str, Any]) -> Iterator[ObservedRecord]:
        actor = item.get("actor") or {}
        who = actor.get("email_address") or actor.get("api_key_name")
        kind = str(actor.get("type", "unknown"))
        scope = "|".join(
            str(item.get(k)) for k in ("terminal_type", "customer_type", "subscription_type")
        )
        key = f"{day}|{kind}|{who}|{scope}|{item.get('is_remote')}"
        facts: Facts = {
            "terminal_type": str(item.get("terminal_type")),
            "customer_type": str(item.get("customer_type")),
            "is_remote": bool(item.get("is_remote")),
        }
        core = item.get("core_metrics") or {}
        loc = core.get("lines_of_code") or {}
        facts.update(
            sessions=_num(core.get("num_sessions")),
            lines_added=_num(loc.get("added")),
            lines_removed=_num(loc.get("removed")),
            commits=_num(core.get("commits_by_claude_code")),
            pull_requests=_num(core.get("pull_requests_by_claude_code")),
        )
        for tool, counts in (item.get("tool_actions") or {}).items():
            if isinstance(counts, dict):
                facts[f"{tool}_accepted"] = _num(counts.get("accepted"))
                facts[f"{tool}_rejected"] = _num(counts.get("rejected"))
        # One activity record per actor-day (no model), then one usage record per model, so
        # summing tokens or cost never double-counts the activity counters.
        yield ObservedRecord(VENDOR, f"{key}|activity", day, who, facts=facts)
        for m in item.get("model_breakdown") or []:
            if not isinstance(m, dict):
                continue
            tokens = m.get("tokens") or {}
            cost = m.get("estimated_cost") or {}
            micro = (
                _num(cost.get("amount")) * CENTS_TO_MICRO if cost.get("currency") == "USD" else None
            )
            yield ObservedRecord(
                VENDOR,
                f"{key}|model|{m.get('model')}",
                day,
                who,
                model=str(m.get("model")),
                tokens=TokenUsage(
                    input_tokens=_num(tokens.get("input")),
                    output_tokens=_num(tokens.get("output")),
                    cached_input_tokens=_num(tokens.get("cache_read")),
                ),
                cost_micro=micro,
                facts={"cache_creation_tokens": _num(tokens.get("cache_creation"))},
            )
