"""Admin API: company profile, users and the org-admin invite (inside one install).

    GET/PUT  /admin/v1/organization          company profile + onboarding checklist
    GET/POST /admin/v1/users                 people who use the console
    PUT      /admin/v1/users/{user_id}       name, team, console roles, active
    POST     /admin/v1/invites               one-time org-admin link (before SSO only)
    POST     /admin/v1/auth/invite/redeem    turns the link into a console session

People are principals of type USER whose id is their (lowercased) email. Every change is
recorded in the evidence ledger (the ledger pseudonymises people when erasure is on).
"""

import dataclasses
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from captain.api.admin_queries import AdminQueries, same_team, team_names
from captain.api.org import (
    CHECKLIST_ITEMS,
    INVITE_SESSION_S,
    SIGNIN_LINK_TTL_S,
    InMemoryOrganizationStore,
    InviteError,
    InviteService,
    Organization,
    OrganizationStore,
    normalise_email,
)
from captain.api.signin_mail import SignInMailer
from captain.api.sso_live import SsoSetupError
from captain.core.admin import AdminActor, AdminService
from captain.core.engine import admin_event
from captain.core.factory import BuiltCore
from captain.core.identity.model import KeyStatus, PrincipalRecord
from captain.core.identity.rbac import ROLE_PERMISSIONS, Permission, permissions_for, require
from captain.core.models import EventType, PrincipalType

if TYPE_CHECKING:
    from captain.api.sso_live import LiveSso

DEFAULT_ORG_ID = "default"


log = logging.getLogger(__name__)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewUserBody(_Body):
    email: str = Field(min_length=3, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    team: str | None = Field(default=None, max_length=128)
    job_title: str | None = Field(default=None, max_length=128)
    roles: list[str] = Field(default_factory=list, max_length=10)


class UpdateUserBody(_Body):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    team: str | None = Field(default=None, max_length=128)
    job_title: str | None = Field(default=None, max_length=128)
    roles: list[str] | None = Field(default=None, max_length=10)
    active: bool | None = None


class InviteBody(_Body):
    email: str = Field(min_length=3, max_length=128)


class EmailLinkBody(_Body):
    email: str = Field(min_length=3, max_length=128)


EMAIL_LINKS_PER_15_MIN = 3


class SsoSettingsBody(_Body):
    provider: str = Field(default="oidc", max_length=20)  # entra | okta | google | oidc
    issuer: str = Field(min_length=8, max_length=300)
    client_id: str = Field(min_length=2, max_length=200)
    client_secret: str | None = Field(default=None, max_length=500)  # empty = keep
    roles_claim: str = Field(default="groups", max_length=40)
    group_roles: dict[str, list[str]] = Field(default_factory=dict, max_length=50)

    @field_validator("issuer")
    @classmethod
    def _https(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("https://") or v.startswith("http://127.0.0.1")
                or v.startswith("http://localhost")):  # fmt: skip
            raise ValueError("the provider address must start with https://")
        return v

    @field_validator("group_roles")
    @classmethod
    def _roles(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        out = {}
        for g, roles in v.items():
            bad = [r for r in roles if r not in ROLE_PERMISSIONS or r == "platform-admin"]
            if bad:
                raise ValueError(f"unknown role(s) for group {g!r}: {', '.join(bad)}")
            if g.strip() and roles:
                out[g.strip()] = sorted(set(roles))
        return out


class RedeemBody(_Body):
    token: str = Field(min_length=10, max_length=200)


def user_view(p: PrincipalRecord) -> dict[str, Any]:
    return {
        "id": p.id,
        "email": p.id,
        "name": p.display_name,
        "team": p.team_id,
        "job_title": p.job_title,
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
    sso: "LiveSso",
    session_of: Callable[[Request], tuple[AdminActor, str] | None],
    signin_mailer: SignInMailer | None = None,
    console_url: str | None = None,
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
        done["sso"] = sso.get() is not None  # known from the running install, never stored
        return {
            "org_id": org_id(),  # what budgets for "the whole company" are keyed on
            **org.model_dump(),
            "updated_at": at.isoformat() if at else None,
            "updated_by": by,
            "sso_configured": sso.get() is not None,
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

    @app.get("/admin/v1/teams")
    def list_teams(a: Actor) -> list[str]:
        """Team names in use, for the pick lists on Users and Create a key."""
        require(a.roles, Permission.KEYS_READ)
        return team_names(queries)

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
            team_id=same_team(queries, body.team),
            roles=roles,
            job_title=(body.job_title or "").strip() or None,
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
            team_id=same_team(queries, body.team)
            if "team" in body.model_fields_set
            else old.team_id,
            idp_subject=old.idp_subject,
            roles=roles,
            active=old.active if body.active is None else body.active,
            job_title=((body.job_title or "").strip() or None)
            if "job_title" in body.model_fields_set
            else old.job_title,
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

    def console_roles(email: str) -> frozenset[str]:
        """A user's console roles right now (deactivated or unknown = none)."""
        p = ids.get_principal(email)
        if p is None or not p.active or p.type != PrincipalType.USER.value:
            return frozenset()
        return p.roles

    sent: dict[str, list[float]] = {}

    @app.post("/admin/v1/auth/email-link")
    def email_link(body: EmailLinkBody) -> dict[str, Any]:
        """Companies without SSO: email a one-time sign-in link to an existing user. The
        answer is the same whether or not the email belongs to a user."""
        svc = need_invites()
        if signin_mailer is None or not console_url:
            raise HTTPException(
                501, {"error": "NOT_CONFIGURED", "message": "email sign-in is not set up"}
            )
        try:
            email = normalise_email(body.email)
        except ValueError:
            raise HTTPException(
                400, {"error": "INVALID", "message": "enter a valid email address"}
            ) from None
        now = time.time()
        recent = [t for t in sent.get(email, []) if now - t < SIGNIN_LINK_TTL_S]
        answer = {
            "status": "If this email belongs to a Captain user, a sign-in link is on its way. "
            f"It works once, for {SIGNIN_LINK_TTL_S // 60} minutes."
        }
        if len(recent) >= EMAIL_LINKS_PER_15_MIN or not console_roles(email):
            sent[email] = recent
            return answer
        token = svc.create_signin_link(email)
        try:
            signin_mailer.send(
                email, f"{console_url.rstrip('/')}/accept-invite#token={token}",
                SIGNIN_LINK_TTL_S // 60,
            )  # fmt: skip
        except Exception:  # noqa: BLE001 - never reveal delivery details to the caller
            log.exception("sign-in email could not be sent")
        sent[email] = [*recent, now]
        return answer

    @app.post("/admin/v1/auth/invite/redeem")
    def redeem_invite(body: RedeemBody) -> Response:
        svc = need_invites()
        via_email: list[str] = []

        def roles_for(email: str) -> frozenset[str]:
            via_email.append(email)
            return console_roles(email)

        a, sid, csrf = svc.redeem(body.token, roles_for)
        if via_email:  # an emailed sign-in link: the user already exists with their roles
            record(EventType.INVITE_REDEEMED, a.id, {"how": "email link"})
            return session_response(svc, a, sid, csrf)
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
            p = dataclasses.replace(p, roles=p.roles | {"org-admin"}, active=True)
        ids.put_principal(p)
        record(EventType.INVITE_REDEEMED, a.id, {"role": "org-admin"})
        return session_response(svc, a, sid, csrf)

    def session_response(svc: InviteService, a: AdminActor, sid: str, csrf: str) -> Response:
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

    # ------------------------------------------------------------ SSO setup
    # The org admin connects the company's identity provider here; see sso_live.py.
    def sso_fail(e: Exception) -> HTTPException:
        return HTTPException(400, {"error": "SSO_SETUP", "message": str(e)})

    @app.get("/admin/v1/sso/settings")
    def sso_settings(a: Actor) -> dict[str, Any]:
        require(a.roles, Permission.ORG_WRITE)
        return sso.view()

    @app.put("/admin/v1/sso/settings")
    def sso_save(a: Actor, body: SsoSettingsBody) -> dict[str, Any]:
        require(a.roles, Permission.ORG_WRITE)
        try:
            sso.save_draft(body.model_dump(), a.id)
        except (SsoSetupError, ValueError) as e:
            raise sso_fail(e) from None
        record(EventType.ORG_CHANGED, a.id, {"fields": "sso settings"})
        return sso.view()

    @app.post("/admin/v1/sso/check")
    def sso_check(a: Actor) -> dict[str, Any]:
        require(a.roles, Permission.ORG_WRITE)
        try:
            return sso.check_draft()
        except SsoSetupError as e:
            raise sso_fail(e) from None

    @app.post("/admin/v1/sso/turn-on")
    def sso_on(a: Actor) -> dict[str, Any]:
        require(a.roles, Permission.ORG_WRITE)
        try:
            sso.turn_on(a.id)
        except SsoSetupError as e:
            raise sso_fail(e) from None
        record(EventType.ORG_CHANGED, a.id, {"fields": "sso turned on"})
        return sso.view()

    @app.post("/admin/v1/sso/turn-off")
    def sso_off(a: Actor) -> dict[str, Any]:
        require(a.roles, Permission.ORG_WRITE)
        try:
            sso.turn_off(a.id)
        except SsoSetupError as e:
            raise sso_fail(e) from None
        record(EventType.ORG_CHANGED, a.id, {"fields": "sso turned off"})
        return sso.view()
