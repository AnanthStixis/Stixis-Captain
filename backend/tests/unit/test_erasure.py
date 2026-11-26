"""Erasure by crypto-shredding (ADR-005 §1): a person's identifiers never reach the signed
ledger in the clear, every view still shows them, and after erasure every view shows
"erased subject" while totals and ledger verification are unchanged."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from captain.api.admin_auth import ChainAuthenticator, DevTokenAuthenticator
from captain.api.admin_http import create_admin_app
from captain.api.admin_queries import InMemoryAdminQueries
from captain.core.admin import AdminActor
from captain.core.alerts import Alert, Severity
from captain.core.budget.model import RateLimitDefinition
from captain.core.decision_api import AuthenticationError
from captain.core.identity.killswitch import KillScope
from captain.core.models import (
    ActualUsage,
    ApprovalState,
    ApprovalTicket,
    AutonomyLevel,
    ContextHints,
    CostEstimate,
    Credential,
    PrincipalType,
    RiskTier,
    TokenUsage,
    UsageSource,
)
from captain.core.scrub import swapper
from captain.core.subjects import ERASED, InMemorySubjectStore, Pseudonymiser
from tests.conftest import make_context, model_call
from tests.harness.adapters import CaptainCoreHarness
from tests.unit.test_admin_http import StaticAuth

PERSON = "ravi@example.com"
NEAR_MISS = "ravi@example.com.au"
DEV = "d" * 40
VIEWER = "v" * 40


def H(tok: str = DEV) -> dict[str, str]:  # noqa: N802
    return {"Authorization": f"Bearer {tok}"}


# ------------------------------------------------------------------ pseudonymiser


def pz(people: set[str] | None = None) -> Pseudonymiser:
    people = people if people is not None else {PERSON, "admin@x"}
    return Pseudonymiser(InMemorySubjectStore(), lambda pid: pid in people)


def test_every_personal_field_is_replaced_and_comes_back() -> None:
    p = pz()
    body: dict[str, Any] = {
        "principal_id": PERSON,
        "register_id": "AI-chat",
        "details": {
            "actor": "admin@x",
            "approvers": f"{PERSON},svc",
            "scopes": f"org:o,principal:{PERSON},app:a",
            "scope": f"ptool:{PERSON}:jira.create_issue",
            "client_ip": "10.1.2.3",
            "kind": "x",
        },
    }
    out = p.event(body)
    assert PERSON not in json.dumps(out) and "admin@x" not in json.dumps(out)
    assert "10.1.2.3" not in json.dumps(out) and out["details"]["client_ip"].startswith("ip_")
    assert out["details"]["approvers"].endswith(",svc")  # service ids stay readable
    assert out["details"]["scopes"].startswith("org:o,principal:subj_")
    assert out["details"]["scope"].endswith(":jira.create_issue")
    assert p.event(body) == out  # stable: the same person always gets the same pseudonym
    back = p.resolve(out)
    assert back["principal_id"] == PERSON and back["details"]["actor"] == "admin@x"
    assert back["details"]["scopes"] == body["details"]["scopes"]
    assert back["details"]["scope"] == body["details"]["scope"]
    assert back["details"]["client_ip"] == out["details"]["client_ip"]  # IPs never come back


def test_services_system_actors_and_kill_targets() -> None:
    p = pz()
    assert p.person("svc") == "svc" and p.person("captain") == "captain" and p.person(None) is None
    killed = p.event(
        {"principal_id": "admin@x", "details": {"scope": "principal", "target": PERSON}}
    )
    assert killed["details"]["target"].startswith("subj_")
    other = p.event({"principal_id": "svc", "details": {"scope": "key", "target": "k1"}})
    assert other["details"]["target"] == "k1"


def test_erasure_makes_the_pseudonym_unresolvable() -> None:
    p = pz()
    out = p.event({"principal_id": PERSON})
    assert p.store.delete(PERSON) == out["principal_id"]
    assert p.resolve(out)["principal_id"] == ERASED
    assert p.lookup(PERSON) == PERSON  # a filter for an erased person finds nothing
    assert p.event({"principal_id": PERSON})["principal_id"] != out["principal_id"]  # new key


def test_ip_keys_change_daily_and_are_pruned() -> None:
    days = iter([date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 2)])
    p = Pseudonymiser(InMemorySubjectStore(), lambda _: True, today=lambda: next(days))
    a, b, c = p.ip("10.0.0.1"), p.ip("10.0.0.1"), p.ip("10.0.0.1")
    assert a == b != c  # linkable within a day only
    assert p.ip("invalid") == "invalid"
    assert p.prune_ip_days(30, now=datetime.now(UTC) + timedelta(days=31)) == 2
    assert p.prune_ip_days(30) == 0


# ------------------------------------------------------------------ end to end


@pytest.fixture
def env() -> tuple[CaptainCoreHarness, TestClient, Any]:
    h = CaptainCoreHarness(erasure=True)
    ctx = make_context(PERSON, RiskTier.TIER_3, AutonomyLevel.A2, PrincipalType.USER)
    ctx = ctx.model_copy(
        update={
            "register_entry": ctx.register_entry.model_copy(update={"id": "AI-chat"}),
            "application_id": "app-chat",
        }
    )
    h.register_key("ravi-key", ctx)
    h.register_key(
        "svc", make_context("svc", RiskTier.TIER_3, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    h.set_budget("org:org-1", 1_000_000)
    h.set_budget(f"principal:{PERSON}", 1_000_000)
    h.set_budget(f"principal:{NEAR_MISS}", 5)  # a different person whose id contains it
    auth = ChainAuthenticator(
        DevTokenAuthenticator(DEV),
        StaticAuth({VIEWER: AdminActor("viewer-1", frozenset({"viewer"}))}),
    )
    c = TestClient(create_admin_app(h.built, auth, InMemoryAdminQueries(h.built.infra)))
    rctx = h.resolve(Credential(kind="virtual_key", secret="ravi-key"), ContextHints())
    d = h.core.decide(model_call(rctx))
    r = h.core.reserve(d, CostEstimate(input_tokens=1, max_output_tokens=1, amount=500,
                                       price_version="p"))  # fmt: skip
    h.core.settle(r, ActualUsage(usage=TokenUsage(input_tokens=5, output_tokens=5),
                                 source=UsageSource.PROVIDER_REPORTED, cost=120,
                                 price_version="p", latency_ms=40))  # fmt: skip
    h.core.decide(model_call(rctx, model="forbidden"))
    with pytest.raises(AuthenticationError):  # a refused, unattributable request with an IP
        h.resolve(
            Credential(kind="virtual_key", secret="cpt_bad"), ContextHints(client_ip="10.9.8.7")
        )
    h.core.kill(KillScope.PRINCIPAL, PERSON, "test", "dev-admin")
    h.core.unkill(KillScope.PRINCIPAL, PERSON, "test", "dev-admin")
    h.core.m.ledger.write_pending()
    return h, c, d


def _raw(h: CaptainCoreHarness) -> str:
    return json.dumps([e.body for e in h.built.infra.ledger_store.entries()])


def _views(c: TestClient) -> dict[str, Any]:
    ev = c.get("/admin/v1/events?days=7", headers=H()).json()
    return {
        "events": ev,
        "people": {i["principal_id"] for i in ev["items"]},
        "by_person": c.get("/admin/v1/usage/breakdown?by=principal&days=7", headers=H()).json(),
        "coverage": c.get("/admin/v1/coverage?days=7", headers=H()).json(),
        "chargeback": c.get("/admin/v1/reports/chargeback?days=7", headers=H()).json(),
    }


def test_ledger_holds_no_personal_ids_but_views_show_the_person(env: Any) -> None:
    h, c, d = env
    raw = _raw(h)
    assert PERSON not in raw and "10.9.8.7" not in raw and "subj_" in raw and '"ip_' in raw
    v = _views(c)
    assert PERSON in v["people"]
    assert any(i["key"] == PERSON for i in v["by_person"]["items"])
    assert any(p["principal_id"] == PERSON for p in v["coverage"]["by_person"])
    mine = c.get(f"/admin/v1/events?days=7&principal_id={PERSON}", headers=H()).json()
    assert mine["total"] >= 3 and {i["principal_id"] for i in mine["items"]} == {PERSON}
    detail = c.get(f"/admin/v1/requests/{d.request_id}", headers=H()).json()
    assert {e["principal_id"] for e in detail["events"]} == {PERSON}
    kills = [i for i in c.get("/admin/v1/events?days=7&kind=admin", headers=H()).json()["items"]
             if i["event_type"] == "killswitch.changed"]  # fmt: skip
    assert kills and all((i.get("details") or {}).get("target") == PERSON for i in kills)


def test_erase_refused_while_keys_active_and_for_services_and_viewers(env: Any) -> None:
    h, c, _ = env
    body = {"reason": "data protection request"}
    assert (
        c.post(f"/admin/v1/subjects/{PERSON}/erase", headers=H(VIEWER), json=body).status_code
        == 403
    )
    r = c.post(f"/admin/v1/subjects/{PERSON}/erase", headers=H(), json=body)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "KEYS_ACTIVE"
    assert c.post("/admin/v1/subjects/svc/erase", headers=H(), json=body).status_code == 400


def test_erase_then_every_view_shows_erased_subject_and_totals_hold(env: Any) -> None:
    h, c, _ = env
    before = _views(c)
    h.revoke_key("ravi-key")
    r = c.post(f"/admin/v1/subjects/{PERSON}/erase", headers=H(), json={"reason": "DSR-42"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "erased" and r.json()["subject"].startswith("subj_")
    h.core.m.ledger.write_pending()
    after = _views(c)

    assert PERSON not in json.dumps({k: v for k, v in after.items() if k != "people"})
    assert PERSON not in after["people"] and ERASED in after["people"]
    total = after["by_person"]["total_cost_micros"]
    assert total == before["by_person"]["total_cost_micros"] > 0
    assert sum(i["cost_micros"] for i in after["chargeback"]) == sum(
        i["cost_micros"] for i in before["chargeback"]
    )
    gone = c.get(f"/admin/v1/events?days=7&principal_id={PERSON}", headers=H()).json()
    assert gone["total"] == 0
    admin = c.get("/admin/v1/events?days=7&kind=admin", headers=H()).json()["items"]
    erased = [i for i in admin if i["event_type"] == "subject.erased"]
    assert erased and PERSON not in json.dumps(erased)
    assert c.post("/admin/v1/ledger/verify", headers=H()).json()["ok"] is True
    again = c.post(f"/admin/v1/subjects/{PERSON}/erase", headers=H(), json={"reason": "again"})
    assert again.json()["status"] == "nothing_to_erase"


# ------------------------------------------------------------------ editable records


def test_swapper_replaces_whole_ids_only() -> None:
    sw = swapper(PERSON, "subj_x")
    assert sw(f"principal:{PERSON}|monthly") == "principal:subj_x|monthly"
    assert sw(f"{PERSON},fin-1") == "subj_x,fin-1"
    assert sw(NEAR_MISS) == NEAR_MISS and sw(f"x{PERSON}") == f"x{PERSON}"
    assert sw({PERSON: [PERSON, 1]}) == {"subj_x": ["subj_x", 1]}


def _stores_text(h: CaptainCoreHarness) -> str:
    i = h.built.infra
    parts = [
        i.identity_store._principals, i.identity_store._keys,  # type: ignore[attr-defined]
        i.approval_store._t, i.alert_store.items,  # type: ignore[attr-defined]
        i.killswitch_store._entries, i.budget_catalog.budgets,  # type: ignore[attr-defined]
        i.budget_catalog.rates, i.budget_store._b,  # type: ignore[attr-defined]
    ]  # fmt: skip
    return repr(parts).replace(NEAR_MISS, "")


def test_erase_replaces_the_person_in_every_editable_record(env: Any) -> None:
    h, c, _ = env
    i = h.built.infra
    i.budget_catalog.add_rate(RateLimitDefinition(f"ptool:{PERSON}:jira.create_issue", 5))  # type: ignore[attr-defined]
    h.built.alert_service.raise_alert(
        Alert("budget.threshold", Severity.WARNING, f"Budget principal:{PERSON} reached 80%",
              {"scope": f"principal:{PERSON}"}, recipients=(PERSON,))
    )  # fmt: skip
    i.approval_store.put(
        ApprovalTicket(request_id=uuid4(), state=ApprovalState.PENDING, arguments_hash="h",
                       requested_by=PERSON, assigned_approvers=(PERSON, "fin-1"),
                       expires_at=datetime.now(UTC) + timedelta(hours=1))
    )  # fmt: skip
    h.core.kill(KillScope.PRINCIPAL, PERSON, "left on", "dev-admin")
    spent = h.budget(f"principal:{PERSON}")["spent"]
    assert spent > 0 and PERSON in _stores_text(h)

    h.revoke_key("ravi-key")
    r = c.post(f"/admin/v1/subjects/{PERSON}/erase", headers=H(), json={"reason": "DSR-43"})
    assert r.status_code == 200, r.text
    pseudo, changed = r.json()["subject"], r.json()["records_changed"]
    assert {"principals", "keys", "approval_tickets", "alerts", "kill_switches", "budgets",
            "rate_limits", "budget_counters"} <= set(changed)  # fmt: skip

    assert PERSON not in _stores_text(h)
    assert f"principal:{NEAR_MISS}" in i.budget_catalog.budgets  # type: ignore[attr-defined]
    assert h.budget(f"principal:{pseudo}")["spent"] == spent  # spend is kept
    p = i.identity_store.get_principal(pseudo)
    assert p is not None and p.display_name == ERASED and not p.active
    keys = c.get("/admin/v1/keys", headers=H()).json()
    assert PERSON not in json.dumps(keys)
