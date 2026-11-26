"""Evidence Ledger service: outbox → ordered writer → anchors → verify.

Write path (CTO F4):
1. `enqueue(event)` durably stores the event in the outbox (PostgreSQL in production,
   same transaction as the usage record). Failure raises EvidenceUnavailableError.
2. `write_pending()` (the single writer per partition) seals entries in order:
   entry_hash = SHA-256(prev_hash ‖ RFC 8785 JSON), signed.
3. `anchor()` (hourly) writes a Merkle root over all new entries to WORM storage.
4. `verify()` recomputes everything and reports the first broken entry.
"""

from __future__ import annotations

import base64
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from captain.core.decision_api import EvidenceUnavailableError
from captain.core.ledger.chain import (
    GENESIS,
    Anchor,
    LedgerEntry,
    Signer,
    Verifier,
    canonical,
    compute_entry_hash,
    merkle_root,
    seal,
)
from captain.core.models import LedgerReceipt, UsageEvent


class LedgerStore(Protocol):
    # outbox
    def outbox_put(
        self, event_id: str, partition: str, body: dict[str, Any], event_type: str
    ) -> None: ...
    def outbox_take(self, limit: int) -> list[tuple[str, str, str, dict[str, Any]]]: ...
    def outbox_done(self, event_ids: list[str]) -> None: ...
    def written_event_ids(self, event_ids: list[str]) -> set[str]: ...
    # ledger (append-only)
    def append(self, entries: list[LedgerEntry]) -> None: ...
    def last(self, partition: str) -> LedgerEntry | None: ...
    def entries(self, partition: str | None = None) -> list[LedgerEntry]: ...
    def unanchored(self) -> list[LedgerEntry]: ...
    def mark_anchored(self, anchor: Anchor) -> None: ...


class WormStore(Protocol):
    """Write-once storage for anchors: S3 Object Lock (compliance mode) / Azure
    immutable blob in production; a write-once directory for development."""

    def put_once(self, name: str, data: bytes) -> None: ...
    def get(self, name: str) -> bytes: ...
    def names(self) -> list[str]: ...


class InMemoryLedgerStore:
    def __init__(self) -> None:
        self.outbox: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self.rows: list[LedgerEntry] = []
        self.available = True
        self._lock = threading.Lock()

    def outbox_put(
        self, event_id: str, partition: str, body: dict[str, Any], event_type: str
    ) -> None:
        if not self.available:
            raise ConnectionError("outbox unavailable")
        with self._lock:
            self.outbox[event_id] = (partition, event_type, body)

    def outbox_take(self, limit: int) -> list[tuple[str, str, str, dict[str, Any]]]:
        with self._lock:
            items = list(self.outbox.items())[:limit]
        return [(eid, p, t, b) for eid, (p, t, b) in items]

    def outbox_done(self, event_ids: list[str]) -> None:
        with self._lock:
            for e in event_ids:
                self.outbox.pop(e, None)

    def written_event_ids(self, event_ids: list[str]) -> set[str]:
        wanted = set(event_ids)
        return {e.event_id for e in self.rows if e.event_id in wanted}

    def append(self, entries: list[LedgerEntry]) -> None:
        with self._lock:
            self.rows.extend(entries)

    def last(self, partition: str) -> LedgerEntry | None:
        for e in reversed(self.rows):
            if e.partition == partition:
                return e
        return None

    def entries(self, partition: str | None = None) -> list[LedgerEntry]:
        return [e for e in self.rows if partition is None or e.partition == partition]

    def unanchored(self) -> list[LedgerEntry]:
        return [e for e in self.rows if e.anchor_id is None]

    def mark_anchored(self, anchor: Anchor) -> None:
        from dataclasses import replace

        with self._lock:
            self.rows = [
                replace(e, anchor_id=anchor.anchor_id)
                if e.anchor_id is None
                and e.partition in anchor.ranges
                and anchor.ranges[e.partition][0] <= e.seq <= anchor.ranges[e.partition][1]
                else e
                for e in self.rows
            ]


class InMemoryWormStore:
    def __init__(self) -> None:
        self._d: dict[str, bytes] = {}

    def put_once(self, name: str, data: bytes) -> None:
        if name in self._d:
            raise PermissionError(f"{name} already exists (write-once)")
        self._d[name] = data

    def get(self, name: str) -> bytes:
        return self._d[name]

    def names(self) -> list[str]:
        return sorted(self._d)


class DirectoryWormStore:
    """Development only: refuses overwrite and sets files read-only. This is NOT real
    immutability — an OS admin can still change files. Use S3 Object Lock in production."""

    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def put_once(self, name: str, data: bytes) -> None:
        p = self._root / name
        with p.open("xb") as f:  # fails if exists
            f.write(data)
        p.chmod(0o444)

    def get(self, name: str) -> bytes:
        return (self._root / name).read_bytes()

    def names(self) -> list[str]:
        return sorted(p.name for p in self._root.iterdir())


@dataclass
class VerifyReport:
    ok: bool
    entries_checked: int = 0
    anchors_checked: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def first_problem(self) -> str | None:
        return self.problems[0] if self.problems else None


class LedgerService:
    def __init__(
        self,
        store: LedgerStore,
        worm: WormStore,
        signer: Signer,
        verifier: Verifier,
        pseudonymise: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self._store = store
        # Erasure (ADR-005): personal identifiers are replaced before an event is queued,
        # so they never reach the signed, append-only ledger in the clear.
        self._pseudonymise = pseudonymise
        self._worm = worm
        self._signer = signer
        self._verifier = verifier
        self._write_lock = (
            threading.Lock()
        )  # single writer (per process; per partition lease in prod)

    # ------------------------------------------------------------------ write
    def enqueue(self, event: UsageEvent) -> LedgerReceipt:
        partition = event.register_id or "unattributed"
        body = event.model_dump(mode="json")
        try:
            if self._pseudonymise is not None:
                body = self._pseudonymise(body)
            self._store.outbox_put(str(event.event_id), partition, body, event.event_type.value)
        except Exception as e:
            raise EvidenceUnavailableError(f"evidence could not be queued: {e}") from e
        return LedgerReceipt(
            event_id=event.event_id, partition=partition, accepted_at=datetime.now(UTC)
        )

    def write_pending(self, batch: int = 500) -> int:
        with self._write_lock:
            items = self._store.outbox_take(batch)
            # Crash recovery: if a previous run appended entries but died before
            # clearing the outbox, those events are already sealed. Clear them and
            # never seal them twice (that would duplicate evidence / break the chain).
            done = self._store.written_event_ids([i[0] for i in items])
            if done:
                self._store.outbox_done(sorted(done))
                items = [i for i in items if i[0] not in done]
            heads: dict[str, tuple[int, str]] = {}
            sealed: list[LedgerEntry] = []
            for event_id, partition, event_type, body in items:
                if partition not in heads:
                    last = self._store.last(partition)
                    heads[partition] = (last.seq, last.entry_hash) if last else (0, GENESIS)
                seq, prev = heads[partition]
                e = seal(
                    partition,
                    seq + 1,
                    event_id,
                    event_type,
                    datetime.now(UTC),
                    body,
                    prev,
                    self._signer,
                )
                heads[partition] = (e.seq, e.entry_hash)
                sealed.append(e)
            if sealed:
                self._store.append(sealed)
                self._store.outbox_done([e.event_id for e in sealed])
            return len(sealed)

    def oldest_pending_age_s(self) -> float:
        """Seconds since the oldest event still waiting in the outbox occurred (0 if none)."""
        items = self._store.outbox_take(1)
        if not items:
            return 0.0
        occurred = datetime.fromisoformat(str(items[0][3]["occurred_at"]))
        return max((datetime.now(UTC) - occurred).total_seconds(), 0.0)

    def anchor(self) -> Anchor | None:
        with self._write_lock:
            pending = sorted(self._store.unanchored(), key=lambda e: (e.partition, e.seq))
            if not pending:
                return None
            ranges: dict[str, tuple[int, int]] = {}
            for e in pending:
                lo, _ = ranges.get(e.partition, (e.seq, e.seq))
                ranges[e.partition] = (lo, e.seq)
            root = merkle_root([e.entry_hash for e in pending])
            now = datetime.now(UTC)
            anchor_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            doc = {
                "anchor_id": anchor_id,
                "created_at": now.isoformat(),
                "ranges": {p: list(r) for p, r in sorted(ranges.items())},
                "root": root,
            }
            sig = base64.b64encode(self._signer.sign(canonical(doc))).decode()
            a = Anchor(anchor_id, now, ranges, root, sig, self._signer.key_id)
            self._worm.put_once(
                f"{anchor_id}.json",
                json.dumps({**doc, "signature": sig, "key_id": a.key_id}).encode(),
            )
            self._store.mark_anchored(a)
            return a

    # ----------------------------------------------------------------- verify
    def verify(self) -> VerifyReport:
        rep = VerifyReport(ok=True)
        by_part: dict[str, list[LedgerEntry]] = {}
        for e in self._store.entries():
            by_part.setdefault(e.partition, []).append(e)
        for part, rows in by_part.items():
            rows.sort(key=lambda e: e.seq)
            prev, expected_seq = GENESIS, 1
            for e in rows:
                rep.entries_checked += 1
                where = f"{part}#{e.seq}"
                if e.seq != expected_seq:
                    rep.problems.append(
                        f"{part}: expected seq {expected_seq}, found {e.seq} (missing or reordered)"
                    )
                if e.prev_hash != prev:
                    rep.problems.append(f"{where}: prev_hash does not match previous entry")
                if compute_entry_hash(e.prev_hash, e.hashed_part()) != e.entry_hash:
                    rep.problems.append(f"{where}: content does not match entry_hash (modified)")
                if not self._verifier.verify(
                    e.key_id, bytes.fromhex(e.entry_hash), base64.b64decode(e.signature)
                ):
                    rep.problems.append(f"{where}: invalid signature")
                prev, expected_seq = e.entry_hash, e.seq + 1
        self._verify_anchors(by_part, rep)
        rep.ok = not rep.problems
        return rep

    def _verify_anchors(self, by_part: dict[str, list[LedgerEntry]], rep: VerifyReport) -> None:
        index = {(e.partition, e.seq): e for rows in by_part.values() for e in rows}
        for name in self._worm.names():
            rep.anchors_checked += 1
            raw = json.loads(self._worm.get(name))
            doc = {k: raw[k] for k in ("anchor_id", "created_at", "ranges", "root")}
            if not self._verifier.verify(
                raw["key_id"], canonical(doc), base64.b64decode(raw["signature"])
            ):
                rep.problems.append(f"anchor {raw['anchor_id']}: invalid signature")
                continue
            leaves: list[str] = []
            for part, (lo, hi) in sorted(raw["ranges"].items()):
                for s in range(lo, hi + 1):
                    e = index.get((part, s))
                    if e is None:
                        rep.problems.append(f"anchor {raw['anchor_id']}: entry {part}#{s} missing")
                        break
                    leaves.append(e.entry_hash)
            if merkle_root(leaves) != raw["root"]:
                rep.problems.append(
                    f"anchor {raw['anchor_id']}: Merkle root mismatch (anchored entries changed)"
                )
