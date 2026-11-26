"""Admin API: company profile, users and the org-admin invite (inside one install).

    GET/PUT  /admin/v1/organization          company profile + onboarding checklist
    GET/POST /admin/v1/users                 people who use the console
    PUT      /admin/v1/users/{user_id}       name, team, console roles, active
    POST     /admin/v1/invites               one-time org-admin link (before SSO only)
    POST     /admin/v1/auth/invite/redeem    turns the link into a console session

People are principals of type USER whose id is their (lowercased) email. Every change is
recorded in the evidence ledger (the ledger pseudonymises people when erasure is on).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from captain.api.admin_queries import AdminQueries
from captain.api.org import (
    CHECKLIST_ITEMS,
    INVITE_SESSION_S,
    InMemoryOrganizationStore,
    InviteError,
    InviteService,
    Organization,
    OrganizationStore,
    normalise_email,
)
from captain.core.admin import AdminActor, AdminService
from captain.core.decision_api import AuthenticationError
from captain.core.engine import admin_event
from captain.core.factory import BuiltCore
from captain.core.identity.model import KeyStatus, PrincipalRecord
from captain.core.identity.rbac import ROLE_PERMISSIONS, Permission, permissions_for, require
from captain.core.models import EventType, PrincipalType

if TYPE_CHECKING:
    from captain.api.admin_sso import SsoService

DEFAULT_ORG_ID = "default"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewUserBody(_Body):
    email: str = Field(min_length=3, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    team: str | None = Field(default=None, max_length=128)
    roles: list[str] = Field(default_factory=list, max_length=10)


class UpdateUserBody(_Body):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    team: str | None = Field(default=None, max_length=128)
    roles: list[str] | None = Field(default=None, max_length=10)
    active: bool | None = None


class InviteBody(_Body):
    email: str = Field(min_length=3, max_length=128)


class RedeemBody(_Body):
    token: str = Field(min_length=10, max_length=200)


def user_view(p: PrincipalRecord) -> dict[str, Any]:
    return {
        "id": p.id,
        "email": p.id,
        "name": p.display_name,
        "team": p.team_id,
        "roles": sorted(p.roles),
        "active": p.active,
    }


def install_org_routes(
    app: FastAPI,
    built: BuiltCore,
    admin: AdminService,
    queries: AdminQueries,
    actor_dep: Callable[..., AdminActor],
    org_store: OrganizationStore | None,
    invites: InviteService | None,
    sso: "SsoService | None",
    session_of: Callable[[Request], tuple[AdminActor, str] | None],
) -> None:
    store: OrganizationStore = org_store or InMemoryOrganizationStore()
    ids = built.infra.identity_store
    core = built.core
    Actor = Annotated[AdminActor, Depends(actor_dep)]

    def record(kind: EventType, actor_id: str, details: dict[str, Any]) -> None:
        core.record(admin_event(kind, actor_id, details))

    @app.exception_handler(InviteError)
    async def _invite(_: Request, e: InviteError) -> JSONResponse:
        return JSONResponse({"error": e.code.upper(), "message": str(e)}, status_code=409)

    # ------------------------------------------------------------ organization
    def org_view() -> dict[str, Any]:
        org, at, by = store.get()
        done = dict(org.checklist)
        done["sso"] = sso is not None  # known from the running install, never stored
        return {
            **org.model_dump(),
            "updated_at": at.isoformat() if at else None,
            "updated_by": by,
            "sso_configured": sso is not None,
            "checklist_items": [
                {"key": k, "label": label, "done": bool(done.get(k))}
                for k, label in CHECKLIST_ITEMS
            ],
        }

    @app.get("/admin/v1/organization")
    def get_org(a: Actor) -> dict[str, Any]:
        return org_view()  # any signed-in console user may see the company profile

    @app.put("/admin/v1/organization")
    def put_org(a: Actor, body: Organization) -> dict[str, Any]:
        require(a.roles, Permission.ORG_WRITE)
        body.checklist.pop("sso", None)
        record(EventType.ORG_CHANGED, a.id, {"fields": ",".join(sorted(body.model_fields_set))})
        store.put(body, a.id)
        return org_view()

    # ------------------------------------------------------------------ users
    def org_id() -> str:
        for p in queries.principals():
            return p.org_id
        return DEFAULT_ORG_ID

    def check_roles(a: AdminActor, roles: list[str]) -> frozenset[str]:
        unknown = [r for r in roles if r not in ROLE_PERMISSIONS]
        if unknown:
            raise ValueError(f"unknown role(s) {unknown}; choose from {sorted(ROLE_PERMISSIONS)}")
        wanted = frozenset(roles)
        # No escalation: nobody can grant a permission they do not hold themselves.
        beyond = permissions_for(wanted) - permissions_for(a.roles)
        if beyond:
            raise HTTPException(
                403,
                {"error": "FORBIDDEN",
                 "message": f"you cannot grant {sorted(p.value for p in beyond)}"},
            )  # fmt: skip
        return wanted

    def get_user(user_id: str) -> PrincipalRecord:
        p = ids.get_principal(user_id.strip().lower())
        if p is None or str(p.type) != PrincipalType.USER.value:
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no such user"})
        return p

    @app.get("/admin/v1/users")
    def list_users(a: Actor) -> list[dict[str, Any]]:
        require(a.roles, Permission.KEYS_READ)
        return [
            user_view(p) for p in queries.principals() if str(p.type) == PrincipalType.USER.value
        ]

    @app.post("/admin/v1/users")
    def add_user(a: Actor, body: NewUserBody) -> dict[str, Any]:
        require(a.roles, Permission.USERS_WRITE)
        email = normalise_email(body.email)
        roles = check_roles(a, body.roles)
        if ids.get_principal(email) is not None:
            raise HTTPException(409, {"error": "EXISTS", "message": "this user already exists"})
        rec = PrincipalRecord(
            id=email,
            type=PrincipalType.USER.value,
            org_id=org_id(),
            display_name=body.name.strip(),
            team_id=(body.team or "").strip() or None,
            roles=roles,
        )
        record(
            EventType.USER_CHANGED,
            a.id,
            {"user": email, "action": "created", "roles": ",".join(sorted(roles))},
        )
        ids.put_principal(rec)
        return user_view(rec)

    @app.put("/admin/v1/users/{user_id}")
    def update_user(a: Actor, user_id: str, body: UpdateUserBody) -> dict[str, Any]:
        require(a.roles, Permission.USERS_WRITE)
        old = get_user(user_id)
        roles = check_roles(a, body.roles) if body.roles is not None else old.roles
        if old.id == a.id.strip().lower():
            if body.active is False:
                raise ValueError("you cannot deactivate yourself")
            if Permission.USERS_WRITE not in permissions_for(roles):
                raise ValueError("you cannot remove your own user management")
        new = PrincipalRecord(
            id=old.id,
            type=old.type,
            org_id=old.org_id,
            display_name=body.name.strip() if body.name is not None else old.display_name,
            team_id=((body.team or "").strip() or None)
            if "team" in body.model_fields_set
            else old.team_id,
            idp_subject=old.idp_subject,
            roles=roles,
            active=old.active if body.active is None else body.active,
        )
        details: dict[str, Any] = {"user": old.id, "roles": ",".join(sorted(roles))}
        if old.active and not new.active:
            # Safety-increasing: take effect first (record, then revoke every live key).
            ids.put_principal(new)
            now = datetime.now(UTC)
            revoked = 0
            for k in ids.keys_for_principal(old.id):
                if k.status is KeyStatus.ACTIVE and k.is_usable(now):
                    admin.revoke_key(a, k.key_id, "user deactivated")
                    revoked += 1
            record(
                EventType.USER_CHANGED,
                a.id,
                {**details, "action": "deactivated", "keys_revoked": revoked},
            )
            return {**user_view(new), "keys_revoked": revoked}
        change = "reactivated" if new.active and not old.active else "updated"
        record(EventType.USER_CHANGED, a.id, {**details, "action": change})
        ids.put_principal(new)
        return user_view(new)

    # ---------------------------------------------------------------- invites
    def need_invites() -> InviteService:
        if invites is None:
            raise HTTPException(
                501, {"error": "NOT_CONFIGURED", "message": "invites are not enabled"}
            )
        return invites

    @app.post("/admin/v1/invites")
    def create_invite(a: Actor, body: InviteBody) -> dict[str, Any]:
        """Platform / bootstrap use: a one-time link that signs the company's first org
        admin in before SSO is connected. The raw token is returned once."""
        require(a.roles, Permission.USERS_WRITE)
        svc = need_invites()
        email = normalise_email(body.email)
        token, exp = svc.create(email, a.id)
        record(EventType.INVITE_CREATED, a.id, {"email": email, "role": "org-admin"})
        return {
            "link_path": f"/accept-invite#token={token}",
            "expires_at": datetime.fromtimestamp(exp, UTC).isoformat(),
            "notice": "Send this link to the org admin. It works once, for 7 days, and stops "
            "working when SSO is configured.",
        }

    @app.post("/admin/v1/auth/invite/redeem")
    def redeem_invite(body: RedeemBody) -> Response:
        svc = need_invites()
        a, sid, csrf = svc.redeem(body.token)
        # The invited person becomes a Captain user with org-admin, so the role survives
        # the switch to SSO (it is added to whatever their IdP groups grant).
        p = ids.get_principal(a.id)
        if p is None:
            p = PrincipalRecord(
                id=a.id,
                type=PrincipalType.USER.value,
                org_id=org_id(),
                display_name=a.id.split("@", 1)[0],
                roles=frozenset({"org-admin"}),
            )
        else:
            p = PrincipalRecord(
                p.id, p.type, p.org_id, p.display_name, p.team_id, p.idp_subject,
                p.roles | {"org-admin"}, True,
            )  # fmt: skip
        ids.put_principal(p)
        record(EventType.INVITE_REDEEMED, a.id, {"role": "org-admin"})
        r = JSONResponse(
            {
                "id": a.id,
                "roles": sorted(a.roles),
                "permissions": sorted(x.value for x in permissions_for(a.roles)),
                "csrf_token": csrf,
            }
        )
        r.set_cookie(
            svc.cookie_name,
            sid,
            max_age=INVITE_SESSION_S,
            path="/",
            httponly=True,
            samesite="strict",
            secure=svc.secure_cookies,
        )
        return r

    if sso is None and invites is not None:
        # Without SSO the console still needs to discover / end an invite session.
        @app.get("/admin/v1/auth/session")
        def session(request: Request) -> dict[str, Any]:
            s = session_of(request)
            if s is None:
                raise AuthenticationError("sign in required")
            a, csrf = s
            return {
                "id": a.id,
                "roles": sorted(a.roles),
                "permissions": sorted(p.value for p in permissions_for(a.roles)),
                "csrf_token": csrf,
            }

        @app.post("/admin/v1/auth/logout")
        def logout(request: Request) -> Response:
            svc = need_invites()
            sid = request.cookies.get(svc.cookie_name)
            s = svc.session(sid)
            if s is not None and sid:
                svc.check_csrf(request.method, request.headers, s[1])
                svc.end(sid)
            r = JSONResponse({"status": "signed out"})
            r.delete_cookie(svc.cookie_name, path="/", httponly=True, samesite="strict")
            return r
