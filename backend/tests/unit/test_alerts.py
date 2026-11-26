from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from captain.core.alerts import (
    Alert,
    AlertService,
    EmailNotifier,
    InMemoryAlertStore,
    Severity,
)
from captain.core.models import AutonomyLevel, RiskTier
from tests.harness.adapters import CaptainCoreHarness


class FakeSMTP:
    sent: list[Any] = []

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port, self.tls = host, port, False

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def starttls(self) -> None:
        self.tls = True

    def login(self, u: str, p: str) -> None:
        pass

    def send_message(self, msg: Any) -> None:
        assert self.tls, "must use STARTTLS"
        FakeSMTP.sent.append(msg)


class Flaky:
    def __init__(self) -> None:
        self.fail = True

    def send(self, a: Alert) -> None:
        if self.fail:
            raise ConnectionError("smtp down")


def test_email_delivery() -> None:
    FakeSMTP.sent.clear()
    n = EmailNotifier("smtp.example", 587, "captain@example.com", smtp_factory=FakeSMTP)  # type: ignore[arg-type]
    a = Alert("budget.threshold", Severity.WARNING, "Budget team:t1 reached 80%", {"spent": 800},
              recipients=("finops@example.com",))  # fmt: skip
    n.send(a)
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "finops@example.com" and "[Captain WARNING]" in msg["Subject"]


def test_failed_delivery_is_kept_and_retried() -> None:
    store, flaky, recorded = InMemoryAlertStore(), Flaky(), []
    svc = AlertService(store, [flaky], recorded.append)
    a = svc.raise_alert(Alert("x", Severity.INFO, "s", {}))
    assert recorded and not store.items[a.id].delivered  # recorded in ledger even if not sent
    flaky.fail = False
    assert svc.retry_undelivered() == 1 and store.items[a.id].delivered


def test_alert_evidence_failure_does_not_lose_alert() -> None:
    store = InMemoryAlertStore()

    def broken(_: object) -> None:
        raise ConnectionError("outbox down")

    a = AlertService(store, [], broken).raise_alert(Alert("x", Severity.INFO, "s", {}))
    assert a.id in store.items


def test_core_budget_threshold_raises_alert_with_recipients() -> None:
    from captain.core.models import PrincipalType
    from tests.conftest import make_context

    h = CaptainCoreHarness()
    infra = h.built.infra
    store = infra.alert_store
    assert isinstance(store, InMemoryAlertStore)
    svc = h.built.alert_service
    svc._recipients = lambda a: ("finops@example.com",)  # noqa: SLF001
    h.register_key(
        "k", make_context("svc", RiskTier.TIER_3, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    h.set_budget("org:org-1", 100)
    r = h.core.m.budget.reserve(uuid4(), ("org:org-1",), 90)
    h.core.m.budget.settle(r, 90)
    alerts = list(store.items.values())
    assert [a.kind for a in alerts] == ["budget.threshold"]
    assert alerts[0].recipients == ("finops@example.com",) and alerts[0].delivered


@pytest.mark.parametrize("approval", [None, "APR-7"])
def test_register_downgrade_alert(approval: str | None) -> None:
    from captain.core.context.service import ControlDowngradeError
    from captain.core.models import PrincipalType
    from tests.conftest import make_context

    h = CaptainCoreHarness()
    ctx = make_context("svc", RiskTier.TIER_2, AutonomyLevel.A2, PrincipalType.APPLICATION)
    h.register_key("k", ctx)
    weaker = ctx.register_entry.model_copy(update={"risk_tier": RiskTier.TIER_4})
    store = h.built.infra.alert_store
    assert isinstance(store, InMemoryAlertStore)
    if approval is None:
        with pytest.raises(ControlDowngradeError):
            h.built.register.upsert(weaker)
        assert not store.items
    else:
        h.built.register.upsert(weaker, approval)
        assert [a.severity for a in store.items.values()] == [Severity.CRITICAL]
