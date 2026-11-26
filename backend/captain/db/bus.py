"""Cross-instance event bus on PostgreSQL LISTEN/NOTIFY.

Used for key revocation, kill switch and register changes so every Captain instance
evicts caches immediately. NOTIFY is transactional and delivered to all listening
sessions; delivery is at-most-once per connected listener, so the 30 s cache TTL
remains the backstop if a listener was disconnected when an event fired.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict

import psycopg

from captain.core.bus import Handler, Topic

log = logging.getLogger("captain.bus")

_CHANNEL = "captain_events"
_SEP = "\x1f"


def plain_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def channel_for(sqlalchemy_url: str) -> str:
    """captain_events, or captain_events_<schema> for a company in its own schema."""
    m = re.search(r"search_path(?:=|%3D)([A-Za-z0-9_]+)", sqlalchemy_url)
    return f"{_CHANNEL}_{m.group(1).lower()}" if m else _CHANNEL


class PostgresBus:
    def __init__(self, dsn: str) -> None:
        self._dsn = plain_dsn(dsn)
        self._channel = channel_for(dsn)
        self._subs: dict[Topic, list[Handler]] = defaultdict(list)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._listen, name="captain-bus", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("event bus listener did not start")

    def publish(self, topic: Topic, payload: str) -> None:
        if _SEP in payload or len(payload) > 7000:  # NOTIFY payload limit is 8000 bytes
            raise ValueError("invalid bus payload")
        with psycopg.connect(self._dsn, autocommit=True) as c:
            c.execute("SELECT pg_notify(%s, %s)", (self._channel, f"{topic.value}{_SEP}{payload}"))

    def subscribe(self, topic: Topic, handler: Handler) -> None:
        with self._lock:
            self._subs[topic].append(handler)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _listen(self) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute(f"LISTEN {self._channel}")
            self._ready.set()
            while not self._stop.is_set():
                for n in conn.notifies(timeout=0.5, stop_after=50):
                    topic_s, _, payload = n.payload.partition(_SEP)
                    try:
                        topic = Topic(topic_s)
                    except ValueError:
                        continue
                    with self._lock:
                        handlers = list(self._subs[topic])
                    for h in handlers:
                        try:
                            h(payload)
                        except Exception:  # a faulty handler must not kill the listener
                            log.exception("event bus handler failed for %s", topic)
