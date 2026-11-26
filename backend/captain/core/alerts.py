"""Alerts: saved, recorded in the evidence ledger, and delivered.

Delivery never blocks or fails a governed request: a notifier error is caught, the
alert is kept with `delivered=False`, and the worker retries later.
"""

from __future__ import annotations

import logging
import smtplib
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.message import EmailMessage
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from captain.core.engine import admin_event
from captain.core.models import EventType, UsageEvent

log = logging.getLogger("captain.alerts")


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class Alert:
    kind: str  # budget.threshold | budget.override | register.downgrade | approval.escalated …
    severity: Severity
    subject: str
    details: dict[str, str | int | float | bool | None]
    register_id: str | None = None
    recipients: tuple[str, ...] = ()
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    delivered: bool = False
    attempts: int = 0


class AlertStore(Protocol):
    def put(self, a: Alert) -> None: ...
    def undelivered(self, limit: int) -> list[Alert]: ...
    def mark(self, alert_id: UUID, delivered: bool, attempts: int) -> None: ...


class InMemoryAlertStore:
    def __init__(self) -> None:
        self.items: dict[UUID, Alert] = {}
        self._lock = threading.Lock()

    def put(self, a: Alert) -> None:
        with self._lock:
            self.items[a.id] = a

    def undelivered(self, limit: int) -> list[Alert]:
        return [a for a in self.items.values() if not a.delivered][:limit]

    def mark(self, alert_id: UUID, delivered: bool, attempts: int) -> None:
        with self._lock:
            self.items[alert_id] = replace(
                self.items[alert_id], delivered=delivered, attempts=attempts
            )


class Notifier(Protocol):
    def send(self, a: Alert) -> None: ...


class LogNotifier:
    def send(self, a: Alert) -> None:
        log.warning("ALERT %s %s: %s %s", a.severity, a.kind, a.subject, a.details)


class EmailNotifier:
    """SMTP delivery (STARTTLS). Credentials come from the client's secret store, never
    from code. `smtp_factory` is injectable for tests."""

    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        username: str | None = None,
        password: str | None = None,
        smtp_factory: Callable[[str, int], smtplib.SMTP] = smtplib.SMTP,
    ) -> None:
        self._host, self._port, self._sender = host, port, sender
        self._user, self._pw = username, password
        self._factory = smtp_factory

    def send(self, a: Alert) -> None:
        if not a.recipients:
            return
        msg = EmailMessage()
        msg["From"] = self._sender
        msg["To"] = ", ".join(a.recipients)
        msg["Subject"] = f"[Captain {a.severity}] {a.subject}"
        body = "\n".join(f"{k}: {v}" for k, v in sorted(a.details.items()))
        msg.set_content(f"{a.subject}\n\nType: {a.kind}\n{body}\n\nAlert id: {a.id}\n")
        with self._factory(self._host, self._port) as s:
            s.starttls()
            if self._user and self._pw:
                s.login(self._user, self._pw)
            s.send_message(msg)


RecipientResolver = Callable[[Alert], Sequence[str]]


class AlertService:
    def __init__(
        self,
        store: AlertStore,
        notifiers: Sequence[Notifier],
        enqueue_evidence: Callable[[UsageEvent], object],
        recipients: RecipientResolver = lambda a: (),
        max_attempts: int = 5,
    ) -> None:
        self._store = store
        self._notifiers = notifiers
        self._enqueue = enqueue_evidence
        self._recipients = recipients
        self._max = max_attempts

    def raise_alert(self, a: Alert) -> Alert:
        a = replace(a, recipients=a.recipients or tuple(self._recipients(a)))
        self._store.put(a)
        try:
            self._enqueue(
                admin_event(
                    EventType.ALERT_RAISED, "captain",
                    {"alert_id": str(a.id), "kind": a.kind, "severity": a.severity.value,
                     "subject": a.subject, **a.details},
                    a.register_id,
                )
            )  # fmt: skip
        except Exception:  # evidence is best-effort for alerts; the alert itself is kept
            log.exception("alert %s could not be recorded in the ledger", a.id)
        self._deliver(a)
        return a

    def _deliver(self, a: Alert) -> bool:
        try:
            for n in self._notifiers:
                n.send(a)
        except Exception:
            log.exception("alert %s delivery failed (attempt %d)", a.id, a.attempts + 1)
            self._store.mark(a.id, False, a.attempts + 1)
            return False
        self._store.mark(a.id, True, a.attempts + 1)
        return True

    def retry_undelivered(self, limit: int = 100) -> int:
        """Called by the worker. Gives up after max_attempts (alert stays visible)."""
        n = 0
        for a in self._store.undelivered(limit):
            if a.attempts < self._max and self._deliver(a):
                n += 1
        return n
