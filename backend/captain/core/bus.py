"""Internal event bus for cross-instance propagation (revocation, kill switch, register
changes). In-memory for one process; PostgreSQL LISTEN/NOTIFY (captain.db.bus) for many."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

log = logging.getLogger("captain.bus")


class Topic(StrEnum):
    KEY_REVOKED = "captain.key.revoked"
    KILL_SWITCH = "captain.killswitch"
    REGISTER_CHANGED = "captain.register.changed"
    POLICY_PUBLISHED = "captain.policy.published"


Handler = Callable[[str], None]


class EventBus(Protocol):
    def publish(self, topic: Topic, payload: str) -> None: ...
    def subscribe(self, topic: Topic, handler: Handler) -> None: ...


class InMemoryBus:
    def __init__(self) -> None:
        self._subs: dict[Topic, list[Handler]] = defaultdict(list)
        self._lock = threading.Lock()

    def publish(self, topic: Topic, payload: str) -> None:
        with self._lock:
            handlers = list(self._subs[topic])
        for h in handlers:
            try:
                h(payload)
            except Exception:  # one faulty subscriber must not block revocation for others
                log.exception("event bus handler failed for %s", topic)

    def subscribe(self, topic: Topic, handler: Handler) -> None:
        with self._lock:
            self._subs[topic].append(handler)
