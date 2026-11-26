from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from captain.core.decision_api import EvidenceUnavailableError
from captain.core.ledger.chain import Ed25519Signer, KeyRingVerifier, merkle_root, seal
from captain.core.ledger.service import (
    DirectoryWormStore,
    InMemoryLedgerStore,
    InMemoryWormStore,
    LedgerService,
)
from captain.core.models import Channel, CoverageClass, EventType, UsageEvent


def ev(register: str | None = "AI-1", n: int = 0) -> UsageEvent:
    return UsageEvent(
        trace_id=f"t{n}", event_type=EventType.CALL_ALLOWED, channel=Channel.API,
        coverage=CoverageClass.GOVERNED, principal_id="u1", register_id=register,
    )  # fmt: skip


@pytest.fixture
def parts() -> tuple[LedgerService, InMemoryLedgerStore, InMemoryWormStore, Ed25519Signer]:
    store, worm, signer = InMemoryLedgerStore(), InMemoryWormStore(), Ed25519Signer("k1")
    svc = LedgerService(store, worm, signer, KeyRingVerifier({"k1": signer.public_key()}))
    for i in range(5):
        svc.enqueue(ev("AI-1", i))
    for i in range(3):
        svc.enqueue(ev("AI-2", i))
    svc.write_pending()
    return svc, store, worm, signer


def test_chain_verifies(parts) -> None:  # type: ignore[no-untyped-def]
    svc, store, *_ = parts
    assert [e.seq for e in store.entries("AI-1")] == [1, 2, 3, 4, 5]
    rep = svc.verify()
    assert rep.ok and rep.entries_checked == 8
    assert not store.outbox


def test_entries_continue_chain_across_batches(parts) -> None:  # type: ignore[no-untyped-def]
    svc, store, *_ = parts
    svc.enqueue(ev("AI-1", 9))
    svc.write_pending()
    e5, e6 = store.entries("AI-1")[-2:]
    assert e6.seq == 6 and e6.prev_hash == e5.entry_hash
    assert svc.verify().ok


def test_modification_detected_at_right_entry(parts) -> None:  # type: ignore[no-untyped-def]
    svc, store, *_ = parts
    i = next(i for i, e in enumerate(store.rows) if e.partition == "AI-1" and e.seq == 3)
    store.rows[i] = replace(store.rows[i], body={**store.rows[i].body, "principal_id": "attacker"})
    rep = svc.verify()
    assert not rep.ok and rep.first_problem and rep.first_problem.startswith("AI-1#3")


def test_deletion_detected(parts) -> None:  # type: ignore[no-untyped-def]
    svc, store, *_ = parts
    store.rows = [e for e in store.rows if not (e.partition == "AI-1" and e.seq == 2)]
    assert not svc.verify().ok


def test_reorder_detected(parts) -> None:  # type: ignore[no-untyped-def]
    svc, store, *_ = parts
    a1 = [e for e in store.rows if e.partition == "AI-1"]
    swapped = [replace(a1[1], seq=1), replace(a1[0], seq=2), *a1[2:]]
    store.rows = swapped + [e for e in store.rows if e.partition != "AI-1"]
    assert not svc.verify().ok


def test_resealed_chain_without_key_is_detected(parts) -> None:  # type: ignore[no-untyped-def]
    """DB admin rewrites an entry AND recomputes all later hashes, but has no signing key."""
    svc, store, *_ = parts
    forger = Ed25519Signer("k1")  # same key id, different (attacker) key
    rows = sorted(store.entries("AI-1"), key=lambda e: e.seq)
    prev, forged = "0" * 64, []
    for e in rows:
        body = {**e.body, "principal_id": "attacker"} if e.seq == 2 else e.body
        n = seal(e.partition, e.seq, e.event_id, e.event_type, e.recorded_at, body, prev, forger)
        forged.append(n)
        prev = n.entry_hash
    store.rows = forged + [e for e in store.rows if e.partition != "AI-1"]
    rep = svc.verify()
    assert not rep.ok and any("invalid signature" in p for p in rep.problems)


def test_anchor_detects_rewrite_even_with_signing_key(parts) -> None:  # type: ignore[no-untyped-def]
    svc, store, worm, signer = parts
    a = svc.anchor()
    assert a is not None and a.ranges == {"AI-1": (1, 5), "AI-2": (1, 3)}
    assert all(e.anchor_id == a.anchor_id for e in store.rows)
    assert svc.anchor() is None  # nothing new
    rows = sorted(store.entries("AI-1"), key=lambda e: e.seq)  # key holder re-seals history
    prev, forged = "0" * 64, []
    for e in rows:
        body = {**e.body, "principal_id": "x"} if e.seq == 1 else e.body
        n = seal(e.partition, e.seq, e.event_id, e.event_type, e.recorded_at, body, prev, signer)
        forged.append(replace(n, anchor_id=e.anchor_id))
        prev = n.entry_hash
    store.rows = forged + [e for e in store.rows if e.partition != "AI-1"]
    rep = svc.verify()
    assert not rep.ok and any("Merkle root mismatch" in p for p in rep.problems)


def test_worm_is_write_once(parts) -> None:  # type: ignore[no-untyped-def]
    svc, _, worm, _ = parts
    a = svc.anchor()
    assert a is not None
    with pytest.raises(PermissionError):
        worm.put_once(f"{a.anchor_id}.json", b"{}")


def test_directory_worm(tmp_path: Path) -> None:
    w = DirectoryWormStore(tmp_path / "anchors")
    w.put_once("a.json", b"1")
    with pytest.raises(FileExistsError):
        w.put_once("a.json", b"2")
    assert w.get("a.json") == b"1"


def test_outbox_failure_raises_evidence_unavailable() -> None:
    store = InMemoryLedgerStore()
    store.available = False
    s = Ed25519Signer("k")
    svc = LedgerService(store, InMemoryWormStore(), s, KeyRingVerifier({"k": s.public_key()}))
    with pytest.raises(EvidenceUnavailableError):
        svc.enqueue(ev())


def test_unattributed_partition_and_no_content() -> None:
    store = InMemoryLedgerStore()
    s = Ed25519Signer("k")
    svc = LedgerService(store, InMemoryWormStore(), s, KeyRingVerifier({"k": s.public_key()}))
    assert svc.enqueue(ev(register=None)).partition == "unattributed"


def test_merkle() -> None:
    a, b, c = "aa" * 32, "bb" * 32, "cc" * 32
    assert merkle_root([a, b, c]) != merkle_root([a, c, b])
    assert merkle_root([a]) != a  # leaves are domain-separated


def test_hash_is_timezone_independent(parts) -> None:  # type: ignore[no-untyped-def]
    """Regression: PostgreSQL returns timestamps in the session timezone (+05:30 here);
    the same instant must produce the same hash."""
    from datetime import timedelta, timezone

    svc, store, *_ = parts
    ist = timezone(timedelta(hours=5, minutes=30))
    store.rows = [replace(e, recorded_at=e.recorded_at.astimezone(ist)) for e in store.rows]
    assert svc.verify().ok
