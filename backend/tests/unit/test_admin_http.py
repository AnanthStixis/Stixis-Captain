from __future__ import annotations

import io
import json
import time
import zipfile
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from openpyxl import load_workbook

from captain.api.admin_auth import (
    AdminSsoConfig,
    ChainAuthenticator,
    DevTokenAuthenticator,
    OidcAdminAuthenticator,
)
from captain.api.admin_http import create_admin_app
from captain.api.admin_queries import InMemoryAdminQueries
from captain.core.admin import AdminActor
from captain.core.decision_api import AuthenticationError
from captain.core.identity.oidc import IssuerConfig, StaticJwks
from captain.core.models import (
    AutonomyLevel,
    ContextHints,
    CostEstimate,
    Credential,
    PrincipalType,
    RiskTier,
)
from tests.conftest import make_context, model_call, tool_call
from tests.harness.adapters import CaptainCoreHarness

DEV = "d" * 40
VIEWER = "v" * 40


class StaticAuth:
    def __init__(self, tokens: dict[str, AdminActor]) -> None:
        self.tokens = tokens

    def authenticate(self, token: str, client_host: str | None) -> AdminActor:
        if token not in self.tokens:
            raise AuthenticationError("invalid admin token")
        return self.tokens[token]


@pytest.fixture(params=[False, True], ids=["plain", "erasure"])
def env(request: pytest.FixtureRequest) -> tuple[CaptainCoreHarness, TestClient]:
    # Every view test runs twice: with person ids in the clear, and with the ledger holding
    # pseudonyms (ADR-005). The views must answer the same either way.
    h = CaptainCoreHarness(erasure=request.param)
    h.register_key(
        "svc", make_context("svc", RiskTier.TIER_3, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    h.register_key(
        "agent", make_context("agent", RiskTier.TIER_2, AutonomyLevel.A3, PrincipalType.AGENT)
    )
    h.set_budget("org:org-1", 1_000_000)
    auth = ChainAuthenticator(
        DevTokenAuthenticator(DEV),
        StaticAuth(
            {
                VIEWER: AdminActor("viewer-1", frozenset({"viewer"})),
                "a" * 40: AdminActor("fin-1", frozenset({"approver"})),
            }
        ),
    )
    return h, TestClient(create_admin_app(h.built, auth, InMemoryAdminQueries(h.built.infra)))


def H(tok: str = DEV) -> dict[str, str]:  # noqa: N802
    return {"Authorization": f"Bearer {tok}"}


def activity(h: CaptainCoreHarness) -> None:
    ctx = h.resolve(Credential(kind="virtual_key", secret="svc"), ContextHints())
    d = h.core.decide(model_call(ctx))
    from captain.core.models import ActualUsage, TokenUsage, UsageSource

    r = h.core.reserve(
        d, CostEstimate(input_tokens=1, max_output_tokens=1, amount=500, price_version="p")
    )
    h.core.settle(
        r,
        ActualUsage(
            usage=TokenUsage(input_tokens=5, output_tokens=5),
            source=UsageSource.PROVIDER_REPORTED,
            cost=120,
            price_version="p",
            latency_ms=40,
        ),
    )
    h.core.decide(model_call(ctx, model="forbidden"))
    h.core.m.ledger.write_pending()


def test_authentication_required(env: Any) -> None:
    _, c = env
    assert c.get("/admin/v1/me").status_code == 401
    assert c.get("/admin/v1/me", headers=H("wrong" * 10)).status_code == 401
    me = c.get("/admin/v1/me", headers=H()).json()
    assert me["id"] == "dev-admin" and "keys:write" in me["permissions"]


def test_dev_token_refused_from_remote_hosts() -> None:
    a = DevTokenAuthenticator(DEV)
    assert a.authenticate(DEV, "127.0.0.1").id == "dev-admin"
    with pytest.raises(AuthenticationError):
        a.authenticate(DEV, "10.1.2.3")
    with pytest.raises(ValueError):
        DevTokenAuthenticator("short")


def test_rbac_enforced_on_server(env: Any) -> None:
    _, c = env
    assert c.get("/admin/v1/overview", headers=H(VIEWER)).status_code == 200
    body = {
        "principal_id": "svc",
        "register_id": "AI-svc",
        "project_id": "proj-1",
        "application_id": "app-svc",
        "environment": "DEVELOPMENT",
    }
    assert c.post("/admin/v1/keys", headers=H(VIEWER), json=body).status_code == 403
    kill = {"scope": "model", "target": "x", "reason": "nope"}
    assert c.post("/admin/v1/killswitch", headers=H(VIEWER), json=kill).status_code == 403
    assert (
        c.get("/admin/v1/events?kind=admin", headers=H(VIEWER)).status_code == 403
    )  # audit needs auditor


def test_key_issue_shown_once_and_never_listed(env: Any) -> None:
    h, c = env
    body = {
        "principal_id": "svc",
        "register_id": "AI-svc",
        "project_id": "proj-1",
        "application_id": "app-svc",
        "environment": "DEVELOPMENT",
        "label": "ci",
        "ttl_days": 30,
    }
    r = c.post("/admin/v1/keys", headers=H(), json=body).json()
    raw = r["raw_key"]
    assert raw.startswith("cpt_") and "only once" in r["notice"]
    listing = c.get("/admin/v1/keys", headers=H()).text
    assert raw not in listing and "secret_hash" not in listing
    kid = r["key"]["key_id"]
    assert (
        c.post(f"/admin/v1/keys/{kid}/revoke", headers=H(), json={"reason": "test"}).json()[
            "status"
        ]
        == "revoked"
    )
    keys = {k["key_id"]: k for k in c.get("/admin/v1/keys?principal_id=svc", headers=H()).json()}
    assert keys[kid]["status"] == "REVOKED"
    assert (
        c.post("/admin/v1/keys/nope/revoke", headers=H(), json={"reason": "test"}).status_code
        == 404
    )


def test_kill_switch_round_trip_affects_decisions(env: Any) -> None:
    h, c = env
    ctx = h.resolve(Credential(kind="virtual_key", secret="svc"), ContextHints())
    k = {"scope": "model", "target": "claude-sonnet", "reason": "incident 42"}
    c.post("/admin/v1/killswitch", headers=H(), json=k)
    assert c.get("/admin/v1/killswitch", headers=H()).json()[0]["target"] == "claude-sonnet"
    assert h.core.decide(model_call(ctx)).reason_code.value == "KILL_SWITCH_ACTIVE"
    c.post("/admin/v1/killswitch/release", headers=H(), json=k)
    assert h.core.decide(model_call(ctx)).decision.value == "ALLOW"


def test_budgets_define_override_and_state(env: Any) -> None:
    _, c = env
    c.put(
        "/admin/v1/budgets",
        headers=H(),
        json={"scope": "team:team-1", "period": "MONTHLY", "limit_micros": 5000},
    )
    b = {x["scope"]: x for x in c.get("/admin/v1/budgets", headers=H()).json()}
    assert b["team:team-1"]["limit_micros"] == 5000 and b["team:team-1"]["spent_micros"] == 0
    r = c.post(
        "/admin/v1/budgets/override",
        headers=H(),
        json={"scope": "team:team-1", "extra_micros": 1000, "reason": "launch"},
    )
    assert r.status_code == 200
    assert (
        c.post(
            "/admin/v1/budgets/override",
            headers=H(),
            json={"scope": "team:none", "extra_micros": 1, "reason": "xyz"},
        ).status_code
        == 404
    )
    assert (
        c.put(
            "/admin/v1/budgets",
            headers=H(),
            json={"scope": "bad scope", "period": "MONTHLY", "limit_micros": 1},
        ).status_code
        == 422
    )


def test_approvals_queue_and_decision(env: Any) -> None:
    h, c = env
    ctx = h.resolve(Credential(kind="virtual_key", secret="agent"), ContextHints())
    req = tool_call(ctx, {"po": 1})
    t = h.core.request_approval(req, h.core.decide(req))
    pending = c.get("/admin/v1/approvals", headers=H("a" * 40)).json()
    assert [p["ticket_id"] for p in pending] == [str(t.ticket_id)]
    r = c.post(
        f"/admin/v1/approvals/{t.ticket_id}/decide",
        headers=H("a" * 40),
        json={"approve": True, "reason": "ok to proceed"},
    )
    assert r.json()["state"] == "APPROVED"
    assert (
        c.post(
            f"/admin/v1/approvals/{t.ticket_id}/decide",
            headers=H(VIEWER),
            json={"approve": True, "reason": "x y z"},
        ).status_code
        == 403
    )


def test_overview_events_and_request_detail(env: Any) -> None:
    h, c = env
    activity(h)
    o = c.get("/admin/v1/overview?days=7", headers=H()).json()
    assert o["governed_calls"] == 2 and o["spend_micros"] == 120
    assert o["refusals_by_reason"] == {"MODEL_NOT_ALLOWED": 1} and o["latency_ms"]["p50"] == 40
    ev = c.get("/admin/v1/events?days=7", headers=H()).json()
    assert ev["total"] == 3 and all("content" not in json.dumps(i).lower() for i in ev["items"])
    rid = next(i["request_id"] for i in ev["items"] if i["event_type"] == "usage.settled")
    detail = c.get(f"/admin/v1/requests/{rid}", headers=H()).json()
    assert {e["event_type"] for e in detail["events"]} == {"call.allowed", "usage.settled"}


def test_register_downgrade_needs_approval_ref(env: Any) -> None:
    h, c = env
    e = next(x for x in c.get("/admin/v1/register", headers=H()).json() if x["id"] == "AI-svc")
    weaker = {**e, "risk_tier": "TIER_4"}
    assert (
        c.put("/admin/v1/register/AI-svc", headers=H(), json={"entry": weaker}).status_code == 409
    )
    r = c.put(
        "/admin/v1/register/AI-svc", headers=H(), json={"entry": weaker, "approval_ref": "APR-9"}
    )
    assert r.status_code == 200 and r.json()["risk_tier"] == "TIER_4"


def test_registry_admin(env: Any) -> None:
    h, c = env
    from captain.core.registry.service import InMemoryRegistryStore, RegistryService

    h.core.m.registry = h.built.infra.registry = RegistryService(InMemoryRegistryStore())
    assert (
        c.put(
            "/admin/v1/registry/provider",
            headers=H(),
            json={"id": "p1", "kind": "openai", "region": "eu", "credential_ref": "env:K"},
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/admin/v1/registry/model",
            headers=H(),
            json={"alias": "m1", "provider_id": "p1", "provider_model": "x", "region": "eu"},
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/admin/v1/registry/model",
            headers=H(),
            json={"alias": "m2", "provider_id": "nope", "provider_model": "x", "region": "eu"},
        ).status_code
        == 400
    )
    assert [m["alias"] for m in c.get("/admin/v1/registry/model", headers=H()).json()] == ["m1"]


def test_audit_verify_evidence_and_reports(env: Any) -> None:
    from datetime import UTC, datetime

    h, c = env
    activity(h)
    h.core.m.ledger.anchor()
    v = c.post("/admin/v1/ledger/verify", headers=H()).json()
    assert v["ok"] and v["entries_checked"] >= 3
    z = zipfile.ZipFile(
        io.BytesIO(c.get("/admin/v1/evidence?framework=EU_AI_ACT&days=7", headers=H()).content)
    )
    assert set(z.namelist()) == {"controls.csv", "events.jsonl", "manifest.json"}
    assert json.loads(z.read("manifest.json"))["framework"] == "EU_AI_ACT"
    n = datetime.now(UTC)
    rep = c.get(f"/admin/v1/reports/monthly?year={n.year}&month={n.month}", headers=H()).json()
    assert rep["spend"]["total_micros"] == 120
    x = c.get(f"/admin/v1/reports/monthly?year={n.year}&month={n.month}&format=xlsx", headers=H())
    assert "Summary" in load_workbook(io.BytesIO(x.content)).sheetnames
    csv = c.get("/admin/v1/reports/chargeback?format=csv", headers=H()).text
    assert csv.splitlines()[0].startswith("cost_centre,team,project,model")


# ---------------------------------------------------------------- SSO mapping
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISS = "https://login.example/tenant/v2.0"


def sso() -> OidcAdminAuthenticator:
    jwk = {**json.loads(RSAAlgorithm.to_jwk(KEY.public_key())), "kid": "k1"}
    cfg = AdminSsoConfig(
        IssuerConfig(ISS, "captain-console", "https://x/jwks"),
        group_roles={"grp-captain-admins": ("admin",), "grp-finops": ("finops",)},
    )
    return OidcAdminAuthenticator(cfg, StaticJwks({ISS: {"keys": [jwk]}}))


def token(groups: list[str]) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISS,
            "aud": "captain-console",
            "sub": "u-123",
            "iat": now,
            "exp": now + 600,
            "preferred_username": "priya@corp.example",
            "groups": groups,
        },
        KEY,
        algorithm="RS256",
        headers={"kid": "k1"},
    )


def test_sso_maps_groups_to_roles() -> None:
    a = sso().authenticate(token(["grp-finops", "unrelated"]), "10.0.0.5")
    assert a.id == "priya@corp.example" and a.roles == frozenset({"finops"})
    with pytest.raises(AuthenticationError, match="no Captain role"):
        sso().authenticate(token(["unrelated"]), "10.0.0.5")


def test_console_served_with_spa_fallback_and_no_traversal(env: Any, tmp_path: Any) -> None:
    h, _ = env
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>console</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("do not serve")
    c = TestClient(
        create_admin_app(
            h.built,
            DevTokenAuthenticator(DEV),
            InMemoryAdminQueries(h.built.infra),
            static_dir=dist,
        )
    )
    assert "console" in c.get("/activity").text  # deep link -> index.html
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert "do not serve" not in c.get("/..%2Fsecret.txt").text
    assert c.get("/admin/v1/unknown").status_code == 404  # API paths never fall back
    assert c.get("/admin/v1/me", headers=H()).status_code == 200


def test_observed_usage_is_separate_from_governed_spend(env: Any) -> None:
    from datetime import date

    from captain.core.models import TokenUsage
    from captain.integrations.telemetry.base import ActorMap, ObservedRecord, import_records

    h, c = env
    activity(h)
    today = date.today()
    import_records(
        h.core,
        [
            ObservedRecord(
                "cursor",
                "r1",
                today,
                "a@example.com",
                model="m",
                tokens=TokenUsage(input_tokens=100, output_tokens=40),
                cost_micro=2_000_000,
            ),
            ObservedRecord("cursor", "r2", today, "b@example.com", cost_micro=1_000_000),
            ObservedRecord(
                "cursor", "old", date(2020, 1, 1), "c@example.com", cost_micro=9_000_000
            ),
        ],
        ActorMap({"a@example.com": "alice"}),
    )
    h.core.m.ledger.write_pending()
    o = c.get("/admin/v1/observed?days=30", headers=H()).json()
    assert (
        o["totals"]["events"] == 2 and o["totals"]["cost_micros"] == 3_000_000
    )  # the 2020 row is out
    assert o["governed_spend_micros"] == 120  # untouched by imports
    (src,) = o["by_source"]
    assert src["source"] == "cursor" and src["kind"] == "vendor import"
    assert src["unmapped_events"] == 1 and src["principals"] == 2
    assert src["tokens_in"] == 100 and src["tokens_out"] == 40
    who = {x["principal"]: x for x in o["actors"]}
    assert who["alice"]["mapped"] is True
    pseudo = next(x for x in o["actors"] if x["principal"].startswith("vendor-unmapped:"))
    assert pseudo["mapped"] is False
    assert "example.com" not in json.dumps(o)
    assert o["daily"] == [{"day": today.isoformat(), "events": 2, "cost_micros": 3_000_000}]
    assert "not part of governed spend" in o["note"]
    # the same read permission as the overview; anonymous is refused
    assert c.get("/admin/v1/observed").status_code == 401
    assert c.get("/admin/v1/observed", headers=H(VIEWER)).status_code == 200


def test_usage_breakdown_groups_by_person_project_application_and_model(env: Any) -> None:
    h, c = env
    activity(h)  # one settled call (120) by principal svc, and one refused call
    by = lambda dim: c.get(f"/admin/v1/usage/breakdown?by={dim}&days=7", headers=H()).json()  # noqa: E731
    person = by("principal")
    assert person["by"] == "principal" and person["total_cost_micros"] == 120
    (row,) = person["items"]
    assert row["key"] == "svc" and row["calls"] == 1 and row["cost_micros"] == 120
    assert row["tokens_in"] == 5 and row["tokens_out"] == 5 and row["refused"] == 1
    assert row["share_pct"] == 100.0 and row["estimated_cost_micros"] == 0
    for dim, key in [
        ("project", "proj-1"),
        ("app", "app-svc"),
        ("cost_centre", "CC-100"),
        ("team", "team-1"),
    ]:
        got = by(dim)["items"]
        assert [(i["key"], i["calls"], i["cost_micros"]) for i in got] == [(key, 1, 120)], dim
        assert got[0]["refused"] == 0  # refusals are only attributed to people
    assert by("model")["items"][0]["calls"] == 1
    assert c.get("/admin/v1/usage/breakdown?by=nonsense", headers=H()).status_code == 422
    assert c.get("/admin/v1/usage/breakdown").status_code == 401
    assert c.get("/admin/v1/usage/breakdown", headers=H(VIEWER)).status_code == 200


def test_csv_exports_neutralise_formulas_and_match_the_json(env: Any) -> None:
    h, c = env
    activity(h)
    r = c.get("/admin/v1/usage/breakdown.csv?by=principal&days=7", headers=H())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().split("\n")
    assert lines[0].startswith("key,calls,refused") and lines[1].startswith("svc,1,1,")
    ev = c.get("/admin/v1/events.csv?days=7", headers=H())
    header = ev.text.splitlines()[0]
    assert ev.status_code == 200 and header.startswith("recorded_at,who,headline,outcome,why")
    assert c.get("/admin/v1/events.csv").status_code == 401
    from captain.api.admin_http import _csv_cell

    assert _csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _csv_cell("@x") == "'@x"
    assert _csv_cell(None) == "" and _csv_cell(5) == 5 and _csv_cell("ok") == "ok"


def test_coverage_and_agents_views(env: Any) -> None:
    h, c = env
    activity(h)
    cov = c.get("/admin/v1/coverage?days=7", headers=H()).json()
    gov = next(x for x in cov["classes"] if x["coverage"] == "GOVERNED")
    assert gov["events"] >= 1 and cov["total_events"] >= gov["events"]
    assert {x["coverage"] for x in cov["classes"]} == {
        "GOVERNED",
        "OBSERVED",
        "BYPASS_DETECTED",
        "UNOBSERVED",
    }
    assert cov["by_channel"] and cov["by_person"][0]["principal_id"] == "svc"
    ag = c.get("/admin/v1/agents?days=7", headers=H()).json()
    assert ag and {"id", "tier", "autonomy", "allowed_tools", "calls", "tools_held"} <= set(ag[0])
    assert ag[0]["eval_scored"] == 0 and ag[0]["eval_mean"] is None
    # Live evaluation results are summarised per system (ADR-005).
    from captain.core.models import Channel, CoverageClass, EventType, UsageEvent

    rid = ag[0]["id"]
    for etype, details in [
        (EventType.EVALUATION_SCORED, {"overall": 0.9, "low": False}),
        (EventType.EVALUATION_SCORED, {"overall": 0.4, "low": True}),
        (EventType.EVALUATION_FAILED, {"error": "judge failed"}),
    ]:
        h.core.m.ledger.enqueue(
            UsageEvent(trace_id="t", event_type=etype, channel=Channel.API,
                       coverage=CoverageClass.GOVERNED, principal_id="svc",
                       register_id=rid, details=details)
        )  # fmt: skip
    h.core.m.ledger.write_pending()
    row = next(x for x in c.get("/admin/v1/agents?days=7", headers=H()).json() if x["id"] == rid)
    assert (row["eval_scored"], row["eval_mean"], row["eval_low"], row["eval_failed"]) == (
        2, 0.65, 1, 1,
    )  # fmt: skip
    assert c.get("/admin/v1/coverage").status_code == 401


def test_policy_view_and_simulator_are_read_only_questions(env: Any) -> None:
    h, c = env
    # The test double has no readable policy data: the view says so instead of guessing.
    assert c.get("/admin/v1/policy", headers=H()).status_code == 501
    h.core.m.policy.data = lambda path: {"bundle": {"version": "9"}, "tiers": {"T": 1}}.get(path)  # type: ignore[attr-defined]
    got = c.get("/admin/v1/policy", headers=H()).json()
    assert got["bundle"] == {"version": "9"} and got["tiers"] == {"T": 1}
    body = {"tier": "TIER_2", "autonomy": "A3", "tool": "x.write", "tool_impact": "high",
            "findings": [{"type": "SECRET", "score": 0.9}]}  # fmt: skip
    r = c.post("/admin/v1/policy/simulate", headers=H(), json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["simulated"] is True and d["decision"] and d["reason_code"] and d["policy_version"]
    before = len(h.built.infra.ledger_store.outbox)  # type: ignore[attr-defined]
    c.post("/admin/v1/policy/simulate", headers=H(), json=body)
    assert len(h.built.infra.ledger_store.outbox) == before  # type: ignore[attr-defined]
    assert (
        c.post("/admin/v1/policy/simulate", headers=H(), json={"tier": "TIER_9"}).status_code == 422
    )
    assert c.post("/admin/v1/policy/simulate", json=body).status_code == 401


def test_rate_limits_can_be_defined_and_listed_and_are_recorded(env: Any) -> None:
    h, c = env
    body = {"scope": "tool:demo.create_issue", "requests_per_minute": 5}
    assert c.put("/admin/v1/rate-limits", headers=H(VIEWER), json=body).status_code == 403
    assert c.put("/admin/v1/rate-limits", headers=H(), json=body).status_code == 200
    got = c.get("/admin/v1/rate-limits", headers=H()).json()
    assert got == [
        {"scope": "tool:demo.create_issue", "requests_per_minute": 5, "tokens_per_minute": None}
    ]
    bad = {"scope": "tool:x"}  # neither limit given
    assert c.put("/admin/v1/rate-limits", headers=H(), json=bad).status_code in (400, 422)
    assert (
        c.put(
            "/admin/v1/rate-limits",
            headers=H(),
            json={"scope": "no colon", "requests_per_minute": 1},
        ).status_code
        == 422
    )
    assert c.get("/admin/v1/rate-limits").status_code == 401
    kinds = [
        b.get("details", {}).get("kind")
        for (_p, _t, b) in h.built.infra.ledger_store.outbox.values()
    ]  # type: ignore[attr-defined]
    assert "rate_limit" in kinds


def test_a_rate_limit_can_be_lifted_and_that_is_recorded_first(env: Any) -> None:
    h, c = env
    put = {"scope": "tool:demo.x", "requests_per_minute": 3}
    c.put("/admin/v1/rate-limits", headers=H(), json=put)
    assert c.delete("/admin/v1/rate-limits?scope=tool:demo.x", headers=H(VIEWER)).status_code == 403
    assert c.delete("/admin/v1/rate-limits?scope=tool:demo.x", headers=H()).status_code == 200
    assert c.get("/admin/v1/rate-limits", headers=H()).json() == []
    assert c.delete("/admin/v1/rate-limits?scope=tool:demo.x", headers=H()).status_code == 404
    assert c.delete("/admin/v1/rate-limits?scope=bad", headers=H()).status_code == 422
    assert c.delete("/admin/v1/rate-limits?scope=tool:demo.x").status_code == 401
    kinds = [
        b.get("details", {}).get("kind")
        for (_p, _t, b) in h.built.infra.ledger_store.outbox.values()  # type: ignore[attr-defined]
    ]
    assert "rate_limit_removed" in kinds


def test_pilot_report_has_metrics_and_an_honest_acceptance_checklist(env: Any) -> None:
    h, c = env
    activity(h)
    rep = c.get("/admin/v1/reports/pilot?days=7", headers=H()).json()
    assert rep["volume"]["governed_decisions"] >= 1 and rep["cost"]["spend_micros"] == 120
    assert rep["cost"]["top_people"][0]["key"] == "svc"
    status = {r["criterion"]: r["status"] for r in rep["acceptance"]}
    assert status["Pilot metrics reported"] == "MET"
    assert status["Chargeback produced automatically"] == "MET"
    # Things the ledger cannot prove stay CHECK ELSEWHERE; absent examples are NOT SEEN.
    assert status["SSO and roles restrict access"] == "CHECK ELSEWHERE"
    assert status["Latency within target at pilot load"] == "CHECK ELSEWHERE"
    assert status["80% budget alert"] == "NOT SEEN"
    md = c.get("/admin/v1/reports/pilot?days=7&format=md", headers=H())
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert "## MVP acceptance (M1a)" in md.text and "CHECK ELSEWHERE" in md.text
    assert c.get("/admin/v1/reports/pilot").status_code == 401
