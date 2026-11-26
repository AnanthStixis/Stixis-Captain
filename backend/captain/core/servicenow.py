"""ServiceNow incidents from Captain alerts (a Notifier).

Uses ServiceNow's Table API:  POST {instance}/api/now/table/incident  with a JSON body and
HTTP basic authentication for an integration user. Only alerts at or above `min_severity`
open an incident (default CRITICAL), so the queue is not flooded. The Captain alert id is
sent as `correlation_id`, so an administrator can trace an incident back to the alert.

Never in an incident: prompt or response content, keys, or secrets. Alert details are
already non-sensitive by construction (ids, counts, reason codes).

Delivery follows the alert service rules: a failure raises, the alert is kept undelivered
and retried by the worker. Not verified against a live ServiceNow instance.
"""

from __future__ import annotations

import httpx

from captain.core.alerts import Alert, Severity

_ORDER = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
# ServiceNow: urgency and impact are 1 (high) .. 3 (low).
_LEVEL = {Severity.CRITICAL: "1", Severity.WARNING: "2", Severity.INFO: "3"}


class ServiceNowError(Exception):
    pass


class ServiceNowNotifier:
    def __init__(
        self,
        instance_url: str,
        username: str,
        password: str,
        min_severity: Severity = Severity.CRITICAL,
        assignment_group: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        base = instance_url.rstrip("/")
        local = base.startswith(("http://127.0.0.1", "http://localhost"))
        if not base.startswith("https://") and not local:
            raise ValueError("ServiceNow URL must use https")
        if not username or not password:
            raise ValueError("ServiceNow needs an integration user and password")
        self._url = f"{base}/api/now/table/incident"
        self._auth = (username, password)
        self._min, self._group = min_severity, assignment_group
        self._http = client or httpx.Client(timeout=10.0, follow_redirects=False)

    def send(self, a: Alert) -> None:
        if _ORDER[a.severity] < _ORDER[self._min]:
            return
        body: dict[str, str] = {
            "short_description": f"[Captain {a.severity}] {a.subject}"[:160],
            "description": "\n".join(
                [f"Type: {a.kind}", *(f"{k}: {v}" for k, v in sorted(a.details.items()))]
            )[:4000],
            "urgency": _LEVEL[a.severity],
            "impact": _LEVEL[a.severity],
            "category": "software",
            "correlation_id": str(a.id),
        }
        if self._group:
            body["assignment_group"] = self._group
        try:
            r = self._http.post(
                self._url, json=body, auth=self._auth, headers={"Accept": "application/json"}
            )
        except httpx.HTTPError as e:
            raise ServiceNowError(f"ServiceNow unreachable: {type(e).__name__}") from e
        if r.status_code not in (200, 201):
            raise ServiceNowError(f"ServiceNow refused the incident: HTTP {r.status_code}")
