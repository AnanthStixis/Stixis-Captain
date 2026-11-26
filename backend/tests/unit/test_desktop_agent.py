"""Desktop agent v1 (ADR-006): the gateway endpoint that receives the Windows agent's daily
report. Observe-only metadata; a closed report format; idempotent per device and day."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import AutonomyLevel, PrincipalType, RiskTier
from captain.devprovider import mock_transport
from captain.integrations.api_gateway.app import create_gateway_app
from captain.integrations.api_gateway.provider import OpenAICompatibleClient
from captain.integrations.telemetry.desktop_agent import DesktopReport, records_from_report
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness
from tests.unit.test_gateway import FixedSecrets

REPORT: dict[str, Any] = {
    "agent_version": "1.0.0",
    "device": "3f9a0c1e5b7d4e8a",
    "user": "ravi@corp.example",
    "day": "2026-11-26",
    "os": "windows",
    "apps": [
        {"app": "vscode", "installed": True, "running": True, "version": "1.107.0"},
        {"app": "claude", "installed": True, "running": False},
        {"app": "chatgpt", "installed": False, "running": False},
    ],
    "mcp_servers": [
        {"app": "vscode", "server": "captain", "transport": "http", "remote_host": "captain.corp"},
        {"app": "claude", "server": "filesystem", "transport": "stdio"},
        {
            "app": "cursor",
            "server": "jira",
            "transport": "http",
            "remote_host": "mcp.atlassian.com",
        },
    ],
}


def rig(
    role: str | None = "desktop-agent",
) -> tuple[CaptainCoreHarness, TestClient, dict[str, str]]:
    h = CaptainCoreHarness()
    h.register_key("agent", make_context("desktop-fleet", RiskTier.TIER_3, AutonomyLevel.A1,
                                         PrincipalType.SERVICE_ACCOUNT))  # fmt: skip
    ids = h.built.infra.identity_store
    p = ids.get_principal("desktop-fleet")
    assert p is not None
    ids.put_principal(replace(p, roles=frozenset({role} if role else set())))
    app = create_gateway_app(
        h.core,
        OpenAICompatibleClient(FixedSecrets(), transport=mock_transport()),
        captain_hosts=("captain.corp",),
        desktop_actors={"ravi@corp.example": "ravi"},
    )
    return h, TestClient(app), {"Authorization": f"Bearer {h._keys['agent']}"}  # noqa: SLF001


def ledger(h: CaptainCoreHarness) -> list[dict[str, Any]]:
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    return [b for (_p, _t, b) in store.outbox.values()]


def test_report_becomes_inventory_and_bypass_evidence() -> None:
    h, c, hdr = rig()
    r = c.post("/v1/desktop/report", headers=hdr, json=REPORT)
    assert r.status_code == 200, r.text
    # 2 apps present (chatgpt is neither installed nor running), 2 bypasses, 1 via Captain
    assert r.json() == {"recorded": 4, "apps": 2, "bypass_signals": 2, "via_captain": 1}
    events = [b for b in ledger(h) if b["event_type"] == "call.observed"]
    assert {e["channel"] for e in events} == {"DESKTOP"}
    bypass = [e for e in events if e["coverage"] == "BYPASS_DETECTED"]
    assert {e["details"]["v_server"] for e in bypass} == {"filesystem", "jira"}
    assert all(e["principal_id"] == "ravi" for e in events)  # mapped user, not the UPN


def test_resending_the_same_day_changes_nothing() -> None:
    h, c, hdr = rig()
    c.post("/v1/desktop/report", headers=hdr, json=REPORT)
    c.post("/v1/desktop/report", headers=hdr, json=REPORT)
    ids = [b["event_id"] for b in ledger(h) if b["event_type"] == "call.observed"]
    assert len(ids) == len(set(ids)) == 4  # same event ids: the ledger keeps one of each


def test_unmapped_user_is_hashed_never_named() -> None:
    h, c, hdr = rig()
    c.post("/v1/desktop/report", headers=hdr, json={**REPORT, "user": "CORP\\meera"})
    text = json.dumps(ledger(h))
    assert "meera" not in text and "vendor-unmapped:" in text


@pytest.mark.parametrize(
    "change",
    [
        {"apps": [{"app": "vscode", "installed": True, "running": True, "path": "C:\\x"}]},
        {
            "mcp_servers": [
                {
                    "app": "claude",
                    "server": "fs",
                    "transport": "stdio",
                    "args": ["--root", "C:\\Users\\ravi"],
                }
            ]
        },  # fmt: skip
        {"hostname": "RAVI-LAPTOP"},
        {"device": "RAVI-LAPTOP"},
        {"apps": [{"app": "notepad", "installed": True, "running": True}]},
    ],
)
def test_the_report_format_is_closed(change: dict[str, Any]) -> None:
    """A changed agent cannot quietly send more: unknown fields, a machine name, paths or
    arguments are refused, and nothing is recorded."""
    h, c, hdr = rig()
    r = c.post("/v1/desktop/report", headers=hdr, json={**REPORT, **change})
    assert r.status_code == 422
    assert not [b for b in ledger(h) if b["event_type"] == "call.observed"]


def test_only_desktop_agent_keys_may_report() -> None:
    h, c, hdr = rig(role=None)
    r = c.post("/v1/desktop/report", headers=hdr, json=REPORT)
    assert r.status_code == 403
    assert c.post("/v1/desktop/report", json=REPORT).status_code == 401


def test_records_from_report_counts_captain_hosts_case_insensitively() -> None:
    rep = DesktopReport.model_validate(REPORT)
    recs, via = records_from_report(rep, ["CAPTAIN.CORP"])
    assert via == 1 and len(recs) == 4
