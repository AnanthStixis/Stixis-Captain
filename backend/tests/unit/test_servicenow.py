"""ServiceNow incident notifier (against a fake instance; no live ServiceNow here)."""

from __future__ import annotations

import json

import httpx
import pytest

from captain.core.alerts import Alert, Severity
from captain.core.servicenow import ServiceNowError, ServiceNowNotifier


def alert(sev: Severity = Severity.CRITICAL) -> Alert:
    return Alert("kill.switch", sev, "Kill switch engaged", {"scope": "channel", "n": 3})


def notifier(handler, **kw):  # type: ignore[no-untyped-def]
    c = httpx.Client(transport=httpx.MockTransport(handler))
    return ServiceNowNotifier("https://acme.service-now.com", "svc", "pw", client=c, **kw)


def test_critical_alert_opens_an_incident_with_the_alert_id() -> None:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, json={"result": {"number": "INC1"}})

    a = alert()
    notifier(h, assignment_group="AI Governance").send(a)
    (r,) = seen
    assert r.url.path == "/api/now/table/incident" and r.method == "POST"
    assert r.headers["authorization"].startswith("Basic ")
    body = json.loads(r.content)
    assert body["correlation_id"] == str(a.id) and body["urgency"] == "1"
    assert body["assignment_group"] == "AI Governance" and "scope: channel" in body["description"]


def test_alerts_below_the_threshold_open_nothing() -> None:
    def h(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no incident expected")

    notifier(h).send(alert(Severity.WARNING))
    notifier(h, min_severity=Severity.CRITICAL).send(alert(Severity.INFO))


def test_failures_raise_so_the_alert_is_retried() -> None:
    with pytest.raises(ServiceNowError):
        notifier(lambda r: httpx.Response(403)).send(alert())

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x")

    with pytest.raises(ServiceNowError):
        notifier(down).send(alert())


def test_url_must_be_https_and_credentials_present() -> None:
    with pytest.raises(ValueError):
        ServiceNowNotifier("http://acme.service-now.com", "u", "p")
    with pytest.raises(ValueError):
        ServiceNowNotifier("https://acme.service-now.com", "", "")
