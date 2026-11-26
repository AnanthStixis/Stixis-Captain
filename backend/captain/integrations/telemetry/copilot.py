"""GitHub Copilot per-user daily usage importer.

Source: GitHub's Copilot usage metrics reports API (documentation checked 2026-11):
`GET /orgs/{org}/copilot/metrics/reports/users-1-day?day=YYYY-MM-DD` answers with
`download_links` (signed URLs) and `report_day`. Headers: `Accept:
application/vnd.github+json` and `X-GitHub-Api-Version: 2026-03-10`; the token needs
the "View Organization Copilot Metrics" permission (classic: `read:org`) and the
organisation's Copilot usage metrics policy must be enabled.

Per-user records carry `day`, `user_login`, `code_generation_activity_count`,
`code_acceptance_activity_count`, `loc_added_sum`, `loc_deleted_sum`, `used_agent`,
`used_chat`, `used_cli`, `user_initiated_interaction_count` and `totals_by_ide`.
Copilot reports activity COUNTS, not tokens or cost, so these records carry no usage.

NOT VERIFIED: the documentation gives the record schema but not the file format behind a
download link. The reader accepts a JSON array, a JSON object holding a list, or one JSON
object per line, and refuses anything else rather than guessing. Check it against a real
report before relying on it.

Security: the download links are signed URLs on other hosts. The GitHub token is NEVER
sent to them, only to api.github.com, and only https links are followed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx

from captain.integrations.telemetry.base import Facts, ImportFailure, ObservedRecord

VENDOR = "github_copilot"
API_VERSION = "2026-03-10"
COUNTERS = (
    "code_generation_activity_count",
    "code_acceptance_activity_count",
    "loc_added_sum",
    "loc_deleted_sum",
    "user_initiated_interaction_count",
)
FLAGS = ("used_agent", "used_chat", "used_cli")


def parse_report(text: str) -> list[dict[str, Any]]:
    """JSON array, {"...": [..]} object, or one JSON object per line."""
    text = text.strip()
    if not text:
        return []
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, list):
        return [r for r in whole if isinstance(r, dict)]
    if isinstance(whole, dict):
        lists = [v for v in whole.values() if isinstance(v, list)]
        if lists and all(isinstance(r, dict) for r in lists[0]):
            return list(lists[0])
        if "user_login" in whole:
            return [whole]
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise ImportFailure("Copilot report file is not JSON or JSON lines") from e
        if isinstance(obj, dict):
            rows.append(obj)
    if not rows:
        raise ImportFailure("Copilot report file held no records")
    return rows


class CopilotUsers:
    vendor = VENDOR

    def __init__(
        self,
        org: str,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.startswith("https://") and "127.0.0.1" not in base_url:
            raise ValueError("the GitHub API must be reached over https")
        self._org, self._token, self._base = org, token, base_url.rstrip("/")
        self._c = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=5.0), follow_redirects=False, transport=transport
        )

    def _links(self, day: date) -> list[str]:
        try:
            r = self._c.get(
                f"{self._base}/orgs/{self._org}/copilot/metrics/reports/users-1-day",
                params={"day": day.isoformat()},
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
        except httpx.HTTPError as e:
            raise ImportFailure(f"GitHub unreachable: {type(e).__name__}") from e
        if r.status_code in (401, 403, 404):
            raise ImportFailure(
                f"GitHub answered HTTP {r.status_code} (check the token's Copilot metrics "
                "permission and that the usage metrics policy is enabled)"
            )
        if r.status_code != 200:
            raise ImportFailure(f"GitHub answered HTTP {r.status_code}")
        links = r.json().get("download_links") if isinstance(r.json(), dict) else None
        if not isinstance(links, list):
            raise ImportFailure("GitHub sent an unexpected response")
        return [str(x) for x in links]

    def _download(self, url: str) -> str:
        if urlsplit(url).scheme != "https":
            raise ImportFailure("refusing a non-https download link")
        try:
            # No Authorization header: signed URLs carry their own access.
            r = self._c.get(url)
        except httpx.HTTPError as e:
            raise ImportFailure(f"report download failed: {type(e).__name__}") from e
        if r.status_code != 200:
            raise ImportFailure(f"report download answered HTTP {r.status_code}")
        return r.text

    def fetch(self, day: date) -> Iterator[ObservedRecord]:
        for url in self._links(day):
            for row in parse_report(self._download(url)):
                login = row.get("user_login")
                if not isinstance(login, str):
                    continue
                facts: Facts = {k: row[k] for k in COUNTERS if isinstance(row.get(k), int)}
                facts.update({k: bool(row[k]) for k in FLAGS if k in row})
                yield ObservedRecord(
                    VENDOR, f"{row.get('day', day)}|{login}", day, login, facts=facts
                )
