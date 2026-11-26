"""Register sync: cautious reconciliation with the organisation's source of truth."""

from __future__ import annotations

import json
from typing import Any

import pytest

from captain.core.models import AutonomyLevel, RegisterEntry, RiskTier
from captain.register_sync import (
    SyncFileError,
    apply_plan,
    describe,
    load_entries,
    plan_sync,
)
from tests.unit.test_admin import setup


def entry(i: str, tier: RiskTier = RiskTier.TIER_3, **kw: Any) -> RegisterEntry:
    return RegisterEntry(
        id=i, name=i, risk_tier=tier, autonomy=kw.pop("autonomy", AutonomyLevel.A2),
        owner_id="o", cost_centre="CC", purpose="p", **kw,
    )  # fmt: skip


def test_plan_separates_create_update_weakening_unchanged_and_missing() -> None:
    cur = [entry("A"), entry("B"), entry("C", RiskTier.TIER_2), entry("GONE")]
    want = [
        entry("A"),
        entry("B").model_copy(update={"purpose": "new"}),
        entry("C", RiskTier.TIER_3),  # lower tier = weaker controls
        entry("NEW"),
    ]
    p = plan_sync(cur, want)
    assert [e.id for e in p.create] == ["NEW"] and [e.id for e in p.update] == ["B"]
    assert [e.id for e in p.weakening] == ["C"] and p.unchanged == ["A"]
    assert p.missing_from_file == ["GONE"]
    assert "needs --approval-ref" in describe(p)


def test_version_differences_alone_are_not_changes() -> None:
    assert plan_sync([entry("A").model_copy(update={"version": 7})], [entry("A")]).unchanged == [
        "A"
    ]


def test_apply_skips_weakening_without_approval_and_applies_with_it() -> None:
    h, admin, store = setup()
    h.built.register.upsert(entry("C", RiskTier.TIER_2))
    plan = plan_sync([entry("C", RiskTier.TIER_2)], [entry("C", RiskTier.TIER_3), entry("N")])
    skipped = apply_plan(admin, plan, None)
    assert [e.id for e in skipped.weakening] == ["C"]
    assert h.built.register.get("C").risk_tier is RiskTier.TIER_2  # type: ignore[union-attr]
    assert h.built.register.get("N") is not None  # creates still went through
    apply_plan(admin, plan, "CHG-1")
    assert h.built.register.get("C").risk_tier is RiskTier.TIER_3  # type: ignore[union-attr]
    h.core.m.ledger.write_pending()
    changes = [e for e in store.rows if e.event_type == "register.changed"]
    assert changes and all(e.body["principal_id"] == "register-sync" for e in changes)


def test_file_is_validated_completely_before_anything_changes() -> None:
    good = entry("A").model_dump(mode="json")
    with pytest.raises(SyncFileError, match="entry 2"):
        load_entries(json.dumps([good, {"id": "B"}]))
    with pytest.raises(SyncFileError, match="duplicate"):
        load_entries(json.dumps([good, good]))
    with pytest.raises(SyncFileError):
        load_entries("[]")
    with pytest.raises(SyncFileError):
        load_entries("not json")
    assert load_entries(json.dumps([good]))[0].id == "A"
