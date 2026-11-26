"""Kill switch: stop a user, key, application, project, register entry, model, tool,
MCP server, channel, or the whole organisation.

State is persisted by a store and propagated to every Captain instance through the
event bus (Valkey pub/sub in production), target ≤ 5 s at p99. The local cache TTL
is the backstop if an event is missed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from captain.core.bus import EventBus, Topic


class KillScope(StrEnum):
    ORG = "org"
    PRINCIPAL = "principal"
    KEY = "key"
    APPLICATION = "application"
    PROJECT = "project"
    REGISTER_ENTRY = "register"
    MODEL = "model"
    TOOL = "tool"
    MCP_SERVER = "mcp_server"
    CHANNEL = "channel"


@dataclass(frozen=True, slots=True)
class KillSwitchEntry:
    scope: KillScope
    target: str
    reason: str
    actor_id: str
    activated_at: datetime


class KillSwitchStore(Protocol):
    def activate(self, entry: KillSwitchEntry) -> None: ...
    def deactivate(self, scope: KillScope, target: str) -> None: ...
    def active(self) -> list[KillSwitchEntry]: ...


class InMemoryKillSwitchStore:
    def __init__(self) -> None:
        self._entries: dict[tuple[KillScope, str], KillSwitchEntry] = {}
        self._lock = threading.Lock()

    def activate(self, entry: KillSwitchEntry) -> None:
        with self._lock:
            self._entries[(entry.scope, entry.target)] = entry

    def deactivate(self, scope: KillScope, target: str) -> None:
        with self._lock:
            self._entries.pop((scope, target), None)

    def active(self) -> list[KillSwitchEntry]:
        with self._lock:
            return list(self._entries.values())


class KillSwitch:
    """Active entries are cached in memory so a decision does not hit the database.
    A change publishes on the event bus and every instance drops its cache at once;
    `cache_ttl_s` (default 5 s, the CTO propagation target) is the backstop if a bus
    message is missed."""

    def __init__(
        self, store: KillSwitchStore, bus: EventBus | None = None, cache_ttl_s: float = 5.0
    ) -> None:
        self._store = store
        self._bus = bus
        self._ttl = cache_ttl_s
        self._cache: tuple[float, list[KillSwitchEntry]] | None = None
        self._lock = threading.Lock()
        if bus is not None:
            bus.subscribe(Topic.KILL_SWITCH, lambda _: self.invalidate())

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def _changed(self, scope: KillScope, target: str) -> None:
        self.invalidate()
        if self._bus is not None:
            self._bus.publish(Topic.KILL_SWITCH, f"{scope.value}:{target}")

    def activate(
        self, scope: KillScope, target: str, reason: str, actor_id: str
    ) -> KillSwitchEntry:
        entry = KillSwitchEntry(scope, target, reason, actor_id, datetime.now(UTC))
        self._store.activate(entry)
        self._changed(scope, target)
        return entry

    def deactivate(self, scope: KillScope, target: str) -> None:
        self._store.deactivate(scope, target)
        self._changed(scope, target)

    def _active(self) -> list[KillSwitchEntry]:
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and now - self._cache[0] < self._ttl:
                return self._cache[1]
        entries = self._store.active()
        with self._lock:
            self._cache = (now, entries)
        return entries

    def match(self, targets: dict[KillScope, str | None]) -> KillSwitchEntry | None:
        """Return the first active entry matching any of the request's attributes."""
        for e in self._active():
            if targets.get(e.scope) == e.target:
                return e
        return None
