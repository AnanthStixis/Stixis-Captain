"""Platform endpoints on a client install, called only by the Stixis control plane (step 5).

    POST /admin/v1/platform/bootstrap   company profile + first org admin + one-time invite
    GET  /admin/v1/platform/summary     health and counts for Stixis's company list

Authentication: header `X-Captain-Platform-Token` must equal `CAPTAIN_PLATFORM_TOKEN`, set
on the install (from its Key Vault) by whoever installs it. With no token configured these
endpoints do not exist (404). The summary holds counts and names of settings only: no
evidence, no prompts, no people beyond the number of users.
"""

# No `from __future__ import annotations`: FastAPI must resolve the request models.
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from captain.api.admin_org import DEFAULT_ORG_ID
from captain.api.admin_queries import AdminQueries
from captain.api.org import (
    InviteError,
    InviteService,
    Organization,
    OrganizationStore,
    normalise_email,
)
from captain.core.engine import admin_event
from captain.core.factory import BuiltCore
from captain.core.identity.model import PrincipalRecord
from captain.core.models import EventType, PrincipalType
from captain.envfile import current_env

PLATFORM_ACTOR = "stixis-platform"
CALL_TYPES = {"call.allowed", "call.masked", "call.refused", "tool.held", "tool.executed"}


class BootstrapBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: Organization
    org_admin_email: str = Field(min_length=3, max_length=128)
    org_admin_name: str = Field(default="", max_length=200)


def install_platform_routes(
    app: FastAPI,
    built: BuiltCore,
    queries: AdminQueries,
    org_store: OrganizationStore,
    invites: InviteService | None,
    env: Mapping[str, str] | None = None,
) -> None:
    e = env if env is not None else current_env()
    token = e.get("CAPTAIN_PLATFORM_TOKEN") or ""
    ids = built.infra.identity_store

    def check(given: str | None) -> None:
        if not token:
            raise HTTPException(404, {"error": "NOT_FOUND"})
        if not given or not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, {"error": "UNAUTHENTICATED", "message": "platform token"})

    def record(kind: EventType, details: dict[str, Any]) -> None:
        built.core.record(admin_event(kind, PLATFORM_ACTOR, details))

    PlatformToken = Annotated[str | None, Header(alias="X-Captain-Platform-Token")]

    @app.post("/admin/v1/platform/bootstrap")
    def bootstrap(
        body: BootstrapBody, x_captain_platform_token: PlatformToken = None
    ) -> dict[str, Any]:
        check(x_captain_platform_token)
        try:
            email = normalise_email(body.org_admin_email)
        except ValueError as e:
            raise HTTPException(400, {"error": "BAD_EMAIL", "message": str(e)}) from e
        current, _, _ = org_store.get()
        company = body.company.model_copy(
            update={"adopters": current.adopters, "checklist": current.checklist}
        )  # the company's own choices are theirs; Stixis sets the details only
        record(EventType.ORG_CHANGED, {"fields": "company profile (from Stixis)"})
        org_store.put(company, PLATFORM_ACTOR)

        existing = ids.get_principal(email)
        if existing is None:
            org = next((p.org_id for p in queries.principals()), DEFAULT_ORG_ID)
            record(
                EventType.USER_CHANGED, {"user": email, "action": "created", "roles": "org-admin"}
            )
            ids.put_principal(PrincipalRecord(
                id=email, type=PrincipalType.USER.value, org_id=org,
                display_name=body.org_admin_name.strip() or email.split("@", 1)[0],
                roles=frozenset({"org-admin"}),
            ))  # fmt: skip
        elif "org-admin" not in existing.roles:
            from dataclasses import replace

            roles = existing.roles | {"org-admin"}
            record(EventType.USER_CHANGED, {"user": email, "action": "updated",
                                            "roles": ",".join(sorted(roles))})  # fmt: skip
            ids.put_principal(replace(existing, roles=frozenset(roles), active=True))

        if invites is None:
            return {"status": "ok", "invite": None,
                    "message": "Invites are not configured on this install."}  # fmt: skip
        try:
            raw, exp = invites.create(email, PLATFORM_ACTOR)
        except InviteError as e:  # SSO already on: the org admin signs in with SSO
            return {"status": "ok", "invite": None, "message": str(e)}
        record(EventType.INVITE_CREATED, {"email": email, "role": "org-admin"})
        return {
            "status": "ok",
            "invite": {
                "link_path": f"/accept-invite#token={raw}",
                "expires_at": datetime.fromtimestamp(exp, UTC).isoformat(),
            },
        }

    @app.get("/admin/v1/platform/summary")
    def summary(x_captain_platform_token: PlatformToken = None) -> dict[str, Any]:
        check(x_captain_platform_token)
        org, updated_at, _ = org_store.get()
        until = datetime.now(UTC)
        calls = sum(
            n for _c, _ch, _p, n in queries.coverage_counts(
                until - timedelta(days=7), until, CALL_TYPES, {"admin", "security"})
        )  # fmt: skip
        principals = queries.principals()
        keys = [k for k in queries.keys() if str(k.status.value) == "ACTIVE"]
        return {
            "company_name": org.company_name,
            "adopters": org.adopters,
            "checklist_done": sum(1 for v in org.checklist.values() if v),
            "users": sum(1 for p in principals if str(p.type) == PrincipalType.USER.value),
            "org_admins": sum(1 for p in principals if "org-admin" in p.roles),
            "active_keys": len(keys),
            "calls_last_7_days": calls,
            "profile_updated_at": updated_at.isoformat() if updated_at else None,
            "checked_at": until.isoformat(),
        }
