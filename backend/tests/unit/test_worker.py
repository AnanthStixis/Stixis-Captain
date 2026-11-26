from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from captain.config import ConfigError, load_settings
from captain.core.alerts import InMemoryAlertStore
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import AutonomyLevel, ContextHints, Credential, PrincipalType, RiskTier
from captain.workers.runner import CoreWorker, LocalLease, WorkerConfig
from tests.conftest import make_context, model_call, tool_call
from tests.harness.adapters import CaptainCoreHarness


def setup(name: str = "w") -> tuple[CaptainCoreHarness, CoreWorker]:
    h = CaptainCoreHarness()
    h.register_key(
        "k", make_context("svc", RiskTier.TIER_3, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    h.register_key(
        "a", make_context("agent", RiskTier.TIER_2, AutonomyLevel.A3, PrincipalType.AGENT)
    )
    w = CoreWorker(h.built, LocalLease(name), lambda t: ("cfo",),
                   WorkerConfig(escalate_after=timedelta(0)))  # fmt: skip
    return h, w


def test_tick_runs_all_jobs_and_drains_outbox() -> None:
    h, w = setup("drain")
    ctx = h.resolve(Credential(kind="virtual_key", secret="k"), ContextHints())
    for _ in range(3):
        h.core.decide(model_call(ctx))
    ran = w.tick(now=0)
    assert ran == ["ledger", "ledger_lag", "anchor", "sweep", "escalation", "alert_retry"]
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    assert not store.outbox and len(store.rows) == 3 and all(e.anchor_id for e in store.rows)
    assert w.tick(now=0.1) == []  # nothing due yet
    assert "ledger" in w.tick(now=1.0)
    w.lease.release()


def test_only_one_lease_holder() -> None:
    _, w1 = setup("shared")
    _, w2 = setup("shared")
    assert w1.tick(now=0) and w2.tick(now=0) == []
    w1.lease.release()
    assert w2.tick(now=0)
    w2.lease.release()


def test_lag_alert_raised_once() -> None:
    h, w = setup("lag")
    ctx = h.resolve(Credential(kind="virtual_key", secret="k"), ContextHints())
    old = model_call(ctx).model_copy(
        update={"received_at": datetime.now(UTC) - timedelta(minutes=5)}
    )
    h.core.decide(old)
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    eid = next(iter(store.outbox))
    p, t, b = store.outbox[eid]
    store.outbox[eid] = (
        p,
        t,
        {**b, "occurred_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()},
    )
    w.check_lag()
    w.check_lag()
    alerts = h.built.infra.alert_store
    assert isinstance(alerts, InMemoryAlertStore)
    assert [a.kind for a in alerts.items.values()] == ["ledger.lag"]
    w.lease.release()


def test_escalation_job_alerts_approvers() -> None:
    h, w = setup("esc")
    ctx = h.resolve(Credential(kind="virtual_key", secret="a"), ContextHints())
    req = tool_call(ctx, {"po": 1})
    h.core.request_approval(req, h.core.decide(req))
    assert w.escalate(lambda t: ("cfo",)) == 1
    alerts = h.built.infra.alert_store
    assert isinstance(alerts, InMemoryAlertStore)
    a = next(a for a in alerts.items.values() if a.kind == "approval.escalated")
    assert "cfo" in a.recipients
    w.lease.release()


def test_failing_job_does_not_stop_others() -> None:
    _, w = setup("fail")

    def boom() -> None:
        raise RuntimeError("x")

    w.jobs[2].run = boom  # anchor
    ran = w.tick(now=0)
    assert "anchor" not in ran and "sweep" in ran
    w.lease.release()


def test_settings_validation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    base = {
        "CAPTAIN_DATABASE_URL": "postgresql+psycopg://u@h/db",
        "CAPTAIN_KEY_PEPPER": "ab" * 32,
        "CAPTAIN_LEDGER_KEY": f"pem:{tmp_path / 'k.pem'}",
        "CAPTAIN_WORM_DIR": str(tmp_path / "worm"),
        "CAPTAIN_APPROVERS": '{"*": ["fin-1"]}',
        "CAPTAIN_OIDC_ISSUERS": '[{"issuer": "https://i", "audience": "captain", "jwks_uri": "https://i/jwks"}]',
    }
    s = load_settings(base)
    assert s.approvers == {"*": ("fin-1",)} and s.oidc_issuers[0].audience == "captain"
    for bad in ({"CAPTAIN_KEY_PEPPER": "abcd"}, {"CAPTAIN_KEY_PEPPER": "zz"},
                {"CAPTAIN_LEDGER_KEY": "kms:arn"}, {"CAPTAIN_DATABASE_URL": ""}):  # fmt: skip
        with pytest.raises(ConfigError):
            load_settings({**base, **bad})
