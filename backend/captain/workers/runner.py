"""Captain Core background worker.

Jobs (each runs only while this process holds the lease):
| Job                 | Default interval | Why                                              |
|---------------------|------------------|--------------------------------------------------|
| ledger writer       | 0.5 s            | outbox → hash-chained, signed ledger entries     |
| ledger lag check    | 15 s             | alert if the oldest queued event is > 60 s old   |
| anchor              | 1 h              | Merkle root to WORM storage (CTO F4: hourly)     |
| reservation sweep   | 60 s             | release budget held by crashed calls             |
| approval escalation | 60 s             | add backup approvers to overdue approvals        |
| alert retry         | 60 s             | re-send alerts whose delivery failed             |
| vendor imports      | 6 h (per job)    | OBSERVED usage from vendors (CAPTAIN_IMPORTS)    |

Single writer: the hash chain needs entries written in order, so exactly one worker
holds the lease (a PostgreSQL advisory lock). Other replicas wait as hot standbys and
take over within one lease-check interval if the holder dies.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Protocol

from captain.core.alerts import Alert, Severity
from captain.core.factory import BuiltCore
from captain.core.models import ApprovalTicket

log = logging.getLogger("captain.worker")


class Lease(Protocol):
    def acquire(self) -> bool: ...
    def release(self) -> None: ...


class LocalLease:
    """Single-process lease (tests / one-instance deployments)."""

    _held: set[str] = set()
    _lock = threading.Lock()

    def __init__(self, name: str) -> None:
        self.name, self._mine = name, False

    def acquire(self) -> bool:
        with LocalLease._lock:
            if self._mine:
                return True
            if self.name in LocalLease._held:
                return False
            LocalLease._held.add(self.name)
            self._mine = True
            return True

    def release(self) -> None:
        with LocalLease._lock:
            if self._mine:
                LocalLease._held.discard(self.name)
                self._mine = False


@dataclass
class Job:
    name: str
    every_s: float
    run: Callable[[], object]
    last: float = field(default=float("-inf"))
    # Slow jobs (calls to other companies' APIs) run on their own thread so they can
    # never hold up the ledger writer; a job that is still running is not started twice.
    background: bool = False
    thread: threading.Thread | None = None


@dataclass
class WorkerConfig:
    ledger_every_s: float = 0.5
    lag_every_s: float = 15
    lag_alert_after_s: float = 60
    anchor_every_s: float = 3600
    sweep_every_s: float = 60
    escalation_every_s: float = 60
    escalate_after: timedelta = timedelta(minutes=30)
    alert_retry_every_s: float = 60
    ip_key_prune_every_s: float = 3600
    ip_key_keep_days: int = 30  # client-IP hashes become unlinkable after this (ADR-005)


class CoreWorker:
    def __init__(
        self,
        built: BuiltCore,
        lease: Lease,
        escalation_approvers: Callable[[ApprovalTicket], tuple[str, ...]],
        cfg: WorkerConfig | None = None,
        extra_jobs: list[Job] | None = None,
    ) -> None:
        self.built = built
        self.lease = lease
        self.cfg = cfg or WorkerConfig()
        core = built.core.m
        c = self.cfg
        self.jobs = [
            Job("ledger", c.ledger_every_s, self.write_ledger),
            Job("ledger_lag", c.lag_every_s, self.check_lag),
            Job("anchor", c.anchor_every_s, core.ledger.anchor),
            Job("sweep", c.sweep_every_s, core.budget.sweep_expired),
            Job("escalation", c.escalation_every_s, lambda: self.escalate(escalation_approvers)),
            Job("alert_retry", c.alert_retry_every_s, built.alert_service.retry_undelivered),
            *(
                [
                    Job(
                        "ip_keys",
                        c.ip_key_prune_every_s,
                        partial(pz.prune_ip_days, c.ip_key_keep_days),
                    )
                ]
                if (pz := built.pseudonymiser) is not None
                else []
            ),
            *(extra_jobs or []),
        ]
        self._stop = threading.Event()
        self._lag_alerted = False

    # ------------------------------------------------------------------ jobs
    def write_ledger(self) -> int:
        n = total = self.built.core.m.ledger.write_pending()
        while n:  # drain backlog in batches
            n = self.built.core.m.ledger.write_pending()
            total += n
        return total

    def check_lag(self) -> float:
        lag = self.built.core.m.ledger.oldest_pending_age_s()
        if lag > self.cfg.lag_alert_after_s and not self._lag_alerted:
            self._lag_alerted = True
            self.built.alert_service.raise_alert(
                Alert(
                    "ledger.lag",
                    Severity.CRITICAL,
                    "Evidence ledger is falling behind",
                    {"oldest_pending_s": round(lag, 1)},
                )
            )
        elif lag <= self.cfg.lag_alert_after_s:
            self._lag_alerted = False
        return lag

    def escalate(self, approvers: Callable[[ApprovalTicket], tuple[str, ...]]) -> int:
        done = self.built.core.m.approvals.escalate_due(self.cfg.escalate_after, approvers)
        for t in done:
            self.built.alert_service.raise_alert(
                Alert(
                    "approval.escalated",
                    Severity.WARNING,
                    f"Approval overdue: {t.action_summary}",
                    {"ticket_id": str(t.ticket_id), "approvers": ",".join(t.assigned_approvers)},
                    register_id=t.register_id or None,
                    recipients=t.assigned_approvers,
                )
            )
        return len(done)

    # ------------------------------------------------------------------- loop
    def tick(self, now: float | None = None) -> list[str]:
        """Run every job that is due. Returns the names that ran (for tests/metrics)."""
        if not self.lease.acquire():
            return []
        now = time.monotonic() if now is None else now
        ran = []
        for j in self.jobs:
            if now - j.last >= j.every_s:
                if j.background:
                    if j.thread is not None and j.thread.is_alive():
                        continue  # still running; try again next interval
                    j.last = now
                    j.thread = threading.Thread(
                        target=self._guarded, args=(j,), name=f"job-{j.name}", daemon=True
                    )
                    j.thread.start()
                    ran.append(j.name)
                    continue
                j.last = now
                try:
                    j.run()
                    ran.append(j.name)
                except Exception:  # one failing job must not stop the others
                    log.exception("worker job %s failed", j.name)
        return ran

    @staticmethod
    def _guarded(j: Job) -> None:
        try:
            j.run()
        except Exception:
            log.exception("worker job %s failed", j.name)

    def join_background(self, timeout: float = 10) -> None:
        """Wait for background jobs (tests, shutdown)."""
        for j in self.jobs:
            if j.thread is not None:
                j.thread.join(timeout)

    def run_forever(self, poll_s: float = 0.25) -> None:
        log.info("captain worker started at %s", datetime.now(UTC).isoformat())
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(poll_s)
        finally:
            self.lease.release()

    def stop(self) -> None:
        self._stop.set()
