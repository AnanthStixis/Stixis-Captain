from __future__ import annotations

from types import SimpleNamespace as N

from captain.api.admin_deletes import budget_check, register_check, user_check
from captain.core.budget.model import BudgetDefinition, Period


class Q:
    def __init__(self, budgets=(), keys=(), entries=()):
        self._b, self._k, self._e = list(budgets), list(keys), list(entries)

    def budget_definitions(self):
        return self._b

    def keys(self, principal_id=None):
        return [k for k in self._k if principal_id in (None, k.principal_id)]

    def register_entries(self):
        return self._e

    def usage_by(self, by, since, until):
        return [("ravi@corp.example", 12, 0, 0, 0, 0, 0)]


class Budgets:
    def state(self, bucket):
        return N(spent=5_000_000)


def test_the_last_limit_that_stops_calls_cannot_be_removed():
    org = BudgetDefinition("org:o1", Period.MONTHLY, 100_000_000, True)
    team = BudgetDefinition("team:pay", Period.MONTHLY, 10_000_000, False)
    c = budget_check(Q([org, team]), Budgets(), "org:o1")
    assert not c["can_delete"] and c["spent_micros"] == 5_000_000
    assert budget_check(Q([org, team]), Budgets(), "team:pay")["can_delete"]
    other = BudgetDefinition("team:hr", Period.DAILY, 1, True)
    assert budget_check(Q([org, other]), Budgets(), "org:o1")["can_delete"]


def test_an_ai_use_with_live_keys_cannot_be_retired():
    live = N(register_id="coding", status=N(value="ACTIVE"), principal_id="x")
    gone = N(register_id="coding", status=N(value="REVOKED"), principal_id="x")
    e = [N(id="coding", name="Coding assistant", active=True, owner_id="o")]
    assert not register_check(Q(keys=[live, gone], entries=e), "coding")["can_delete"]
    assert register_check(Q(keys=[gone], entries=e), "coding")["can_delete"]


def test_a_person_who_owns_an_ai_use_or_is_the_last_admin_is_kept():
    e = [N(id="c", name="Coding", active=True, owner_id="ravi@corp.example")]
    key = N(register_id="c", status=N(value="ACTIVE"), principal_id="ravi@corp.example")
    c = user_check(Q(keys=[key], entries=e), "Ravi@corp.example", {"priya@corp.example"})
    assert not c["can_delete"] and c["active_keys"] == 1 and c["calls_recent"] == 12
    assert not user_check(Q(), "priya@corp.example", {"priya@corp.example"})["can_delete"]
    assert user_check(Q(), "priya@corp.example", {"priya@corp.example", "a@b.c"})["can_delete"]
