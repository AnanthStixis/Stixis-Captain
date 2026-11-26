"""Register sync: keep the AI Register in step with the organisation's source of truth.

    python -m captain.register_sync --file register.json              # show what would change
    python -m captain.register_sync --file register.json --apply      # make the changes
    python -m captain.register_sync --file register.json --apply --approval-ref CHG-1234

`register.json` is a JSON list of register entries (the same fields the AI Register page
shows), exported from your CMDB / GRC tool by whatever means you have. Nothing about any
product's export is assumed.

Safety rules (the register is the control plane, so sync is deliberately cautious):
- New entries are created; changed entries are updated, each recorded in the evidence
  ledger as actor "register-sync".
- A change that WEAKENS controls (lower tier, lower autonomy, high-risk flag removed) is
  applied only with --approval-ref; without it the entry is skipped and reported.
- An entry missing from the file is reported and left alone: sync never retires a system
  by omission.
- The file is validated completely before anything changes; one bad entry stops the run.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from captain.core.admin import AdminActor, AdminService
from captain.core.context.service import is_control_downgrade
from captain.core.models import RegisterEntry

SYNC_ACTOR = AdminActor("register-sync", frozenset({"admin"}))


class SyncFileError(Exception):
    pass


@dataclass
class SyncPlan:
    create: list[RegisterEntry] = field(default_factory=list)
    update: list[RegisterEntry] = field(default_factory=list)
    weakening: list[RegisterEntry] = field(default_factory=list)  # need an approval reference
    unchanged: list[str] = field(default_factory=list)
    missing_from_file: list[str] = field(default_factory=list)


def load_entries(text: str) -> list[RegisterEntry]:
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError as e:
        raise SyncFileError(f"not valid JSON: {e}") from e
    if not isinstance(raw, list) or not raw:
        raise SyncFileError("the file must be a non-empty JSON list of register entries")
    entries: list[RegisterEntry] = []
    for i, item in enumerate(raw, 1):
        try:
            entries.append(RegisterEntry.model_validate(item))
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise SyncFileError(f"entry {i}: {where}: {first['msg']}") from e
    ids = [e.id for e in entries]
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        raise SyncFileError(f"duplicate ids in the file: {', '.join(dupes)}")
    return entries


def _same(a: RegisterEntry, b: RegisterEntry) -> bool:
    return a.model_copy(update={"version": 1}) == b.model_copy(update={"version": 1})


def plan_sync(current: list[RegisterEntry], desired: list[RegisterEntry]) -> SyncPlan:
    have = {e.id: e for e in current}
    plan = SyncPlan()
    for d in desired:
        old = have.get(d.id)
        if old is None:
            plan.create.append(d)
        elif _same(old, d):
            plan.unchanged.append(d.id)
        elif is_control_downgrade(old, d):
            plan.weakening.append(d)
        else:
            plan.update.append(d)
    plan.missing_from_file = sorted(set(have) - {d.id for d in desired})
    return plan


def apply_plan(admin: AdminService, plan: SyncPlan, approval_ref: str | None) -> SyncPlan:
    """Apply a plan; returns what was left undone (weakening changes without approval)."""
    for e in [*plan.create, *plan.update]:
        admin.upsert_register(SYNC_ACTOR, e)
    skipped = SyncPlan()
    for e in plan.weakening:
        if approval_ref:
            admin.upsert_register(SYNC_ACTOR, e, approval_ref)
        else:
            skipped.weakening.append(e)
    return skipped


def describe(plan: SyncPlan, skipped: SyncPlan | None = None) -> str:
    lines = [
        f"create: {len(plan.create)}  update: {len(plan.update)}  unchanged: {len(plan.unchanged)}"
    ]
    lines += [f"  + {e.id} ({e.risk_tier.value}, {e.autonomy.value})" for e in plan.create]
    lines += [f"  ~ {e.id}" for e in plan.update]
    if plan.weakening:
        lines.append(f"weakens controls (needs --approval-ref): {len(plan.weakening)}")
        lines += [f"  ! {e.id} -> {e.risk_tier.value}, {e.autonomy.value}" for e in plan.weakening]
    if plan.missing_from_file:
        lines.append(
            "in the register but not in the file (left alone): " + ", ".join(plan.missing_from_file)
        )
    if skipped is not None and skipped.weakening:
        lines.append(f"SKIPPED {len(skipped.weakening)} weakening change(s): no approval reference")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="captain-register-sync", description=__doc__)
    ap.add_argument("--file", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approval-ref")
    a = ap.parse_args(argv)
    try:
        desired = load_entries(a.file.read_text(encoding="utf-8-sig"))
    except (SyncFileError, OSError) as e:
        print(f"cannot use the file: {e}", file=sys.stderr)
        return 2
    from sqlalchemy import create_engine

    from captain.api.admin_queries import PgAdminQueries
    from captain.config import build_from_env

    built, settings = build_from_env()
    admin = AdminService(built.core, built.register, built.infra.budget_catalog)  # type: ignore[arg-type]
    plan = plan_sync(
        PgAdminQueries(create_engine(settings.database_url)).register_entries(), desired
    )
    if not a.apply:
        print(describe(plan) + "\n(dry run: nothing changed; add --apply to make the changes)")
        return 0
    skipped = apply_plan(admin, plan, a.approval_ref)
    print(describe(plan, skipped))
    return 1 if skipped.weakening else 0


if __name__ == "__main__":
    sys.exit(main())
