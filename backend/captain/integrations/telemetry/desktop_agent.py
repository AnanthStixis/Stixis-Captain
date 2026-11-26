"""Desktop agent v1 (ADR-006): what the Windows agent reports, and how it becomes evidence.

The agent (`desktop/windows/Invoke-CaptainDesktopAgent.ps1`) runs once a day on a managed
machine and sends ONE report: which AI desktop apps are installed or running, and which MCP
servers those apps are configured to use. It is observe-only: it never blocks, never reads
content, never sends file paths, command arguments, environment variables, headers or
tokens. The report format below is closed (unknown fields are refused), so a changed agent
cannot quietly start sending more.

What the evidence can honestly say:
- an app record = "this app was installed / running on this device that day" (inventory,
  OBSERVED). It does not say the app was used, or for what.
- an MCP server not going through Captain = a bypass SIGNAL (BYPASS_DETECTED), never
  proof of use. A local (stdio) server can never be governed by a proxy, so it is always
  reported.

Re-sending the same day's report changes nothing: record ids are per device, item and day.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from captain.core.models import Channel, CoverageClass
from captain.integrations.telemetry.base import ObservedRecord

APPS = ("chatgpt", "claude", "cursor", "vscode")  # in scope for v1 (decided 2026-11-26)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSeen(_Closed):
    app: str = Field(pattern="^(chatgpt|claude|cursor|vscode)$")
    installed: bool
    running: bool
    version: str | None = Field(default=None, max_length=40, pattern=r"^[0-9A-Za-z.\-+]*$")


class McpServerSeen(_Closed):
    app: str = Field(pattern="^(claude|cursor|vscode)$")
    server: str = Field(min_length=1, max_length=80)
    transport: str = Field(pattern="^(stdio|http)$")
    remote_host: str | None = Field(default=None, max_length=253, pattern=r"^[0-9A-Za-z.\-:\[\]]*$")


class DesktopReport(_Closed):
    """One device, one user, one day."""

    agent_version: str = Field(max_length=20, pattern=r"^[0-9A-Za-z.\-]+$")
    device: str = Field(pattern="^[0-9a-f]{16,64}$")  # a hash of the machine id, never the name
    user: str = Field(min_length=1, max_length=256)  # UPN or DOMAIN\user; mapped or hashed
    day: date
    os: str = Field(pattern="^windows$")
    apps: list[AppSeen] = Field(default_factory=list, max_length=len(APPS))
    mcp_servers: list[McpServerSeen] = Field(default_factory=list, max_length=200)


def _id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def records_from_report(r: DesktopReport, captain_hosts: Iterable[str]) -> tuple[
    list[ObservedRecord], int
]:  # fmt: skip
    """(records to write, MCP servers that correctly go through Captain)."""
    hosts = {h.lower() for h in captain_hosts}
    day = r.day.isoformat()
    out: list[ObservedRecord] = []
    for a in r.apps:
        if not (a.installed or a.running):
            continue
        out.append(
            ObservedRecord(
                "desktop:inventory",
                f"{_id(r.device, a.app)}|{day}",
                r.day,
                r.user,
                facts={"app": a.app, "installed": a.installed, "running": a.running,
                       "version": a.version, "device": r.device[:16],
                       "agent_version": r.agent_version},
                coverage=CoverageClass.OBSERVED,
                channel=Channel.DESKTOP,
            )
        )  # fmt: skip
    via_captain = 0
    for s in r.mcp_servers:
        if s.transport == "http" and (s.remote_host or "").lower() in hosts:
            via_captain += 1
            continue
        out.append(
            ObservedRecord(
                "desktop:mcp-config",
                f"{_id(r.device, s.app, s.server)}|{day}",
                r.day,
                r.user,
                facts={"app": s.app, "server": s.server, "transport": s.transport,
                       "remote_host": s.remote_host, "via_captain": False,
                       "device": r.device[:16], "agent_version": r.agent_version},
                coverage=CoverageClass.BYPASS_DETECTED,
                channel=Channel.DESKTOP,
            )
        )  # fmt: skip
    return out, via_captain
