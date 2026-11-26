"""Run the Captain Core worker:  python -m captain.workers   (configuration from env)."""

from __future__ import annotations

import logging
import signal
from typing import Any

from sqlalchemy import create_engine

from captain.api.state import PgStateStore, StateStore
from captain.config import build_from_env, escalation_approvers_from_env
from captain.core.alerts import Alert, Severity
from captain.db.lease import PgAdvisoryLease, lease_name
from captain.envfile import current_env
from captain.integrations.telemetry.schedule import jobs_from_env
from captain.workers.runner import CoreWorker, Job

log = logging.getLogger("captain.worker")
OWNER = "worker"


class StateDone:
    """Remembers imported days in the shared state store (kept for just over a year)."""

    def __init__(self, store: StateStore) -> None:
        self._s = store

    def is_done(self, key: str) -> bool:
        return self._s.get("import_done", key, OWNER) is not None

    def mark(self, key: str, info: dict[str, Any]) -> None:
        self._s.put("import_done", key, OWNER, info)


def build_worker() -> CoreWorker:
    built, settings = build_from_env()

    def notify(kind: str, title: str, details: dict[str, str | int | float | bool | None]) -> None:
        built.alert_service.raise_alert(Alert(kind, Severity.WARNING, title, details))

    # Real environment wins over backend/.env (development convenience only).
    env = current_env()
    done = StateDone(PgStateStore(create_engine(settings.database_url), ttl_s=400 * 24 * 3600))
    imports, errors = jobs_from_env(env, built.core, done, notify)
    for e in errors:
        log.error("scheduled import not started: %s", e)
    extra = [Job(f"import:{j.name}", j.every_s, j.run, background=True) for j in imports]
    if extra:
        log.info("scheduled imports: %s", ", ".join(j.name for j in extra))

    worker = CoreWorker(
        built,
        PgAdvisoryLease(settings.database_url, lease_name(settings.database_url)),
        escalation_approvers_from_env(),
        extra_jobs=extra,
    )
    return worker


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if current_env().get("CAPTAIN_MULTI_TENANT") == "1":
        # Shared Captain: one worker per company, each with its own ledger lease.
        import threading

        from captain.tenancy import run_all_workers

        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        run_all_workers(build_worker, stop)
        return
    worker = build_worker()
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    signal.signal(signal.SIGINT, lambda *_: worker.stop())
    worker.run_forever()


if __name__ == "__main__":
    main()
