"""Erasure, part 2 (ADR-005): the editable records.

The ledger already holds only a pseudonym for a person. Erasure also replaces the person's
id with that SAME pseudonym in every editable record that still names them: the principal
and its keys, approval tickets, alert history, budgets and rate limits on their scopes,
and kill switches. Once their key is deleted the pseudonym resolves to nothing, so every
record shows "erased subject" and counts, spend and links between records still add up.

An id is replaced only as a whole token (not inside a longer id: erasing `ravi` leaves
`ravindra` alone). Tokens are delimited by anything but letters, digits and `.@+_-`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from captain.core.subjects import ERASED


class Scrubber(Protocol):
    def scrub(self, old: str, new: str) -> dict[str, int]:
        """Replace `old` with `new` everywhere; returns records changed per kind."""
        ...


def swapper(old: str, new: str) -> Callable[[Any], Any]:
    pat = re.compile(r"(?<![\w.@+-])" + re.escape(old) + r"(?![\w.@+-])")

    def swap(v: Any) -> Any:
        if isinstance(v, str):
            return pat.sub(new, v)
        if isinstance(v, dict):
            return {swap(k): swap(x) for k, x in v.items()}
        if isinstance(v, list):
            return [swap(x) for x in v]
        if isinstance(v, tuple):
            return tuple(swap(x) for x in v)
        if isinstance(v, frozenset):
            return frozenset(swap(x) for x in v)
        return v

    return swap


def mentions(old: str, v: Any) -> bool:
    return bool(swapper(old, "\x00")(v) != v)


class InMemoryScrubber:
    """Over the in-memory stores (tests and the harness)."""

    def __init__(self, infra: Any) -> None:
        self._i = infra

    def scrub(self, old: str, new: str) -> dict[str, int]:
        sw = swapper(old, new)
        n: dict[str, int] = {}

        def bump(kind: str) -> None:
            n[kind] = n.get(kind, 0) + 1

        ids = self._i.identity_store
        if (p := ids._principals.pop(old, None)) is not None:  # noqa: SLF001
            ids._principals[new] = replace(  # noqa: SLF001
                p, id=new, display_name=ERASED, idp_subject=None, roles=frozenset(), active=False
            )
            bump("principals")
        for kid, k in list(ids._keys.items()):  # noqa: SLF001
            if k.principal_id == old:
                ids._keys[kid] = replace(k, principal_id=new, label="")  # noqa: SLF001
                bump("keys")
        ap = self._i.approval_store
        for tid, t in list(ap._t.items()):  # noqa: SLF001
            if mentions(old, t.model_dump(mode="json")):
                ap._t[tid] = type(t).model_validate(sw(t.model_dump(mode="json")))  # noqa: SLF001
                bump("approval_tickets")
        al = self._i.alert_store
        for aid, a in list(getattr(al, "items", {}).items()):
            if mentions(old, (a.subject, a.details, a.recipients, a.register_id)):
                al.items[aid] = replace(
                    a, subject=sw(a.subject), details=sw(a.details), recipients=sw(a.recipients)
                )
                bump("alerts")
        ks = self._i.killswitch_store
        for key, e in list(ks._entries.items()):  # noqa: SLF001
            if mentions(old, (e.target, e.actor_id)):
                del ks._entries[key]  # noqa: SLF001
                e2 = replace(e, target=sw(e.target), actor_id=sw(e.actor_id))
                ks._entries[(e2.scope, e2.target)] = e2  # noqa: SLF001
                bump("kill_switches")
        cat = self._i.budget_catalog
        for scope in [s for s in list(cat.budgets) if mentions(old, s)]:
            cat.budgets[sw(scope)] = [replace(d, scope=sw(d.scope)) for d in cat.budgets.pop(scope)]
            bump("budgets")
        for scope in [s for s in list(cat.rates) if mentions(old, s)]:
            cat.rates[sw(scope)] = replace(cat.rates.pop(scope), scope=sw(scope))
            bump("rate_limits")
        bs = self._i.budget_store
        for d in (bs._b, bs._rate):  # noqa: SLF001
            for k in [k for k in list(d) if mentions(old, k)]:
                d[sw(k)] = d.pop(k)
                bump("budget_counters")
        bs._alerted = {sw(k) for k in bs._alerted}  # noqa: SLF001
        return n
