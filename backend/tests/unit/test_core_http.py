"""HTTP Core API: the server never trusts client-supplied context, decisions or ids."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from captain.api.core_http import create_app
from captain.core.models import AutonomyLevel, PrincipalType, RiskTier
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness
from tests.unit.test_engine import Guard, RedactPolicy


@pytest.fixture
def env() -> tuple[CaptainCoreHarness, TestClient, dict[str, str]]:
    h = CaptainCoreHarness()
    h.core.m.policy = RedactPolicy()
    from captain.core.guardrails.service import GuardrailService

    h.core.m.guardrails = GuardrailService(Guard())
    h.register_key(
        "svc", make_context("svc", RiskTier.TIER_3, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    h.register_key(
        "agent", make_context("agent", RiskTier.TIER_2, AutonomyLevel.A3, PrincipalType.AGENT)
    )
    h.register_key(
        "other", make_context("other", RiskTier.TIER_3, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    auth = {n: f"Bearer {h._keys[n]}" for n in ("svc", "agent", "other")}  # noqa: SLF001
    return h, TestClient(create_app(h.core)), auth


def decide(c: TestClient, auth: str, **kw: Any) -> Any:
    body = {"channel": "BROWSER", "action": "MODEL_CALL", "model": "claude-sonnet", **kw}
    return c.post("/core/v1/decide", json=body, headers={"Authorization": auth})


def test_auth_required(env: Any) -> None:
    _, c, _ = env
    assert c.get("/health").json() == {"status": "ok"}
    assert decide(c, "").status_code == 401
    r = decide(c, "Bearer cpt_live_0000000000000000_" + "0" * 48 + "_00000000")
    assert r.status_code == 401 and r.json()["reason_code"] == "AUTH_INVALID"


def test_client_cannot_send_context(env: Any) -> None:
    _, c, auth = env
    r = c.post(
        "/core/v1/decide",
        headers={"Authorization": auth["svc"]},
        json={
            "channel": "API",
            "action": "MODEL_CALL",
            "model": "claude-sonnet",
            "context": {"register_entry": {"risk_tier": "TIER_4"}},
        },
    )
    assert r.status_code == 422  # unknown field refused


def test_decide_block_and_hint_conflict(env: Any) -> None:
    _, c, auth = env
    assert decide(c, auth["svc"], model="other").json()["reason_code"] == "MODEL_NOT_ALLOWED"
    r = decide(c, auth["svc"], hints={"project_id": "someone-elses"})
    assert r.status_code == 403 and r.json()["reason_code"] == "CONTEXT_UNRESOLVED"


def test_reserve_settle_owned_by_caller(env: Any) -> None:
    h, c, auth = env
    h.set_budget("org:org-1", 1000)
    rid = decide(c, auth["svc"]).json()["request_id"]
    est = {"input_tokens": 1, "max_output_tokens": 1, "amount": 600, "price_version": "p"}
    # Another principal cannot use this request id (and can't tell whether it exists).
    r = c.post(
        "/core/v1/reserve",
        json={"request_id": rid, "estimate": est},
        headers={"Authorization": auth["other"]},
    )
    assert r.status_code == 404
    res = c.post(
        "/core/v1/reserve",
        json={"request_id": rid, "estimate": est},
        headers={"Authorization": auth["svc"]},
    )
    assert res.status_code == 200
    over = decide(c, auth["svc"]).json()["request_id"]
    r = c.post(
        "/core/v1/reserve",
        json={"request_id": over, "estimate": est},
        headers={"Authorization": auth["svc"]},
    )
    assert r.status_code == 429 and r.json()["reason_code"] == "BUDGET_EXHAUSTED"
    actual = {
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "source": "ESTIMATED",
        "cost": 100,
        "price_version": "p",
    }
    s = c.post(
        "/core/v1/settle",
        json={"reservation_id": res.json()["reservation_id"], "actual": actual},
        headers={"Authorization": auth["svc"]},
    )
    assert s.json()["released"] == 500


def test_redaction_and_restore(env: Any) -> None:
    _, c, auth = env
    d = decide(c, auth["svc"], contents=[{"text": "mail jo@x.com", "trusted": True}]).json()
    assert d["decision"] == "ALLOW_WITH_REDACTION"
    assert d["redacted_contents"][0]["text"] == "mail <EMAIL_ADDRESS_1>"
    r = c.post(
        "/core/v1/restore",
        json={"request_id": d["request_id"], "text": "to <EMAIL_ADDRESS_1>"},
        headers={"Authorization": auth["svc"]},
    )
    assert r.json() == {"text": "to jo@x.com"}
    r = c.post(
        "/core/v1/restore",
        json={"request_id": d["request_id"], "text": "x"},
        headers={"Authorization": auth["other"]},
    )
    assert r.status_code == 404


def test_approval_over_http(env: Any) -> None:
    h, c, auth = env
    hdr = {"Authorization": auth["agent"]}
    d = c.post(
        "/core/v1/decide",
        headers=hdr,
        json={
            "channel": "MCP",
            "action": "TOOL_CALL",
            "tool": "jira.create_issue",
            "tool_arguments": {"s": 1},
        },
    ).json()
    assert d["decision"] == "REQUIRE_APPROVAL"
    t = c.post("/core/v1/approvals", json={"request_id": d["request_id"]}, headers=hdr).json()
    assert (
        c.get(
            f"/core/v1/approvals/{t['ticket_id']}", headers={"Authorization": auth["svc"]}
        ).status_code
        == 404
    )
    h.core.decide_approval(UUID(t["ticket_id"]), True, "fin-1", "ok")
    used = c.post(
        f"/core/v1/approvals/{t['ticket_id']}/consume",
        json={"request_id": d["request_id"]},
        headers=hdr,
    )
    assert used.json()["state"] == "CONSUMED"
    again = c.post(
        f"/core/v1/approvals/{t['ticket_id']}/consume",
        json={"request_id": d["request_id"]},
        headers=hdr,
    )
    assert again.status_code == 409


def test_record_only_observed_and_on_own_behalf(env: Any) -> None:
    h, c, auth = env
    hdr = {"Authorization": auth["svc"]}
    base = {"event_type": "call.observed", "channel": "HOOK", "trace_id": "t"}
    assert (
        c.post("/core/v1/record", json={**base, "coverage": "GOVERNED"}, headers=hdr).status_code
        == 503
    )
    r = c.post(
        "/core/v1/record",
        json={**base, "coverage": "OBSERVED", "principal_id": "someone-else"},
        headers=hdr,
    )
    assert r.status_code == 200
    from captain.core.ledger.service import InMemoryLedgerStore

    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    body = next(b for (_p, t, b) in store.outbox.values() if t == "call.observed")
    assert body["principal_id"] == "svc"  # not impersonated


def test_body_size_limit(env: Any) -> None:
    h, _, auth = env
    c = TestClient(create_app(h.core, max_body_bytes=100))
    assert decide(c, auth["svc"], contents=[{"text": "x" * 500}]).status_code == 413
