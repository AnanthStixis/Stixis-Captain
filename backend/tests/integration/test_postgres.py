"""Captain Core persistence against a real local PostgreSQL."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from captain.core.approvals.service import ApprovalService
from captain.core.budget.model import BudgetLevel
from captain.core.budget.store import ReservationRejected, UnknownReservation
from captain.core.bus import Topic
from captain.core.decision_api import ApprovalError, AuthenticationError
from captain.core.identity.model import PrincipalRecord
from captain.core.identity.service import IdentityService
from captain.core.ledger.chain import Ed25519Signer, KeyRingVerifier
from captain.core.ledger.service import InMemoryWormStore, LedgerService
from captain.core.models import (
    ApprovalState,
    Channel,
    CoverageClass,
    Decision,
    DecisionType,
    Environment,
    EventType,
    ReasonCode,
    RiskTier,
    UsageEvent,
)
from captain.core.usage.pricing import ModelPrice
from captain.db.bus import PostgresBus
from captain.db.stores import (
    PgApprovalStore,
    PgBudgetStore,
    PgIdentityStore,
    PgLedgerStore,
    load_price_catalog,
    save_price,
)
from tests.conftest import AGENT_KEY, tool_call
from tests.integration.conftest import URL

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------ ledger


def _ledger(pg: Engine) -> tuple[LedgerService, PgLedgerStore]:
    s = Ed25519Signer("k1")
    store = PgLedgerStore(pg)
    return LedgerService(
        store, InMemoryWormStore(), s, KeyRingVerifier({"k1": s.public_key()})
    ), store


def _ev(i: int, reg: str = "AI-1") -> UsageEvent:
    return UsageEvent(
        trace_id=f"t{i}",
        event_type=EventType.CALL_ALLOWED,
        channel=Channel.API,
        coverage=CoverageClass.GOVERNED,
        principal_id="u1",
        register_id=reg,
    )


def test_ledger_chain_anchor_verify(pg: Engine) -> None:
    svc, store = _ledger(pg)
    for i in range(6):
        svc.enqueue(_ev(i, "AI-1" if i % 2 else "AI-2"))
    assert svc.write_pending() == 6
    svc.enqueue(_ev(9))
    svc.write_pending()  # continues chain from the DB head
    a = svc.anchor()
    assert a is not None and a.ranges == {"AI-1": (1, 4), "AI-2": (1, 3)}
    assert store.unanchored() == []
    assert all(e.anchor_id == a.anchor_id for e in store.entries())
    assert svc.verify().ok


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE evidence_ledger SET event_type = 'x'",
        "DELETE FROM evidence_ledger",
        "TRUNCATE evidence_ledger",
    ],
)
def test_ledger_is_append_only_in_database(pg: Engine, sql: str) -> None:
    svc, _ = _ledger(pg)
    svc.enqueue(_ev(1))
    svc.write_pending()
    with pytest.raises(DBAPIError, match="append-only"), pg.begin() as c:
        c.execute(text(sql))


def test_break_glass_tamper_is_detected(pg: Engine) -> None:
    svc, _ = _ledger(pg)
    for i in range(3):
        svc.enqueue(_ev(i))
    svc.write_pending()
    with pg.begin() as c:  # owner disables the trigger and edits a record
        c.execute(text("ALTER TABLE evidence_ledger DISABLE TRIGGER USER"))
        c.execute(
            text(
                "UPDATE evidence_ledger SET body = jsonb_set(body, '{principal_id}', '\"x\"') "
                "WHERE seq = 2"
            )
        )
        c.execute(text("ALTER TABLE evidence_ledger ENABLE TRIGGER USER"))
    rep = svc.verify()
    assert not rep.ok and rep.first_problem and "AI-1#2" in rep.first_problem


# ------------------------------------------------------------------ budget


def test_budget_no_overshoot_under_concurrency(pg: Engine) -> None:
    store = PgBudgetStore(pg)
    levels = [BudgetLevel("org", 10_000_000, True), BudgetLevel("team", 1_000, True)]

    def go(_: int) -> bool:
        try:
            store.reserve(str(uuid4()), levels, 30, 600)
            return True
        except ReservationRejected:
            return False

    with ThreadPoolExecutor(16) as p:
        ok = sum(p.map(go, range(100)))
    assert ok == 33
    st = store.state("team")
    assert st.reserved == 990 and st.reserved + st.spent <= st.limit
    assert store.state("org").reserved == 990  # all-or-nothing across levels


def test_budget_settle_once_and_sweep(pg: Engine) -> None:
    store = PgBudgetStore(pg)
    store.reserve("r1", [BudgetLevel("b", 100, True)], 60, 600)
    store.settle("r1", 25)
    with pytest.raises(UnknownReservation):
        store.settle("r1", 25)
    assert (store.state("b").spent, store.state("b").reserved) == (25, 0)
    store.reserve("r2", [BudgetLevel("b", 100, True)], 10, 0)
    assert store.sweep() == 1 and store.state("b").reserved == 0


def test_rate_window(pg: Engine) -> None:
    store = PgBudgetStore(pg)
    assert all(store.hit_rate("k", 1, 10, 3, None) for _ in range(3))
    assert not store.hit_rate("k", 1, 10, 3, None)


# --------------------------------------------------------------- identity


def test_identity_and_cross_instance_revocation(pg: Engine) -> None:
    assert URL
    bus_a, bus_b = PostgresBus(URL), PostgresBus(URL)
    try:
        store = PgIdentityStore(pg)
        store.put_principal(PrincipalRecord(id="u1", type="USER", org_id="o1", display_name="U"))
        a = IdentityService(store, b"p" * 32, bus_a)
        b = IdentityService(
            store, b"p" * 32, bus_b, cache_ttl_s=3600
        )  # long TTL: only the bus can evict
        k = a.issue_key("u1", "AI-1", "p1", "a1", Environment.DEVELOPMENT, None)
        assert b.authenticate(k.raw_key).key_id == k.record.key_id  # cached on instance B
        a.revoke_key(k.record.key_id, "leak")
        deadline = time.monotonic() + 5  # CTO target: ≤ 5 s
        while True:
            try:
                b.authenticate(k.raw_key)
            except AuthenticationError:
                break
            assert time.monotonic() < deadline, "revocation did not propagate within 5 s"
            time.sleep(0.05)
        assert store.get_key(k.record.key_id).last_used_at is not None  # type: ignore[union-attr]
    finally:
        bus_a.close()
        bus_b.close()


def test_bus_ignores_unknown_topics(pg: Engine) -> None:
    assert URL
    bus = PostgresBus(URL)
    got: list[str] = []
    bus.subscribe(Topic.KILL_SWITCH, got.append)
    bus.publish(Topic.KILL_SWITCH, "principal:u1")
    deadline = time.monotonic() + 5
    while not got and time.monotonic() < deadline:
        time.sleep(0.05)
    bus.close()
    assert got == ["principal:u1"]


# -------------------------------------------------------------- approvals


def test_approval_compare_and_set(pg: Engine, core) -> None:  # type: ignore[no-untyped-def]
    from captain.core.models import ContextHints, Credential

    ctx = core.resolve(Credential(kind="virtual_key", secret=AGENT_KEY), ContextHints())
    svc = ApprovalService(PgApprovalStore(pg), lambda r: ("fin-1",), lambda t: None)
    req = tool_call(ctx, {"po": 1})
    d = Decision(
        request_id=req.request_id,
        trace_id="t",
        decision=DecisionType.REQUIRE_APPROVAL,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="x",
        policy_bundle="b",
        policy_version="1",
        risk_tier=RiskTier.TIER_2,
    )
    t = svc.request(req, d)
    svc.decide(t.ticket_id, True, "fin-1", "ok")
    with ThreadPoolExecutor(8) as p:  # 8 racing consumers, exactly one wins
        results = list(p.map(lambda _: _try(svc, t.ticket_id, req), range(8)))
    assert results.count(True) == 1
    assert svc.status(t.ticket_id).state is ApprovalState.CONSUMED


def _try(svc: ApprovalService, tid, req) -> bool:  # type: ignore[no-untyped-def]
    try:
        svc.consume(tid, req, recheck=lambda: None)
        return True
    except ApprovalError:
        return False


# ---------------------------------------------------------------- pricing


def test_price_versions_persist(pg: Engine) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    save_price(
        pg, ModelPrice("claude-sonnet", "anthropic", "2026-01", t0, 3_000_000, 15_000_000, 300_000)
    )
    with pytest.raises(DBAPIError):  # a version can never be overwritten
        save_price(pg, ModelPrice("claude-sonnet", "anthropic", "2026-01", t0, 1, 1))
    assert load_price_catalog(pg).by_version("claude-sonnet", "2026-01").input_per_m == 3_000_000


def test_workload_bindings_persist(pg: Engine) -> None:
    from captain.core.identity.oidc import WorkloadBinding
    from captain.db.stores import PgWorkloadBindings

    PgIdentityStore(pg).put_principal(
        PrincipalRecord(id="svc", type="APPLICATION", org_id="o1", display_name="s")
    )
    store = PgWorkloadBindings(pg)
    b = WorkloadBinding("https://iss", "sa:x", "svc", "AI-1", "p1", "a1", Environment.PRODUCTION)
    store.put(b)
    assert store.get("https://iss", "sa:x") == b
    assert store.get("https://iss", "sa:other") is None


def test_pg_lease_single_holder(pg: Engine) -> None:
    from captain.db.lease import PgAdvisoryLease

    assert URL
    a, b = PgAdvisoryLease(URL, "captain-test-lease"), PgAdvisoryLease(URL, "captain-test-lease")
    try:
        assert a.acquire() and a.acquire()  # re-entrant for the holder
        assert not b.acquire()
        a.release()
        assert b.acquire()
    finally:
        a.release()
        b.release()


def test_build_from_settings_end_to_end(pg: Engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Production wiring from settings: PostgreSQL stores, PEM signer, worker drains."""
    from captain.config import build_from_settings, load_settings
    from captain.core.ledger.signers import PemFileSigner
    from captain.workers.runner import CoreWorker, LocalLease

    assert URL
    PemFileSigner.generate(tmp_path / "ledger.pem")
    s = load_settings(
        {
            "CAPTAIN_DATABASE_URL": URL,
            "CAPTAIN_KEY_PEPPER": "cd" * 32,
            "CAPTAIN_LEDGER_KEY": f"pem:{tmp_path / 'ledger.pem'}",
            "CAPTAIN_WORM_DIR": str(tmp_path / "worm"),
        }
    )
    built = build_from_settings(s)
    built.core.m.ledger.enqueue(_ev(1))
    w = CoreWorker(built, LocalLease("e2e"), lambda t: ())
    try:
        assert "ledger" in w.tick(now=0)
        assert built.core.m.ledger.verify().ok
        assert list((tmp_path / "worm").iterdir())  # anchor written to WORM dir
    finally:
        w.lease.release()


def test_pg_alert_store_retry(pg: Engine) -> None:
    from captain.core.alerts import Alert, AlertService, Severity
    from captain.db.stores import PgAlertStore

    class Flaky:
        fail = True

        def send(self, a: Alert) -> None:
            if Flaky.fail:
                raise ConnectionError("smtp down")

    store = PgAlertStore(pg)
    svc = AlertService(store, [Flaky()], lambda e: None)
    a = svc.raise_alert(
        Alert("budget.threshold", Severity.WARNING, "80%", {"spent": 800}, recipients=("f@x",))
    )
    pending = store.undelivered(10)
    assert (
        [p.id for p in pending] == [a.id]
        and pending[0].attempts == 1
        and pending[0].details == {"spent": 800}
    )
    Flaky.fail = False
    assert svc.retry_undelivered() == 1 and store.undelivered(10) == []


def test_pg_registry_round_trip(pg: Engine) -> None:
    from captain.core.registry.service import (
        McpServer,
        ModelEntry,
        Provider,
        ProviderKind,
        RegistryService,
        Route,
        ToolEntry,
        ToolImpact,
        ToolRisk,
    )
    from captain.db.registry_store import PgRegistryStore

    r = RegistryService(PgRegistryStore(pg))
    r.upsert_provider(Provider("anthropic-eu", ProviderKind.ANTHROPIC, "eu", "vault:anthropic"))
    r.upsert_model(
        ModelEntry(
            "claude-sonnet",
            "anthropic-eu",
            "claude-sonnet-5-5",
            "eu",
            frozenset({"chat", "streaming"}),
        )
    )
    r.upsert_route(Route("chat", ("claude-sonnet",)))
    r.upsert_server(McpServer("erp", "https://erp/mcp", "o1"))
    schema = {
        "type": "object",
        "properties": {"amount": {"type": "integer", "maximum": 10}},
        "required": ["amount"],
    }
    r.upsert_tool(
        ToolEntry("erp.create_po", "erp", "PO", ToolImpact.HIGH, ToolRisk.CRITICAL, schema)
    )
    fresh = RegistryService(PgRegistryStore(pg))  # new instance reads from the database
    m = fresh.usable_model("claude-sonnet")
    assert m is not None and m.capabilities == frozenset({"chat", "streaming"})
    assert [t.alias for t in fresh.resolve_route("chat")] == ["claude-sonnet"]
    t = fresh.usable_tool("erp.create_po")
    assert t is not None and t.risk is ToolRisk.CRITICAL
    assert fresh.validate_arguments(t, {"amount": 99}) == ["amount: maximum violated"]


def test_pg_bus_survives_failing_subscriber(pg: Engine) -> None:
    assert URL
    bus = PostgresBus(URL)
    got: list[str] = []

    def bad(_: str) -> None:
        raise RuntimeError("buggy handler")

    bus.subscribe(Topic.KEY_REVOKED, bad)
    bus.subscribe(Topic.KEY_REVOKED, got.append)
    bus.publish(Topic.KEY_REVOKED, "k1")
    time.sleep(1.0)
    bus.publish(Topic.KEY_REVOKED, "k2")  # listener must still be alive
    deadline = time.monotonic() + 5
    while len(got) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    bus.close()
    assert got == ["k1", "k2"]


def test_pg_ledger_writer_recovers_after_crash(pg: Engine) -> None:
    svc, store = _ledger(pg)
    for i in range(3):
        svc.enqueue(_ev(i))
    real_done = store.outbox_done
    store.outbox_done = lambda ids: (_ for _ in ()).throw(RuntimeError("killed"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        svc.write_pending()
    store.outbox_done = real_done  # type: ignore[method-assign]
    svc.write_pending()  # before the fix: unique-key violation on every run
    assert len(store.entries()) == 3 and store.outbox_take(10) == [] and svc.verify().ok


def test_pg_budget_override_shared_across_instances(pg: Engine) -> None:
    from uuid import uuid4 as _u

    from captain.core.budget.model import BudgetDefinition, Period
    from captain.core.budget.service import BudgetService
    from captain.core.decision_api import BudgetExceededError
    from captain.db.stores import PgBudgetCatalog

    cat = PgBudgetCatalog(pg)
    cat.add(BudgetDefinition("team:t1", Period.MONTHLY, 100))
    a = BudgetService(cat, PgBudgetStore(pg), lambda x: None, lambda x: None)
    b = BudgetService(cat, PgBudgetStore(pg), lambda x: None, lambda x: None)
    a.settle(a.reserve(_u(), ("team:t1",), 100), 100)
    a.override("team:t1", 50, "finops", "quarter end")
    b.reserve(_u(), ("team:t1",), 40)
    with pytest.raises(BudgetExceededError):
        b.reserve(_u(), ("team:t1",), 20)


def test_pg_budget_alert_once_across_instances(pg: Engine) -> None:
    from uuid import uuid4 as _u

    from captain.core.budget.model import BudgetDefinition, Period
    from captain.core.budget.service import BudgetService
    from captain.db.stores import PgBudgetCatalog

    cat = PgBudgetCatalog(pg)
    cat.add(BudgetDefinition("team:t9", Period.MONTHLY, 100))
    alerts: list[object] = []
    a = BudgetService(cat, PgBudgetStore(pg), alerts.append, lambda x: None)
    b = BudgetService(cat, PgBudgetStore(pg), alerts.append, lambda x: None)  # second instance
    a.settle(a.reserve(_u(), ("team:t9",), 85), 85)  # crosses 80% on instance A
    b.settle(b.reserve(_u(), ("team:t9",), 5), 5)  # still above 80% on instance B
    assert len(alerts) == 1


def test_http_state_shared_across_api_replicas_without_content(pg: Engine) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import text as _t

    from captain.api.core_http import create_app
    from captain.api.state import PgStateStore
    from captain.core.models import AutonomyLevel, PrincipalType, RiskTier
    from tests.conftest import make_context
    from tests.harness.adapters import CaptainCoreHarness

    h = CaptainCoreHarness()
    h.register_key(
        "svc", make_context("svc", RiskTier.TIER_4, AutonomyLevel.A1, PrincipalType.APPLICATION)
    )
    h.set_budget("org:org-1", 10_000)
    auth = {"Authorization": f"Bearer {h._keys['svc']}"}  # noqa: SLF001
    replica_a = TestClient(create_app(h.core, PgStateStore(pg)))
    replica_b = TestClient(create_app(h.core, PgStateStore(pg)))
    d = replica_a.post(
        "/core/v1/decide",
        headers=auth,
        json={
            "channel": "API",
            "action": "MODEL_CALL",
            "model": "claude-sonnet",
            "max_output_tokens": 50,
            "contents": [{"text": "contact ravi@example.com please"}],
        },
    ).json()
    est = {"input_tokens": 1, "max_output_tokens": 1, "amount": 500, "price_version": "p"}
    r = replica_b.post(
        "/core/v1/reserve", headers=auth, json={"request_id": d["request_id"], "estimate": est}
    )
    assert r.status_code == 200
    actual = {
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "source": "ESTIMATED",
        "cost": 100,
        "price_version": "p",
    }
    s = replica_a.post(
        "/core/v1/settle",
        headers=auth,
        json={"reservation_id": r.json()["reservation_id"], "actual": actual},
    )
    assert s.json()["charged"] == 100
    with pg.connect() as c:
        stored = " ".join(str(row[0]) for row in c.execute(_t("SELECT doc FROM api_state")))
    assert "ravi@example.com" not in stored and "contact" not in stored


def test_pg_kill_switch_reaches_other_instance_fast(pg: Engine) -> None:
    from captain.core.identity.killswitch import KillScope, KillSwitch
    from captain.db.stores import PgKillSwitchStore

    assert URL
    bus_a, bus_b = PostgresBus(URL), PostgresBus(URL)
    try:
        a = KillSwitch(PgKillSwitchStore(pg), bus_a, cache_ttl_s=3600)
        b = KillSwitch(PgKillSwitchStore(pg), bus_b, cache_ttl_s=3600)  # only the bus can refresh
        assert b.match({KillScope.MODEL: "m"}) is None  # B caches "nothing active"
        t0 = time.monotonic()
        a.activate(KillScope.MODEL, "m", "incident", "admin")
        while b.match({KillScope.MODEL: "m"}) is None:
            assert time.monotonic() - t0 < 5, "kill switch did not reach instance B within 5 s"
            time.sleep(0.02)
    finally:
        bus_a.close()
        bus_b.close()


def test_pg_lease_detects_dead_session(pg: Engine) -> None:
    from captain.db.lease import PgAdvisoryLease

    assert URL
    a, b = PgAdvisoryLease(URL, "captain-dead-lease"), PgAdvisoryLease(URL, "captain-dead-lease")
    try:
        assert a.acquire()
        pid = a._conn.info.backend_pid  # type: ignore[union-attr]  # noqa: SLF001
        with pg.connect() as c:  # simulate the session being killed (network drop, failover)
            c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
            c.commit()
        assert b.acquire()  # lock released with the dead session; standby takes over
        assert not a.acquire()  # A notices it lost the lease instead of assuming it holds it
    finally:
        a.release()
        b.release()


def test_pg_state_claim_is_atomic(pg: Engine) -> None:
    from captain.api.state import PgStateStore

    s = PgStateStore(pg)
    with ThreadPoolExecutor(8) as p:
        wins = list(p.map(lambda _: s.claim("idempotency", "k1", "u1", {"r": 1}), range(8)))
    assert wins.count(True) == 1


def test_pg_admin_queries(pg: Engine) -> None:
    from captain.api.admin_queries import PgAdminQueries

    PgIdentityStore(pg).put_principal(
        PrincipalRecord(id="p1", type="USER", org_id="o", display_name="P")
    )
    svc, _ = _ledger(pg)
    svc.enqueue(_ev(1))
    svc.write_pending()
    q = PgAdminQueries(pg)
    assert [p.id for p in q.principals()] == ["p1"]
    assert q.keys() == [] and q.register_entries() == [] and q.alerts(5) == []
    assert len(q.events(None, None)) == 1 and q.registry("tool") == []


# ------------------------------------------------------- dashboard summaries


def test_database_side_summaries_equal_the_in_python_versions(pg: Engine) -> None:
    """The dashboard's heavy queries run in SQL; they must give exactly the answers the
    in-memory (reference) versions give over the same ledger."""
    from captain.api.admin_queries import (
        PgAdminQueries,
        coverage_from_entries,
        page_from_entries,
        partitions_from_entries,
    )

    svc, _ = _ledger(pg)
    shapes = [
        ("AI-1", "u1", EventType.CALL_ALLOWED, CoverageClass.GOVERNED, Channel.API),
        ("AI-1", "u2", EventType.CALL_REFUSED, CoverageClass.GOVERNED, Channel.IDE),
        ("AI-2", "u1", EventType.CALL_OBSERVED, CoverageClass.OBSERVED, Channel.HOOK),
        ("bypass:x", "u3", EventType.CALL_OBSERVED, CoverageClass.BYPASS_DETECTED, Channel.BROWSER),
        ("admin", "adm", EventType.KILL_SWITCH, CoverageClass.GOVERNED, Channel.API),
    ]
    for i in range(30):
        reg, who, et, cov, ch = shapes[i % len(shapes)]
        svc.enqueue(
            UsageEvent(
                trace_id=f"t{i}",
                event_type=et,
                channel=ch,
                coverage=cov,
                principal_id=who,
                register_id=reg,
                decision=None if et is not EventType.CALL_REFUSED else DecisionType.BLOCK,
            )
        )
    svc.write_pending()
    q = PgAdminQueries(pg)
    entries = q.events(None, None)
    types = {"call.allowed", "call.refused", "call.observed"}
    got_cov = sorted(q.coverage_counts(None, None, types, {"admin"}), key=repr)
    assert got_cov == sorted(coverage_from_entries(entries, types, {"admin"}), key=repr)
    assert sorted(q.partition_stats(None, None), key=repr) == sorted(
        partitions_from_entries(entries), key=repr
    )
    for kw in (
        dict(types=types, decision=None, principal_id=None, register_id=None),
        dict(types=None, decision="BLOCK", principal_id=None, register_id=None),
        dict(types=types, decision=None, principal_id="u1", register_id="AI-1"),
    ):
        total, rows = q.events_page(None, None, limit=7, offset=3, **kw)  # type: ignore[arg-type]
        want_total, want = page_from_entries(entries, limit=7, offset=3, **kw)  # type: ignore[arg-type]
        assert total == want_total
        assert {(r.partition, r.seq) for r in rows} == {(w.partition, w.seq) for w in want}


def test_overview_and_usage_aggregates_equal_the_in_python_versions(pg: Engine) -> None:
    from captain.api.admin_queries import (
        PgAdminQueries,
        latencies_from_entries,
        overview_from_entries,
        percentile,
        usage_from_entries,
    )
    from captain.core.models import ActualUsage, TokenUsage, UsageSource

    svc, _ = _ledger(pg)
    people = ["u1", "u2", "u3"]
    for i in range(45):
        who = people[i % 3]
        model = "claude-sonnet" if i % 2 else "vscode"
        svc.enqueue(
            UsageEvent(
                trace_id=f"s{i}",
                event_type=EventType.USAGE_SETTLED,
                channel=Channel.API,
                coverage=CoverageClass.GOVERNED,
                principal_id=who,
                register_id="AI-1",
                model=model,
                usage=ActualUsage(
                    usage=TokenUsage(input_tokens=10 + i, output_tokens=5 + i),
                    source=UsageSource.PROVIDER_REPORTED if i % 5 else UsageSource.ESTIMATED,
                    cost=100 + i,
                    price_version="v1",
                    latency_ms=(20 + 7 * i) if i % 4 else None,
                    time_to_first_token_ms=(10 + i) if i % 3 == 0 else None,
                    provider="mock",
                ),
                details={
                    "scopes": f"org:o,project:p{i % 2},app:a1,principal:{who},model:{model}"
                    if i % 9
                    else f"org:o,principal:{who}",  # some events carry no project / model scope
                    "risk_level": "LOW",
                },
            )
        )
        if i % 3 == 0:
            svc.enqueue(_ev(i, "AI-1"))
        if i % 7 == 0:
            svc.enqueue(
                UsageEvent(
                    trace_id=f"r{i}",
                    event_type=EventType.CALL_REFUSED,
                    channel=Channel.API,
                    coverage=CoverageClass.GOVERNED,
                    principal_id=who,
                    register_id="AI-1",
                    decision=DecisionType.BLOCK,
                    reason_code=ReasonCode.BUDGET_EXHAUSTED,
                )
            )
    svc.write_pending()
    q = PgAdminQueries(pg)
    entries = q.events(None, None)
    assert sorted(q.overview_rows(None, None), key=repr) == sorted(
        overview_from_entries(entries), key=repr
    )
    lat = latencies_from_entries(entries)
    p50, p95 = q.latency_percentiles(None, None)
    assert p50 == pytest.approx(percentile(lat, 0.5)) and p95 == pytest.approx(
        percentile(lat, 0.95)
    )
    ttft = latencies_from_entries(entries, "time_to_first_token_ms")
    t50, t95 = q.latency_percentiles(None, None, "time_to_first_token_ms")
    assert ttft and t50 == pytest.approx(percentile(ttft, 0.5))
    assert t95 == pytest.approx(percentile(ttft, 0.95))
    for by in ("principal", "project", "app", "cost_centre", "team", "model"):
        got = sorted(q.usage_by(by, None, None), key=repr)
        assert got == sorted(usage_from_entries(entries, by), key=repr), by


def test_admin_queries_keep_the_anchor_so_an_evidence_package_reports_it(pg: Engine) -> None:
    """Regression: the dashboard's ledger queries used to drop each entry's anchor, so an
    exported evidence package listed no anchors and called every event unanchored."""
    from datetime import UTC, datetime, timedelta

    from captain.api.admin_queries import PgAdminQueries
    from captain.core.reporting.evidence import export_evidence

    svc, _ = _ledger(pg)
    for i in range(4):
        svc.enqueue(_ev(i, "AI-1"))
    svc.write_pending()
    anchor = svc.anchor()
    assert anchor is not None
    svc.enqueue(_ev(99, "AI-1"))  # written after the anchor: genuinely unanchored
    svc.write_pending()
    q = PgAdminQueries(pg)
    entries = q.events(None, None)
    assert sum(1 for e in entries if e.anchor_id == anchor.anchor_id) == 4
    assert [e.anchor_id for e in entries if e.body["trace_id"] == "t99"] == [None]
    total, page = q.events_page(None, None, None, None, None, None, 50, 0)
    assert total == len(entries) and {p.anchor_id for p in page} >= {anchor.anchor_id, None}
    now = datetime.now(UTC)
    pkg = export_evidence(
        entries, "NIST_AI_RMF", now - timedelta(hours=1), now + timedelta(hours=1)
    )
    assert pkg.manifest["anchor_ids"] == [anchor.anchor_id]
    assert pkg.manifest["unanchored_events"] == 1


def test_subject_keys_create_once_resolve_erase_and_prune(pg: Engine) -> None:
    """Erasure keys (ADR-005) on PostgreSQL: concurrent creation yields one pseudonym,
    deleting the row erases the mapping, and old client-IP day keys are pruned."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime, timedelta

    from captain.core.subjects import ERASED, Pseudonymiser
    from captain.db.stores import PgSubjectStore

    stores = [PgSubjectStore(pg) for _ in range(8)]  # separate caches, like separate processes
    pzs = [Pseudonymiser(st, lambda _: True) for st in stores]
    with ThreadPoolExecutor(8) as ex:
        got = set(ex.map(lambda p: p.person("ravi@example.com"), pzs))
    assert len(got) == 1 and next(iter(got)).startswith("subj_")
    pseudo = next(iter(got))
    reader = Pseudonymiser(PgSubjectStore(pg), lambda _: True)
    assert reader.display(pseudo) == "ravi@example.com"
    assert reader.lookup("ravi@example.com") == pseudo
    assert pzs[0].ip("10.0.0.1") == pzs[1].ip("10.0.0.1")  # one day key shared by all
    assert reader.store.delete("ravi@example.com") == pseudo
    assert reader.display(pseudo) == ERASED
    assert reader.prune_ip_days(30, now=datetime.now(UTC) + timedelta(days=31)) == 1


def test_pg_scrubber_replaces_a_person_everywhere_in_one_go(pg: Engine) -> None:
    """Erasure of the editable records (ADR-005) on PostgreSQL: every table that can name
    a person is rewritten to the pseudonym; a longer id that contains it is left alone."""
    import json
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import insert, select

    from captain.db import schema as s
    from captain.db.scrub import PgScrubber

    who, near, new = "ravi@example.com", "ravi@example.com.au", "subj_abc"
    now = datetime.now(UTC)
    with pg.begin() as c:
        for pid in (who, near):
            row = {"id": pid, "type": "USER", "org_id": "o", "display_name": "Ravi",
                   "idp_subject": f"sub-{pid}"}  # fmt: skip
            c.execute(insert(s.principals).values(**row))
        c.execute(insert(s.virtual_keys).values(
            key_id="k1", secret_hash="0" * 64, principal_id=who, register_id="AI-1",
            project_id="p", application_id="a", environment="DEVELOPMENT", created_at=now,
            status="REVOKED", label="Ravi's laptop",
        ))  # fmt: skip
        c.execute(insert(s.approval_tickets).values(
            ticket_id="t1", state="PENDING", requested_by=who, updated_at=now,
            doc={"requested_by": who, "assigned_approvers": [who, "fin-1"]},
        ))  # fmt: skip
        c.execute(insert(s.alerts).values(
            id="a1", kind="budget.threshold", severity="WARNING", created_at=now,
            doc={"subject": f"Budget principal:{who} reached 80%", "recipients": [who],
                 "details": {"scope": f"principal:{who}"}},
        ))  # fmt: skip
        c.execute(
            insert(s.kill_switches).values(
                scope="principal", target=who, reason="x", actor_id="dev-admin", activated_at=now
            )
        )
        for scope in (f"principal:{who}", f"principal:{near}"):
            c.execute(insert(s.budget_definitions).values(
                scope=scope, period="MONTHLY", limit_micros=10, hard=True, alert_thresholds=["0.8"]
            ))  # fmt: skip
        c.execute(insert(s.rate_limits).values(scope=f"ptool:{who}:jira.x", requests_per_minute=5))
        bucket = f"principal:{who}|MONTHLY|2026-11"
        c.execute(insert(s.budget_buckets).values(bucket=bucket, limit_micros=10, spent_micros=7))
        c.execute(insert(s.budget_alert_marks).values(bucket=bucket, threshold="0.8", at=now))
        c.execute(insert(s.budget_reservations).values(
            reservation_id="r1", amount_micros=1, buckets=[bucket, "org:o|MONTHLY|2026-11"],
            expires_at=now + timedelta(minutes=5),
        ))  # fmt: skip
        c.execute(insert(s.api_state).values(kind="decision", key="d1", owner=who, doc={},
                                             expires_at=now + timedelta(hours=1)))  # fmt: skip

    changed = PgScrubber(pg).scrub(who, new)
    assert {"principals", "keys", "approval_tickets", "alerts", "kill_switches", "budgets",
            "rate_limits", "budget_counters", "api_state"} <= set(changed)  # fmt: skip

    with pg.connect() as c:
        dump = json.dumps([
            [dict(r) for r in c.execute(select(t)).mappings()]
            for t in (s.principals, s.virtual_keys, s.approval_tickets, s.alerts,
                      s.kill_switches, s.budget_definitions, s.rate_limits, s.budget_buckets,
                      s.budget_alert_marks, s.budget_reservations, s.api_state)
        ], default=str)  # fmt: skip
        erased = c.execute(select(s.principals).where(s.principals.c.id == new)).mappings().one()
        spent = c.execute(select(s.budget_buckets.c.spent_micros)).scalar_one()
    assert who not in dump.replace(near, "")
    assert near in dump  # the other person is untouched
    assert erased["display_name"] == "erased subject" and erased["idp_subject"] is None
    assert spent == 7  # spend kept under the new name


def test_a_person_recreated_after_erasure_gets_a_new_pseudonym_in_every_process(
    pg: Engine,
) -> None:
    """Regression (2026-11-26): the gateway cached person -> pseudonym, erasure ran in the
    Admin API, and a re-created person kept being written under the ERASED pseudonym."""
    from captain.core.subjects import ERASED, Pseudonymiser
    from captain.db.stores import PgSubjectStore

    gateway = Pseudonymiser(PgSubjectStore(pg), lambda _: True)  # one process
    admin = Pseudonymiser(PgSubjectStore(pg), lambda _: True)  # another process
    old = gateway.person("ravi@example.com")
    assert gateway.person("ravi@example.com") == old
    assert admin.store.delete("ravi@example.com") == old  # erased in the other process
    new = gateway.person("ravi@example.com")  # the same id, now a new person
    assert new != old and new.startswith("subj_")
    assert admin.display(old) == ERASED and admin.display(new) == "ravi@example.com"


def test_pg_secret_store_holds_only_ciphertext(pg: Engine) -> None:
    """Company AI keys on PostgreSQL (plan step 3): the row holds no readable key, the
    service decrypts it, and deleting the row destroys it."""
    from sqlalchemy import text as sql

    from captain.core.secrets_store import LocalKeyWrapper, SecretsService
    from captain.db.stores import PgSecretStore

    key = "fake-ai-key-for-pg-test-6789"
    svc = SecretsService(PgSecretStore(pg), LocalKeyWrapper(bytes(range(32))))
    stored = svc.put("openai", key, "priya@corp.example")
    with pg.connect() as c:
        raw = c.execute(sql("select * from provider_secrets")).mappings().one()
    assert key.encode() not in b"".join(
        bytes(v) for v in raw.values() if isinstance(v, bytes | memoryview)
    )
    assert raw["last4"] == "6789"
    fresh = SecretsService(PgSecretStore(pg), LocalKeyWrapper(bytes(range(32))))  # another process
    assert fresh.reveal(stored.id) == key
    assert [s.id for s in fresh.store.all()] == [stored.id]
    assert svc.delete(stored.id) and fresh.store.get(stored.id) is None


def test_pg_company_store_round_trip(pg: Engine) -> None:
    """Control plane companies on PostgreSQL (plan step 5): insert, update, list."""
    from captain.api.controlplane import PgCompanyStore

    st = PgCompanyStore(pg)
    st.put({"code": "abc", "status": "onboarding", "details": {"company_name": "ABC"}})
    st.put({"code": "xyz", "status": "active", "details": {"company_name": "XYZ"}})
    st.put({"code": "abc", "status": "active", "details": {"company_name": "ABC Pvt Ltd"}})
    got = st.get("abc")
    assert got is not None and got["status"] == "active"
    assert got["details"]["company_name"] == "ABC Pvt Ltd"
    assert [d["code"] for d in st.all()] == ["abc", "xyz"] and st.get("nope") is None
