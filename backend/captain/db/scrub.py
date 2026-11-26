"""PostgreSQL side of erasure for the editable records (see captain.core.scrub).

Everything happens in ONE transaction: either every record that names the person is
changed, or none is. Candidate rows are found with LIKE and then checked with the exact
whole-token rule in Python, so a longer id that merely contains the person's id is left
alone. The evidence ledger is never touched (it already holds only the pseudonym).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Connection, Engine

from captain.core.scrub import mentions, swapper
from captain.core.subjects import ERASED
from captain.db import schema as s


def _like(v: str) -> str:
    return "%" + v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class PgScrubber:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def scrub(self, old: str, new: str) -> dict[str, int]:
        sw = swapper(old, new)
        n: dict[str, int] = {}
        like = _like(old)
        with self._e.begin() as c:
            self._principal(c, old, new, n)
            self._json_rows(c, s.approval_tickets, "ticket_id", like, old, sw, n,
                            "approval_tickets", extra=("requested_by",))  # fmt: skip
            self._json_rows(c, s.alerts, "id", like, old, sw, n, "alerts")
            self._rekey(c, s.kill_switches, ("scope", "target"), ("target", "actor_id"),
                        like, old, sw, n, "kill_switches")  # fmt: skip
            self._rekey(c, s.budget_definitions, ("id",), ("scope",), like, old, sw, n, "budgets")
            self._rekey(c, s.rate_limits, ("scope",), ("scope",), like, old, sw, n, "rate_limits")
            for t, kind in ((s.budget_buckets, "budget_counters"),
                            (s.budget_alert_marks, "budget_counters"),
                            (s.rate_windows, "budget_counters")):  # fmt: skip
                pk = tuple(col.name for col in t.primary_key.columns)
                self._rekey(c, t, pk, ("bucket",), like, old, sw, n, kind)
            for r in c.execute(select(s.budget_reservations)).mappings():
                if mentions(old, list(r["buckets"])):
                    c.execute(
                        update(s.budget_reservations)
                        .where(s.budget_reservations.c.reservation_id == r["reservation_id"])
                        .values(buckets=sw(list(r["buckets"])))
                    )
                    n["budget_counters"] = n.get("budget_counters", 0) + 1
            gone = c.execute(delete(s.api_state).where(s.api_state.c.owner == old)).rowcount
            if gone:
                n["api_state"] = int(gone)
        return n

    @staticmethod
    def _principal(c: Connection, old: str, new: str, n: dict[str, int]) -> None:
        P = s.principals  # noqa: N806
        row = c.execute(select(P).where(P.c.id == old)).mappings().first()
        if row is None:
            return
        c.execute(insert(P).values(
            id=new, type=row["type"], org_id=row["org_id"], team_id=row["team_id"],
            display_name=ERASED, idp_subject=None, roles=[], active=False,
        ))  # fmt: skip
        k = c.execute(
            update(s.virtual_keys)
            .where(s.virtual_keys.c.principal_id == old)
            .values(principal_id=new, label="")
        ).rowcount
        c.execute(
            update(s.workload_bindings)
            .where(s.workload_bindings.c.principal_id == old)
            .values(principal_id=new)
        )
        c.execute(delete(P).where(P.c.id == old))
        n["principals"] = 1
        if k:
            n["keys"] = int(k)

    @staticmethod
    def _json_rows(
        c: Connection, t: Any, pk: str, like: str, old: str, sw: Any, n: dict[str, int],
        kind: str, extra: tuple[str, ...] = (),
    ) -> None:  # fmt: skip
        from sqlalchemy import String, cast, or_

        conds = [cast(t.c.doc, String).like(like, escape="\\")]
        conds += [t.c[x].like(like, escape="\\") for x in extra]
        for r in c.execute(select(t).where(or_(*conds))).mappings():
            if not mentions(old, [r["doc"], *(r[x] for x in extra)]):
                continue
            vals = {"doc": sw(r["doc"]), **{x: sw(r[x]) for x in extra}}
            c.execute(update(t).where(t.c[pk] == r[pk]).values(**vals))
            n[kind] = n.get(kind, 0) + 1

    @staticmethod
    def _rekey(
        c: Connection, t: Any, pk: tuple[str, ...], cols: tuple[str, ...], like: str,
        old: str, sw: Any, n: dict[str, int], kind: str,
    ) -> None:  # fmt: skip
        """Rows whose key columns may change are deleted and re-inserted, swapped."""
        from sqlalchemy import or_

        rows = (
            c.execute(select(t).where(or_(*(t.c[x].like(like, escape="\\") for x in cols))))
            .mappings()
            .all()
        )
        for r in rows:
            if not mentions(old, [r[x] for x in cols]):
                continue
            row = dict(r)
            c.execute(delete(t).where(*(t.c[k] == row[k] for k in pk)))
            c.execute(insert(t).values(**{**row, **{x: sw(row[x]) for x in cols}}))
            n[kind] = n.get(kind, 0) + 1
