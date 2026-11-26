"""Scheduled vendor imports: which days run, retries, alerts, configuration, and the worker
running them on a background thread without blocking the ledger writer."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from captain.integrations.telemetry.base import ActorMap, ImportFailure, ObservedRecord
from captain.integrations.telemetry.schedule import (
    InMemoryDone,
    ScheduledImport,
    jobs_from_env,
)
from captain.workers.runner import CoreWorker, Job, LocalLease
from tests.harness.adapters import CaptainCoreHarness


class FakeSource:
    vendor = "fake"

    def __init__(self) -> None:
        self.fetched: list[date] = []
        self.fail_on: set[date] = set()
        self.always_fail = False

    def fetch(self, day: date) -> Iterator[ObservedRecord]:
        self.fetched.append(day)
        if self.always_fail or day in self.fail_on:
            raise ImportFailure("vendor said no")
        yield ObservedRecord("fake", f"{day}|a", day, "a@example.com", facts={"n": 1})
        yield ObservedRecord("fake", f"{day}|b", day, "b@example.com", facts={"n": 2})


class Clock:
    def __init__(self, iso: str) -> None:
        self.now = datetime.fromisoformat(iso).replace(tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def make(
    clock: Clock, **kw: Any
) -> tuple[ScheduledImport, FakeSource, InMemoryDone, list[tuple[str, str]], CaptainCoreHarness]:
    h = CaptainCoreHarness()
    src, done, alerts = FakeSource(), InMemoryDone(), []
    job = ScheduledImport(
        "fake",
        src,
        h.core,
        ActorMap(),
        done,
        lambda kind, title, details: alerts.append((kind, title)),
        clock=clock,
        **kw,
    )
    return job, src, done, alerts, h


def test_only_complete_days_are_imported() -> None:
    # 02:00 UTC on the 20th with a 3 hour settle time: the 19th is not complete yet.
    job, src, _, _, _ = make(Clock("2026-11-20T02:00:00"), lookback_days=1)
    assert job.latest_complete_day() == date(2026, 11, 18)
    job.run()
    assert src.fetched == [date(2026, 11, 18)]
    job2, src2, _, _, _ = make(Clock("2026-11-20T03:00:00"), lookback_days=1)
    assert job2.latest_complete_day() == date(2026, 11, 19)
    job2.run()
    assert src2.fetched == [date(2026, 11, 19)]


def test_each_day_is_fetched_once_and_new_days_are_picked_up() -> None:
    clock = Clock("2026-11-20T06:00:00")
    job, src, done, _, h = make(clock, lookback_days=3)
    first = job.run()
    assert first.days_imported == ["2026-11-17", "2026-11-18", "2026-11-19"]
    assert first.recorded == 6 and len(done.items) == 3
    again = job.run()  # nothing new: no vendor calls at all
    assert again.days_skipped == 3 and again.recorded == 0 and len(src.fetched) == 3
    clock.now = datetime(2026, 11, 21, 6, tzinfo=UTC)  # the next day
    nxt = job.run()
    assert nxt.days_imported == ["2026-11-20"] and nxt.days_skipped == 2
    assert src.fetched[-1] == date(2026, 11, 20)


def test_a_failed_day_is_retried_and_stops_the_run() -> None:
    job, src, done, _, _ = make(Clock("2026-11-20T06:00:00"), lookback_days=3)
    src.fail_on = {date(2026, 11, 18)}
    r = job.run()
    assert r.error == "vendor said no" and r.days_imported == ["2026-11-17"]
    assert date(2026, 11, 19) not in src.fetched  # the rest waits for the next run
    assert len(done.items) == 1
    src.fail_on = set()
    r = job.run()
    assert r.error is None and r.days_imported == ["2026-11-18", "2026-11-19"]


def test_unqueueable_evidence_means_the_day_is_not_marked_done() -> None:
    job, _, done, _, h = make(Clock("2026-11-20T06:00:00"), lookback_days=1)
    h.evidence_available = False
    r = job.run()
    assert r.error and "could not be queued" in r.error and not done.items
    h.evidence_available = True
    assert job.run().error is None and len(done.items) == 1


def test_repeated_failures_raise_one_alert_and_success_clears_it() -> None:
    job, src, _, alerts, _ = make(Clock("2026-11-20T06:00:00"), lookback_days=1, alert_after=3)
    src.always_fail = True
    for _ in range(5):
        job.run()
    assert [a[0] for a in alerts] == ["import.failed"]  # once, not on every failure
    src.always_fail = False
    job.run()
    src.always_fail = True
    job2_clock = job._clock  # noqa: SLF001
    assert isinstance(job2_clock, Clock)
    job2_clock.now = datetime(2026, 11, 21, 6, tzinfo=UTC)  # a new day to fail on
    for _ in range(3):
        job.run()
    assert len(alerts) == 2  # a fresh streak alerts again


# --------------------------------------------------------------- configuration


def test_config_builds_jobs_and_reports_bad_entries_without_stopping_the_rest(
    tmp_path: Path,
) -> None:
    amap = tmp_path / "actors.json"
    amap.write_text(json.dumps({"a@example.com": "alice"}))
    env = {
        "CAPTAIN_IMPORTS": json.dumps(
            [
                {"source": "claude-code", "every_hours": 12, "lookback_days": 2},
                {"source": "copilot", "org": "acme"},
                {"source": "copilot"},  # no org
                {"source": "unknown"},
                "nonsense",
            ]
        ),
        "ANTHROPIC_ADMIN_KEY": "sk-ant-admin01-x",
        "GITHUB_TOKEN": "ghp_x",
        "CAPTAIN_ACTOR_MAP_FILE": str(amap),
    }
    h = CaptainCoreHarness()
    jobs, errors = jobs_from_env(env, h.core, InMemoryDone(), lambda *_: None)
    assert [j.name for j in jobs] == ["claude-code", "copilot:acme"]
    assert jobs[0].every_s == 12 * 3600 and jobs[1].every_s == 6 * 3600
    assert (
        len(errors) == 3
        and any("org" in e for e in errors)
        and any("unknown source" in e for e in errors)
    )


def test_config_without_credentials_or_with_bad_json_is_reported() -> None:
    h = CaptainCoreHarness()
    jobs, errors = jobs_from_env(
        {"CAPTAIN_IMPORTS": '[{"source": "claude-code"}]'}, h.core, InMemoryDone(), lambda *_: None
    )
    assert jobs == [] and "ANTHROPIC_ADMIN_KEY" in errors[0]
    assert "ADMIN_KEY" in errors[0] and "sk-" not in errors[0]
    _, errors = jobs_from_env(
        {"CAPTAIN_IMPORTS": "{not json"}, h.core, InMemoryDone(), lambda *_: None
    )
    assert "not a JSON list" in errors[0]
    assert jobs_from_env({}, h.core, InMemoryDone(), lambda *_: None) == ([], [])


# ---------------------------------------------------------------------- worker


def test_worker_runs_imports_in_the_background_and_never_twice_at_once() -> None:
    h = CaptainCoreHarness()
    gate, started, runs = threading.Event(), threading.Event(), []

    def slow() -> None:
        runs.append(1)
        started.set()
        gate.wait(5)

    w = CoreWorker(
        h.built,
        LocalLease("imports-bg"),
        lambda t: (),
        extra_jobs=[Job("import:slow", 1.0, slow, background=True)],
    )
    t0 = time.monotonic()
    assert "import:slow" in w.tick(now=0)
    assert started.wait(2)
    assert time.monotonic() - t0 < 1  # the tick did not wait for the slow job
    assert "import:slow" not in w.tick(now=5)  # still running: not started again
    assert "ledger" in w.tick(now=6)  # other jobs keep going meanwhile
    gate.set()
    w.join_background()
    assert "import:slow" in w.tick(now=10)  # due again once the first run finished
    w.join_background()
    assert len(runs) == 2
    w.lease.release()


def test_a_crashing_background_job_does_not_stop_the_worker() -> None:
    h = CaptainCoreHarness()

    def boom() -> None:
        raise RuntimeError("vendor SDK exploded")

    w = CoreWorker(
        h.built,
        LocalLease("imports-crash"),
        lambda t: (),
        extra_jobs=[Job("import:boom", 1.0, boom, background=True)],
    )
    w.tick(now=0)
    w.join_background()
    assert "ledger" in w.tick(now=2)
    w.lease.release()


@pytest.mark.parametrize("hours", [0.5, 24])
def test_job_interval_comes_from_configuration(hours: float) -> None:
    h = CaptainCoreHarness()
    env = {
        "CAPTAIN_IMPORTS": json.dumps([{"source": "copilot", "org": "o", "every_hours": hours}]),
        "GITHUB_TOKEN": "t",
    }
    (job,), _ = jobs_from_env(env, h.core, InMemoryDone(), lambda *_: None)
    assert job.every_s == hours * 3600
