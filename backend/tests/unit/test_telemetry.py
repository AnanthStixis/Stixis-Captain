"""Vendor usage importers: mapping to OBSERVED evidence, idempotency, and the vendor
adapters against responses shaped like the vendors' documentation (no live vendor calls)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import CoverageClass, EventType, TokenUsage
from captain.integrations.telemetry.base import (
    ActorMap,
    ImportFailure,
    ObservedRecord,
    event_id,
    import_records,
    to_event,
)
from captain.integrations.telemetry.claude_code import ClaudeCodeAnalytics
from captain.integrations.telemetry.copilot import CopilotUsers, parse_report
from captain.integrations.telemetry.csv_import import read_csv
from tests.harness.adapters import CaptainCoreHarness

DAY = date(2026, 10, 19)


# ------------------------------------------------------------------- mapping


def rec(**kw: Any) -> ObservedRecord:
    base: dict[str, Any] = {
        "vendor": "acme",
        "source_id": "r1",
        "day": DAY,
        "actor": "Dev@Example.com",
    }
    return ObservedRecord(**{**base, **kw})


def test_event_is_observed_never_governed_and_hides_unmapped_identities() -> None:
    ev = to_event(rec(), ActorMap())
    assert ev.coverage is CoverageClass.OBSERVED and ev.event_type is EventType.CALL_OBSERVED
    assert ev.principal_id and ev.principal_id.startswith("vendor-unmapped:")
    assert "example.com" not in ev.model_dump_json().lower()  # no raw identity in evidence
    assert ev.details and ev.details["principal_mapped"] is False
    # the pseudonym is stable, and not shared between vendors
    again = to_event(rec(), ActorMap()).principal_id
    other = to_event(rec(vendor="other"), ActorMap()).principal_id
    assert again == ev.principal_id and other != ev.principal_id


def test_mapped_identity_becomes_the_captain_principal_case_insensitively() -> None:
    ev = to_event(rec(), ActorMap({"dev@example.COM": "dev-1"}))
    assert ev.principal_id == "dev-1" and ev.details and ev.details["principal_mapped"] is True


def test_usage_and_cost_are_carried_as_observed_not_estimated_by_captain() -> None:
    ev = to_event(
        rec(model="m1", tokens=TokenUsage(input_tokens=10, output_tokens=5), cost_micro=1_130_000),
        ActorMap(),
    )
    assert ev.usage and ev.usage.source.value == "OBSERVED" and ev.usage.cost == 1_130_000
    assert ev.usage.usage.total_tokens == 15 and ev.model == "m1"
    assert to_event(rec(), ActorMap()).usage is None  # activity only: no usage invented


def test_event_ids_are_deterministic() -> None:
    assert event_id(rec()) == event_id(rec())
    assert event_id(rec()) != event_id(rec(source_id="r2"))
    assert event_id(rec()) != event_id(rec(vendor="other"))


def test_reimporting_the_same_records_adds_no_evidence() -> None:
    h = CaptainCoreHarness()
    records = [rec(source_id="a"), rec(source_id="b")]
    first = import_records(h.core, records, ActorMap())
    h.core.m.ledger.write_pending()
    second = import_records(h.core, records, ActorMap())
    h.core.m.ledger.write_pending()
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    sealed = [r for r in store.rows if r.event_type == "call.observed"]
    assert first.recorded == second.recorded == 2 and len(sealed) == 2


def test_a_record_that_cannot_be_queued_is_counted() -> None:
    h = CaptainCoreHarness()
    h.evidence_available = False
    r = import_records(h.core, [rec()], ActorMap())
    assert (r.recorded, r.failed) == (0, 1)


# --------------------------------------------------------------- Claude Code

CLAUDE_PAGE_1 = {
    "data": [
        {
            "date": "2026-10-19T00:00:00Z",
            "actor": {"type": "user_actor", "email_address": "developer@example.com"},
            "customer_type": "api",
            "terminal_type": "vscode",
            "subscription_type": None,
            "is_remote": False,
            "core_metrics": {
                "num_sessions": 5,
                "lines_of_code": {"added": 1543, "removed": 892},
                "commits_by_claude_code": 12,
                "pull_requests_by_claude_code": 2,
            },
            "tool_actions": {"edit_tool": {"accepted": 45, "rejected": 5}},
            "model_breakdown": [
                {
                    "model": "claude-opus-5-5",
                    "tokens": {
                        "input": 100000,
                        "output": 35000,
                        "cache_read": 10000,
                        "cache_creation": 5000,
                    },
                    "estimated_cost": {"currency": "USD", "amount": 113},
                },
                {
                    "model": "other",
                    "tokens": {"input": 1, "output": 1, "cache_read": 0, "cache_creation": 0},
                    "estimated_cost": {"currency": "EUR", "amount": 5},
                },
            ],
        }
    ],
    "has_more": True,
    "next_page": "page_abc",
}
CLAUDE_PAGE_2 = {
    "data": [
        {
            "date": "2026-10-19T00:00:00Z",
            "actor": {"type": "api_actor", "api_key_name": "ci-key"},
            "customer_type": "api",
            "terminal_type": "cli",
            "is_remote": True,
            "core_metrics": {"num_sessions": 1},
            "model_breakdown": [],
        }
    ],
    "has_more": False,
    "next_page": None,
}


def claude_client(seen: list[httpx.Request]) -> ClaudeCodeAnalytics:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        page = CLAUDE_PAGE_2 if req.url.params.get("page") == "page_abc" else CLAUDE_PAGE_1
        return httpx.Response(200, json=page)

    return ClaudeCodeAnalytics("sk-ant-admin01-test", transport=httpx.MockTransport(handler))


def test_claude_code_follows_pages_and_splits_activity_from_usage() -> None:
    seen: list[httpx.Request] = []
    records = list(claude_client(seen).fetch(DAY))
    first, second = seen
    assert first.url.path == "/v1/organizations/usage_report/claude_code"
    assert first.url.params["starting_at"] == "2026-10-19" and first.url.params["limit"] == "1000"
    assert second.url.params["page"] == "page_abc"
    assert first.headers["x-api-key"] == "sk-ant-admin01-test"
    assert first.headers["anthropic-version"] == "2026-10-21"

    activity = [r for r in records if r.model is None]
    usage = [r for r in records if r.model is not None]
    assert len(activity) == 2 and len(usage) == 2
    a = activity[0]
    assert a.actor == "developer@example.com" and a.facts["sessions"] == 5
    assert a.facts["lines_added"] == 1543 and a.facts["edit_tool_accepted"] == 45
    assert a.tokens is None and a.cost_micro is None  # counters never carry tokens
    opus = next(r for r in usage if r.model == "claude-opus-5-5")
    assert opus.tokens == TokenUsage(
        input_tokens=100000, output_tokens=35000, cached_input_tokens=10000
    )
    assert opus.cost_micro == 1_130_000  # 113 cents = $1.13
    assert next(r for r in usage if r.model == "other").cost_micro is None  # not USD: not guessed
    assert activity[1].actor == "ci-key"  # API actors are attributed by key name
    assert len({r.source_id for r in records}) == len(records)  # every record id is distinct


def test_claude_code_errors_are_import_failures() -> None:
    for status in (401, 403, 500):
        c = ClaudeCodeAnalytics(
            "k", transport=httpx.MockTransport(lambda _r, s=status: httpx.Response(s))
        )
        with pytest.raises(ImportFailure):
            list(c.fetch(DAY))
    odd = ClaudeCodeAnalytics(
        "k", transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"x": 1}))
    )
    with pytest.raises(ImportFailure):
        list(odd.fetch(DAY))
    with pytest.raises(ValueError):
        ClaudeCodeAnalytics("k", base_url="http://evil.example")


# -------------------------------------------------------------------- Copilot

COPILOT_ROWS = [
    {
        "day": "2026-10-19",
        "user_id": 1,
        "user_login": "login1",
        "code_acceptance_activity_count": 3,
        "code_generation_activity_count": 3,
        "loc_added_sum": 32,
        "loc_deleted_sum": 6,
        "used_agent": False,
        "used_chat": True,
        "used_cli": True,
        "user_initiated_interaction_count": 1,
        "totals_by_ide": [{"ide": "vscode", "code_acceptance_activity_count": 1}],
    },
    {"day": "2026-10-19", "user_id": 2, "user_login": "login2", "loc_added_sum": 1},
]


def copilot_client(files: dict[str, str], seen: list[httpx.Request]) -> CopilotUsers:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "api.github.com":
            return httpx.Response(
                200, json={"download_links": list(files), "report_day": "2026-10-19"}
            )
        body = files.get(str(req.url))
        return httpx.Response(200, text=body) if body is not None else httpx.Response(404)

    return CopilotUsers("acme", "ghp_secret", transport=httpx.MockTransport(handler))


def test_copilot_reads_per_user_counts_and_never_sends_the_token_to_download_hosts() -> None:
    url = "https://objects.example.net/report1?sig=abc"
    seen: list[httpx.Request] = []
    records = list(
        copilot_client({url: "\n".join(json.dumps(r) for r in COPILOT_ROWS)}, seen).fetch(DAY)
    )
    api, download = seen
    assert api.url.path == "/orgs/acme/copilot/metrics/reports/users-1-day"
    assert api.url.params["day"] == "2026-10-19"
    assert api.headers["x-github-api-version"] == "2026-03-10"
    assert api.headers["accept"] == "application/vnd.github+json"
    assert api.headers["authorization"] == "Bearer ghp_secret"
    assert "authorization" not in download.headers  # the signed URL carries its own access
    assert [r.actor for r in records] == ["login1", "login2"]
    one = records[0]
    assert one.facts["code_generation_activity_count"] == 3 and one.facts["used_chat"] is True
    assert one.tokens is None and one.cost_micro is None  # Copilot reports counts, not tokens


def test_copilot_report_formats_are_read_tolerantly_and_refused_otherwise() -> None:
    assert len(parse_report(json.dumps(COPILOT_ROWS))) == 2
    assert len(parse_report(json.dumps({"day_totals": COPILOT_ROWS}))) == 2
    assert len(parse_report("\n".join(json.dumps(r) for r in COPILOT_ROWS))) == 2
    assert parse_report("") == []
    with pytest.raises(ImportFailure):
        parse_report("<html>not a report</html>")


def test_copilot_refuses_unsafe_links_and_permission_errors() -> None:
    bad = copilot_client({"http://objects.example.net/r": "[]"}, [])
    with pytest.raises(ImportFailure, match="non-https"):
        list(bad.fetch(DAY))
    for status in (401, 403, 404):
        c = CopilotUsers(
            "acme", "t", transport=httpx.MockTransport(lambda _r, s=status: httpx.Response(s))
        )
        with pytest.raises(ImportFailure, match="permission"):
            list(c.fetch(DAY))


# ------------------------------------------------------------------------ CSV

EXPORT = "Date,Email,Model,Input Tokens,Output Tokens,Cost,Requests\n" + "\n".join(
    [
        '2026-10-19,a@example.com,gpt-x,1000,200,"1.50",12',
        "2026-10-19T08:00:00Z,b@example.com,gpt-x,5,5,0.01,1",
    ]
)
MAPPING = {
    "vendor": "cursor",
    "day": "Date",
    "actor": "Email",
    "model": "Model",
    "input_tokens": "Input Tokens",
    "output_tokens": "Output Tokens",
    "cost_usd": "Cost",
    "id_columns": ["Date", "Email", "Model"],
    "facts": {"requests": "Requests"},
}


def test_csv_export_is_mapped_by_the_administrators_column_mapping() -> None:
    records = list(read_csv(EXPORT, MAPPING))
    assert [r.vendor for r in records] == ["csv:cursor"] * 2
    first = records[0]
    assert first.day == DAY and first.actor == "a@example.com" and first.model == "gpt-x"
    assert first.tokens == TokenUsage(input_tokens=1000, output_tokens=200)
    assert first.cost_micro == 1_500_000 and first.facts == {"requests": 12}
    assert records[1].day == DAY  # ISO datetimes are accepted
    assert len({r.source_id for r in records}) == 2


def test_csv_mapping_errors_are_clear() -> None:
    with pytest.raises(ImportFailure, match="columns not found"):
        list(read_csv("A,B\n1,2\n", MAPPING))
    with pytest.raises(ImportFailure, match="id_columns"):
        list(read_csv(EXPORT, {"vendor": "x", "day": "Date"}))
    with pytest.raises(ImportFailure, match="vendor"):
        list(read_csv(EXPORT, {**MAPPING, "vendor": "bad vendor!"}))
