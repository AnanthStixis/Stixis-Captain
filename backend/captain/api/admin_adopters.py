"""Admin API: adopters, preset downloads and install guides (plan step 4).

    GET /admin/v1/adopters                 catalog + whether each channel was used recently
    GET /admin/v1/downloads/{download_id}  a package or config file preset for this install
    GET /admin/v1/guides/{name}            install-and-test guide (markdown, addresses filled)

Which adopters the company chose is part of the company profile (PUT /admin/v1/organization,
field `adopters`). Downloads never contain a key.

Settings (environment): CAPTAIN_PUBLIC_GATEWAY_URL, CAPTAIN_PUBLIC_MCP_URL,
CAPTAIN_PUBLIC_CORE_URL, CAPTAIN_PUBLIC_CONSOLE_URL (how people reach this install),
CAPTAIN_ARTIFACTS_DIR (release packages; default: this repository), CAPTAIN_GUIDES_DIR
(default: docs/guides in this repository).
"""

# No `from __future__ import annotations`: FastAPI must resolve the local `Actor` alias.
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, Response

from captain.api.admin_queries import AdminQueries
from captain.api.adopters import (
    CATALOG,
    DOWNLOAD_IDS,
    REPO_ROOT,
    ArtifactMissingError,
    Urls,
    build_download,
    guide_text,
)
from captain.core.admin import AdminActor
from captain.core.identity.rbac import Permission, require
from captain.envfile import current_env

CALL_TYPES = {"call.allowed", "call.masked", "call.refused", "call.observed", "tool.held",
              "tool.executed"}  # fmt: skip


def settings_from_env(env: Mapping[str, str] | None = None) -> tuple[Urls, Path, Path]:
    e = env if env is not None else current_env()
    d = Urls()
    urls = Urls(
        gateway=(e.get("CAPTAIN_PUBLIC_GATEWAY_URL") or d.gateway).rstrip("/"),
        mcp=(e.get("CAPTAIN_PUBLIC_MCP_URL") or d.mcp).rstrip("/"),
        core=(e.get("CAPTAIN_PUBLIC_CORE_URL") or d.core).rstrip("/"),
        console=(e.get("CAPTAIN_PUBLIC_CONSOLE_URL") or d.console).rstrip("/"),
    )
    artifacts = Path(e.get("CAPTAIN_ARTIFACTS_DIR") or REPO_ROOT)
    guides = Path(e.get("CAPTAIN_GUIDES_DIR") or REPO_ROOT / "docs" / "guides")
    return urls, artifacts, guides


def install_adopter_routes(
    app: FastAPI,
    queries: AdminQueries,
    actor_dep: Callable[..., AdminActor],
    env: Mapping[str, str] | None = None,
) -> None:
    Actor = Annotated[AdminActor, Depends(actor_dep)]
    urls, artifacts, guides = settings_from_env(env)

    @app.get("/admin/v1/adopters")
    def adopters(a: Actor) -> dict[str, Any]:
        require(a.roles, Permission.REGISTER_READ)
        until = datetime.now(UTC)
        seen: Counter[str] = Counter()
        for _cov, channel, _who, n in queries.coverage_counts(
            until - timedelta(days=7), until, CALL_TYPES, {"admin", "security"}
        ):
            seen[str(channel)] += n
        return {
            "urls": {
                "gateway": urls.gateway,
                "mcp": urls.mcp,
                "core": urls.core,
                "console": urls.console,
            },  # fmt: skip
            "items": [
                {
                    "id": x.id,
                    "name": x.name,
                    "group": x.group,
                    "governs": x.governs,
                    "channel": x.channel,
                    "guide": x.guide,
                    "calls_last_7_days": seen.get(x.channel, 0),
                    "channel_shared": sum(1 for y in CATALOG if y.channel == x.channel) > 1,
                    "downloads": [
                        {"id": d.id, "label": d.label, "filename": d.filename, "note": d.note}
                        for d in x.downloads
                    ],
                }  # fmt: skip
                for x in CATALOG
            ],
        }

    @app.get("/admin/v1/downloads/{download_id}")
    def download(a: Actor, download_id: str) -> Response:
        require(a.roles, Permission.REGISTER_READ)
        if download_id not in DOWNLOAD_IDS:
            raise HTTPException(404, {"error": "NOT_FOUND"})
        try:
            data, name, media = build_download(download_id, artifacts, urls)
        except ArtifactMissingError as e:
            raise HTTPException(
                503, {"error": "NOT_BUILT", "message": f"{e}. Ask your platform team to build "
                                                       "the release packages."},
            ) from e  # fmt: skip
        return Response(
            data, media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )  # fmt: skip

    @app.get("/admin/v1/guides/{name}", response_class=PlainTextResponse)
    def guide(a: Actor, name: str) -> str:
        require(a.roles, Permission.REGISTER_READ)
        try:
            return guide_text(guides, name, urls)
        except KeyError as e:
            raise HTTPException(404, {"error": "NOT_FOUND"}) from e
