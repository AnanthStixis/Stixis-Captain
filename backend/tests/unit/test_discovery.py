"""Bypass discovery from proxy / IdP logs: signals, never governed spend."""

from __future__ import annotations

import pytest

from captain.core.models import CoverageClass
from captain.integrations.telemetry.base import ActorMap, ImportFailure, to_event
from captain.integrations.telemetry.discovery import classify, read_discovery

MAP = {
    "source": "proxy",
    "day": "Date",
    "actor": "User",
    "target": "Host",
    "count": "Requests",
    "id_columns": ["Date", "User", "Host"],
    "captain_hosts": ["captain.example.com"],
    "extra_services": {"intranet-ai.example.com": "Internal AI tool"},
}
LOG = """Date,User,Host,Requests
2026-11-24,ravi@example.com,chat.chatgpt.com,40
2026-11-24,ravi@example.com,www.news.example,9
2026-11-24,asha@example.com,captain.example.com,12
2026-11-24,asha@example.com,https://claude.ai/chat/abc,5
2026-11-24,asha@example.com,intranet-ai.example.com,3
2026-11-24,asha@example.com,notchatgpt.com,1
"""


def test_only_known_ai_services_that_are_not_captain_are_reported() -> None:
    recs = list(read_discovery(LOG, MAP))
    assert [r.facts["service"] for r in recs] == ["ChatGPT", "Claude", "Internal AI tool"]
    assert recs[0].facts["requests"] == 40
    assert all(r.coverage is CoverageClass.BYPASS_DETECTED for r in recs)


def test_matching_is_by_domain_boundary_not_substring() -> None:
    svc = {"chatgpt.com": "ChatGPT"}
    assert classify("evilchatgpt.com", svc, set()) is None
    assert classify("api.chatgpt.com:443", svc, set()) == "ChatGPT"
    assert classify("captain.example.com", {"example.com": "X"}, {"captain.example.com"}) is None


def test_event_is_bypass_with_no_spend_and_a_pseudonymous_identity() -> None:
    rec = next(read_discovery(LOG, MAP))
    ev = to_event(rec, ActorMap({}))
    assert (
        ev.coverage is CoverageClass.BYPASS_DETECTED and ev.register_id == "bypass:discovery:proxy"
    )
    assert ev.usage is None and ev.principal_id and ev.principal_id.startswith("vendor-unmapped:")
    assert "ravi" not in str(ev.model_dump())
    mapped = to_event(rec, ActorMap({"ravi@example.com": "ravi"}))
    assert mapped.principal_id == "ravi"


def test_bad_mapping_or_file_is_refused_clearly() -> None:
    with pytest.raises(ImportFailure):
        list(read_discovery(LOG, {**MAP, "target": "Nope"}))
    with pytest.raises(ImportFailure):
        list(read_discovery(LOG, {"source": "x"}))
