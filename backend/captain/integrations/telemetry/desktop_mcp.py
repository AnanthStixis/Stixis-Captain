"""Desktop MCP configuration check: which MCP servers is a desktop AI app configured to use
WITHOUT going through Captain's MCP proxy?

An administrator (or an endpoint-management script) runs this on a machine, pointing it at the
MCP configuration files of the apps in scope. It reads each file, and for every server it
reports ONLY: the server's name, whether it is a local program (stdio) or a remote address
(http), and the remote host name. It NEVER reads or reports commands' arguments, environment
variables, headers, tokens or file paths inside them: MCP configs routinely hold secrets.

Two config shapes are understood (both are JSON):
- {"mcpServers": {"<name>": {"command": ..., "args": [...]} | {"url": ...}}}   (Claude Desktop,
  Cursor and others use this shape)
- {"servers": {"<name>": {"type": "http", "url": ...} | {"command": ...}}}      (VS Code mcp.json)
The file LOCATIONS differ per app and version and are not assumed here: you pass the paths.

What it proves, honestly: a server that is configured here but not through Captain is a
bypass SIGNAL (BYPASS_DETECTED). It does not say the server was used, what data crossed it, or
whether the use was allowed. A local (stdio) server cannot be governed by an HTTP proxy at all,
so it is always reported. A server whose host is a Captain host is the correct setup and is
not reported (it is counted in the summary).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from captain.core.models import CoverageClass
from captain.integrations.telemetry.base import Facts, ImportFailure, ObservedRecord


@dataclass
class ConfigScan:
    records: list[ObservedRecord]
    via_captain: int = 0
    files_read: int = 0


def _servers(doc: Any, path: Path) -> dict[str, dict[str, Any]]:
    if not isinstance(doc, dict):
        raise ImportFailure(f"{path.name}: not a JSON object")
    for key in ("mcpServers", "servers"):
        section = doc.get(key)
        if isinstance(section, dict):
            return {str(k): v for k, v in section.items() if isinstance(v, dict)}
    return {}


def _host(url: str) -> str | None:
    try:
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def scan_configs(
    paths: Iterable[Path],
    captain_hosts: Iterable[str],
    user: str,
    day: date | None = None,
) -> ConfigScan:
    """Read the config files and return a BYPASS_DETECTED record per server not going
    through Captain. `user` is the machine user (pseudonymised later by the actor map)."""
    hosts = {h.lower() for h in captain_hosts}
    today = day or datetime.now(UTC).date()
    out = ConfigScan(records=[])
    for path in paths:
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            raise ImportFailure(f"cannot read {path.name}: {type(e).__name__}") from e
        out.files_read += 1
        for name, cfg in sorted(_servers(doc, path).items()):
            url = cfg.get("url")
            if isinstance(url, str) and url:
                host = _host(url)
                if host in hosts:
                    out.via_captain += 1
                    continue
                transport, remote = "http", host or "unparseable"
            elif cfg.get("command"):
                transport, remote = "stdio", None  # a local program: never governable by a proxy
            else:
                continue  # neither a command nor a URL: nothing to report
            facts: Facts = {
                "server": name[:80],
                "transport": transport,
                "remote_host": remote,
                "config_file": path.name,
                "via_captain": False,
            }
            # Stable per machine user, file name and server: running it daily is one record
            # per day, and re-running the same day changes nothing.
            ident = hashlib.sha256(f"{user}|{path.name}|{name}".encode()).hexdigest()[:20]
            out.records.append(
                ObservedRecord(
                    "desktop:mcp-config",
                    f"{ident}|{today.isoformat()}",
                    today,
                    user,
                    facts=facts,
                    coverage=CoverageClass.BYPASS_DETECTED,
                )
            )
    return out
