"""Run a vendor usage import:  python -m captain.integrations.telemetry <source> [options]

    claude-code   Anthropic Claude Code analytics   (env ANTHROPIC_ADMIN_KEY)
    copilot       GitHub Copilot per-user usage     (env GITHUB_TOKEN; --org ORG)
    csv           a vendor export plus a column mapping (--file F --mapping M.json)
    discovery     a proxy / DNS / IdP log export plus a mapping: records BYPASS_DETECTED
    desktop-mcp   MCP config files of desktop AI apps (--mcp-config F, --captain-host H):
                  MCP servers configured to bypass Captain, as BYPASS_DETECTED

Common options: --day YYYY-MM-DD (default: yesterday, UTC), --days N (that many days ending
at --day), --actor-map FILE (JSON object: vendor identity -> Captain principal id).
Credentials come from the environment, never from arguments. Events are queued as OBSERVED
evidence; the ledger worker seals them. Re-running a day is safe.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from captain.config import build_from_env
from captain.envfile import current_env
from captain.integrations.telemetry.base import (
    ActorMap,
    ImportFailure,
    ImportResult,
    ObservedRecord,
    RecordSource,
    import_records,
)
from captain.integrations.telemetry.claude_code import ClaudeCodeAnalytics
from captain.integrations.telemetry.copilot import CopilotUsers
from captain.integrations.telemetry.csv_import import read_csv
from captain.integrations.telemetry.desktop_mcp import scan_configs
from captain.integrations.telemetry.discovery import read_discovery


def _days(end: date, n: int) -> list[date]:
    return [end - timedelta(days=i) for i in range(n - 1, -1, -1)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="captain-import", description=__doc__)
    ap.add_argument("source", choices=["claude-code", "copilot", "csv", "discovery", "desktop-mcp"])
    ap.add_argument("--day", type=date.fromisoformat)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--org")
    ap.add_argument("--file", type=Path)
    ap.add_argument("--mapping", type=Path)
    ap.add_argument("--actor-map", type=Path)
    ap.add_argument("--mcp-config", type=Path, action="append", default=[])
    ap.add_argument("--captain-host", action="append", default=[])
    a = ap.parse_args(argv)

    env = current_env()
    end = a.day or (datetime.now(UTC).date() - timedelta(days=1))
    actors = ActorMap(json.loads(a.actor_map.read_text()) if a.actor_map else None)
    built, _ = build_from_env()

    total = ImportResult()
    try:
        runs: list[Iterable[ObservedRecord]] = []
        if a.source == "desktop-mcp":
            if not a.mcp_config or not a.captain_host:
                ap.error("desktop-mcp needs at least one --mcp-config and one --captain-host")
            import getpass

            scan = scan_configs(
                a.mcp_config, a.captain_host, getpass.getuser(), datetime.now(UTC).date()
            )
            print(
                f"read {scan.files_read} config file(s): {scan.via_captain} server(s) go through "
                f"Captain, {len(scan.records)} do not"
            )
            runs.append(scan.records)
        elif a.source in ("csv", "discovery"):
            if not a.file or not a.mapping:
                ap.error(f"{a.source} needs --file and --mapping")
            reader = read_discovery if a.source == "discovery" else read_csv
            runs.append(
                reader(a.file.read_text(encoding="utf-8-sig"), json.loads(a.mapping.read_text()))
            )
        else:
            src: RecordSource
            if a.source == "claude-code":
                if not env.get("ANTHROPIC_ADMIN_KEY"):
                    ap.error("set ANTHROPIC_ADMIN_KEY in the environment")
                src = ClaudeCodeAnalytics(env["ANTHROPIC_ADMIN_KEY"])
            else:
                if not a.org or not env.get("GITHUB_TOKEN"):
                    ap.error("copilot needs --org and GITHUB_TOKEN in the environment")
                src = CopilotUsers(a.org, env["GITHUB_TOKEN"])
            runs.extend(src.fetch(d) for d in _days(end, a.days))
        for records in runs:
            r = import_records(built.core, records, actors)
            total.recorded += r.recorded
            total.failed += r.failed
            total.unmapped_actors += r.unmapped_actors
    except ImportFailure as e:
        print(f"import failed: {e}", file=sys.stderr)
        return 1
    print(
        f"recorded {total.recorded} events "
        f"({total.unmapped_actors} from identities not in the actor map, {total.failed} failed)"
    )
    return 1 if total.failed else 0


if __name__ == "__main__":
    sys.exit(main())
