"""Desktop MCP config check: reports servers that bypass Captain, never their secrets."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from captain.core.models import CoverageClass
from captain.integrations.telemetry.base import ActorMap, ImportFailure, to_event
from captain.integrations.telemetry.desktop_mcp import scan_configs

CLAUDE_STYLE = {
    "mcpServers": {
        "github": {
            "command": "npx",
            "args": ["-y", "some-server", "--token", "TOP-SECRET-ARG"],
            "env": {"GITHUB_TOKEN": "ENV-SECRET-VALUE"},
        },
        "company": {
            "url": "https://captain.example.com/mcp",
            "headers": {"Authorization": "Bearer HDR-SECRET"},
        },
        "remote": {"url": "https://mcp.vendor.example/sse?key=QUERY-SECRET"},
        "empty": {},
    }
}
VSCODE_STYLE = {"servers": {"db": {"type": "http", "url": "https://db.internal.example/mcp"}}}
TODAY = date(2026, 11, 25)


def write(tmp_path: Path, name: str, doc: object) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_only_servers_that_bypass_captain_are_reported(tmp_path: Path) -> None:
    f1 = write(tmp_path, "claude_desktop_config.json", CLAUDE_STYLE)
    f2 = write(tmp_path, "mcp.json", VSCODE_STYLE)
    scan = scan_configs([f1, f2], ["captain.example.com"], "ravi", TODAY)
    assert scan.files_read == 2 and scan.via_captain == 1  # "company" is the right setup
    got = {(r.facts["server"], r.facts["transport"], r.facts["remote_host"]) for r in scan.records}
    assert got == {
        ("github", "stdio", None),  # a local program: no proxy can govern it
        ("remote", "http", "mcp.vendor.example"),
        ("db", "http", "db.internal.example"),
    }
    assert all(r.coverage is CoverageClass.BYPASS_DETECTED for r in scan.records)


def test_secrets_in_the_config_never_reach_the_records(tmp_path: Path) -> None:
    f = write(tmp_path, "cfg.json", CLAUDE_STYLE)
    scan = scan_configs([f], ["captain.example.com"], "ravi", TODAY)
    text = str([(r.facts, r.source_id) for r in scan.records])
    for secret in (
        "TOP-SECRET-ARG",
        "ENV-SECRET-VALUE",
        "HDR-SECRET",
        "QUERY-SECRET",
        "some-server",
    ):
        assert secret not in text
    ev = to_event(scan.records[0], ActorMap({}))
    assert ev.register_id == "bypass:desktop:mcp-config" and ev.usage is None
    assert "ravi" not in str(ev.model_dump())  # the machine user is pseudonymised
    assert to_event(scan.records[0], ActorMap({"ravi": "r1"})).principal_id == "r1"


def test_rerunning_the_same_day_is_the_same_records(tmp_path: Path) -> None:
    f = write(tmp_path, "cfg.json", CLAUDE_STYLE)
    a = scan_configs([f], ["captain.example.com"], "ravi", TODAY)
    b = scan_configs([f], ["captain.example.com"], "ravi", TODAY)
    assert [to_event(r, ActorMap({})).event_id for r in a.records] == [
        to_event(r, ActorMap({})).event_id for r in b.records
    ]
    next_day = scan_configs([f], ["captain.example.com"], "ravi", date(2026, 11, 26))
    assert (
        to_event(next_day.records[0], ActorMap({})).event_id
        != to_event(a.records[0], ActorMap({})).event_id
    )


def test_unreadable_or_odd_files_fail_clearly_not_silently(tmp_path: Path) -> None:
    with pytest.raises(ImportFailure, match="cannot read"):
        scan_configs([tmp_path / "missing.json"], ["x"], "u", TODAY)
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(ImportFailure, match="cannot read"):
        scan_configs([bad], ["x"], "u", TODAY)
    with pytest.raises(ImportFailure, match="not a JSON object"):
        scan_configs([write(tmp_path, "list.json", [1, 2])], ["x"], "u", TODAY)
    assert (
        scan_configs([write(tmp_path, "other.json", {"nothing": 1})], ["x"], "u", TODAY).records
        == []
    )
