"""Scheduled vendor imports (run by the Core worker; see captain.workers).

A scheduled import looks at the last `lookback_days` COMPLETE days and imports each one
once:
- A day counts as complete `settle_hours` after midnight UTC (vendor data lags: Claude
  Code analytics by up to an hour), so a half-filled day is never imported.
- A day that imported cleanly is remembered and never fetched again. A day that failed
  is retried on the next run. Imports are idempotent anyway, so a retry never duplicates
  evidence.
- Because a completed day is final here, a vendor that later revises that day's numbers
  will not be re-imported. Re-run `python -m captain.integrations.telemetry ...` by hand
  to add records that appeared late; already-sealed records stay as they were.
- `alert_after` failed runs in a row raise one alert; the next success clears it.

Configuration (environment), parsed by `jobs_from_env`:

    CAPTAIN_IMPORTS='[{"source": "claude-code", "every_hours": 6, "lookback_days": 3},
                      {"source": "copilot", "org": "acme"}]'
    ANTHROPIC_ADMIN_KEY / GITHUB_TOKEN      credentials, never inside CAPTAIN_IMPORTS
    CAPTAIN_ACTOR_MAP_FILE                  JSON: vendor identity -> Captain principal id
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from captain.core.decision_api import CoreDecisionAPI
from captain.integrations.telemetry.base import (
    ActorMap,
    ImportFailure,
    RecordSource,
    import_records,
)
from captain.integrations.telemetry.claude_code import ClaudeCodeAnalytics
from captain.integrations.telemetry.copilot import CopilotUsers

log = logging.getLogger("captain.imports")


class DoneStore(Protocol):
    """Remembers which (source, day) pairs imported cleanly."""

    def is_done(self, key: str) -> bool: ...
    def mark(self, key: str, info: dict[str, Any]) -> None: ...


class InMemoryDone:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def is_done(self, key: str) -> bool:
        return key in self.items

    def mark(self, key: str, info: dict[str, Any]) -> None:
        self.items[key] = info


# (kind, title, details) -> raise an operator alert
Notify = Callable[[str, str, dict[str, str | int | float | bool | None]], None]


@dataclass
class RunSummary:
    days_imported: list[str] = field(default_factory=list)
    days_skipped: int = 0
    recorded: int = 0
    error: str | None = None


class ScheduledImport:
    def __init__(
        self,
        name: str,
        source: RecordSource,
        core: CoreDecisionAPI,
        actors: ActorMap,
        done: DoneStore,
        notify: Notify,
        *,
        every_s: float = 6 * 3600,
        lookback_days: int = 3,
        settle_hours: float = 3,
        alert_after: int = 3,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.name, self.every_s = name, every_s
        self._src, self._core, self._actors = source, core, actors
        self._done, self._notify = done, notify
        self._lookback, self._settle = lookback_days, settle_hours
        self._alert_after, self._clock = alert_after, clock
        self._streak = 0
        self.last: RunSummary | None = None

    def latest_complete_day(self) -> date:
        return (self._clock() - timedelta(hours=self._settle)).date() - timedelta(days=1)

    def run(self) -> RunSummary:
        out = RunSummary()
        end = self.latest_complete_day()
        days = [end - timedelta(days=i) for i in range(self._lookback - 1, -1, -1)]
        for day in days:
            key = f"{self._src.vendor}|{self.name}|{day.isoformat()}"
            if self._done.is_done(key):
                out.days_skipped += 1
                continue
            try:
                records = list(self._src.fetch(day))
                result = import_records(self._core, records, self._actors)
            except ImportFailure as e:
                out.error = str(e)
                break  # the same problem would hit every remaining day
            if result.failed:
                out.error = f"{result.failed} record(s) could not be queued for {day}"
                break
            self._done.mark(key, {"recorded": result.recorded, "at": self._clock().isoformat()})
            out.days_imported.append(day.isoformat())
            out.recorded += result.recorded
        self._after(out)
        self.last = out
        return out

    def _after(self, out: RunSummary) -> None:
        if out.error is None:
            self._streak = 0
            log.info(
                "import %s: %d day(s) imported, %d record(s), %d already done",
                self.name, len(out.days_imported), out.recorded, out.days_skipped,
            )  # fmt: skip
            return
        self._streak += 1
        log.warning("import %s failed (%d in a row): %s", self.name, self._streak, out.error)
        if self._streak == self._alert_after:
            self._notify(
                "import.failed",
                f"Scheduled import '{self.name}' keeps failing",
                {"source": self.name, "failures_in_a_row": self._streak, "last_error": out.error},
            )


# ------------------------------------------------------------------ configuration


def jobs_from_env(
    env: Mapping[str, str],
    core: CoreDecisionAPI,
    done: DoneStore,
    notify: Notify,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[list[ScheduledImport], list[str]]:
    """Build the configured jobs. A job that cannot be built is reported in the error
    list and skipped, so a bad import setting never stops the rest of the worker."""
    raw = env.get("CAPTAIN_IMPORTS", "").strip()
    if not raw:
        return [], []
    errors: list[str] = []
    try:
        spec = json.loads(raw)
        if not isinstance(spec, list):
            raise ValueError("must be a list")
    except ValueError as e:
        return [], [f"CAPTAIN_IMPORTS is not a JSON list: {e}"]
    try:
        mapping = {}
        if env.get("CAPTAIN_ACTOR_MAP_FILE"):
            mapping = json.loads(Path(env["CAPTAIN_ACTOR_MAP_FILE"]).read_text(encoding="utf-8"))
        actors = ActorMap(mapping)
    except (OSError, ValueError) as e:
        return [], [f"actor map could not be read: {e}"]
    jobs: list[ScheduledImport] = []
    for i, item in enumerate(spec):
        try:
            if not isinstance(item, dict):
                raise ValueError("each entry must be an object")
            source, name = _source(item, env)
            jobs.append(
                ScheduledImport(
                    name,
                    source,
                    core,
                    actors,
                    done,
                    notify,
                    every_s=float(item.get("every_hours", 6)) * 3600,
                    lookback_days=int(item.get("lookback_days", 3)),
                    settle_hours=float(item.get("settle_hours", 3)),
                    clock=clock,
                )  # fmt: skip
            )
        except (ValueError, KeyError) as e:
            errors.append(f"CAPTAIN_IMPORTS[{i}]: {e}")
    return jobs, errors


def _source(item: dict[str, Any], env: Mapping[str, str]) -> tuple[RecordSource, str]:
    kind = item.get("source")
    if kind == "claude-code":
        key = env.get("ANTHROPIC_ADMIN_KEY")
        if not key:
            raise ValueError("claude-code needs ANTHROPIC_ADMIN_KEY in the environment")
        kwargs = {"base_url": item["base_url"]} if "base_url" in item else {}
        return ClaudeCodeAnalytics(key, **kwargs), "claude-code"
    if kind == "copilot":
        token, org = env.get("GITHUB_TOKEN"), item.get("org")
        if not token or not org:
            raise ValueError("copilot needs GITHUB_TOKEN in the environment and an 'org'")
        return CopilotUsers(str(org), token), f"copilot:{org}"
    raise ValueError(f"unknown source {kind!r} (use claude-code or copilot)")
