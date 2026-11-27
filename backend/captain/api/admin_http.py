"""Admin API for the Captain Central Control Dashboard: /admin/v1/*.

- Every endpoint authenticates the administrator (SSO token; dev token locally) and
  checks a specific permission on the SERVER. Hiding UI navigation is never the control.
- Every change goes through AdminService: permission-checked and recorded in the
  evidence ledger, with "record first" for anything that weakens controls.
- Responses never contain secrets: keys are listed by id; the raw key is returned once,
  at issue time. Events never contain prompt content.
"""

import csv
import io
import json
import logging
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from captain.api.admin_adopters import install_adopter_routes
from captain.api.admin_aikeys import install_aikey_routes
from captain.api.admin_auth import AdminAuthenticator
from captain.api.admin_org import install_org_routes
from captain.api.admin_platform import install_platform_routes
from captain.api.admin_queries import (
    AdminQueries,
    ResolvingQueries,
    alert_view,
    event_view,
    key_view,
    principal_view,
    same_team,
)
from captain.api.admin_sso import LOGIN_TTL_S, CsrfError, LoginCookie, SsoError, SsoService
from captain.api.controlplane import CompanyStore, install_controlplane_routes
from captain.api.limits import BodyLimitMiddleware
from captain.api.org import InMemoryOrganizationStore, InviteService, OrganizationStore
from captain.api.signin_mail import SignInMailer
from captain.api.sso_live import LiveSso
from captain.api.state import InMemoryStateStore
from captain.core.admin import AdminActor, AdminService
from captain.core.budget.model import BudgetDefinition, Period, RateLimitDefinition
from captain.core.context.service import Application, ControlDowngradeError, Project
from captain.core.decision_api import AuthenticationError, CoreError
from captain.core.engine import admin_event
from captain.core.factory import BuiltCore
from captain.core.identity.killswitch import KillScope
from captain.core.identity.model import PrincipalRecord
from captain.core.identity.rbac import Permission, PermissionDenied, permissions_for, require
from captain.core.models import (
    ActionType,
    AutonomyLevel,
    Channel,
    Environment,
    EventType,
    FindingType,
    PrincipalType,
    RegisterEntry,
    RiskTier,
)
from captain.core.registry.service import (
    McpServer,
    ModelEntry,
    Provider,
    ProviderKind,
    RegistryError,
    Route,
    ToolEntry,
    ToolImpact,
    ToolRisk,
)
from captain.core.reporting.evidence import FRAMEWORKS, export_evidence
from captain.core.reporting.monthly import (
    month_bounds,
    monthly_report,
    report_sheets,
    to_xlsx,
)
from captain.core.secrets_store import SecretsService
from captain.core.usage.pricing import ModelPrice
from captain.core.usage.reports import chargeback, settled_costs, to_csv

log = logging.getLogger("captain.admin")


def _csv_cell(v: Any) -> Any:
    """Spreadsheet programs run cells that start with = + - @ as formulas: neutralise them."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return "" if v is None else v


CALL_EVENTS = {"call.allowed", "call.masked", "call.refused", "call.observed", "tool.held"}
EVAL_EVENTS = {"evaluation.scored", "evaluation.failed"}
ADMIN_EVENTS = {
    "key.issued",
    "key.revoked",
    "key.rotated",
    "killswitch.changed",
    "register.changed",
    "budget.defined",
    "budget.override",
    "policy.changed",
    "registry.changed",
    "approval.decided",
    "approval.escalated",
    "alert.raised",
    "subject.erased",
    "aikey.changed",
    "company.changed",
    "user.changed",
    "org.changed",
    "invite.created",
    "invite.redeemed",
}


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IssueKeyBody(_Body):
    principal_id: str = Field(default="", max_length=128)
    # A key for an app or service instead of a person: Captain creates it as "app:<name>".
    app_name: str | None = Field(default=None, min_length=2, max_length=80)
    register_id: str
    project_id: str
    application_id: str
    environment: Environment
    ttl_days: int | None = Field(default=None, gt=0, le=365)
    label: str = Field(default="", max_length=200)


class PriceBody(_Body):
    input_usd_per_m: float = Field(ge=0, le=10_000)
    output_usd_per_m: float = Field(ge=0, le=10_000)
    cached_input_usd_per_m: float | None = Field(default=None, ge=0, le=10_000)


class ReasonBody(_Body):
    reason: str = Field(min_length=3, max_length=500)


class KillBody(_Body):
    scope: KillScope
    target: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=3, max_length=500)


class BudgetBody(_Body):
    scope: str = Field(pattern=r"^[a-z_]+:[^\s]+$")
    period: Period
    limit_micros: int = Field(gt=0)
    hard: bool = True


class RateLimitBody(_Body):
    scope: str = Field(pattern=r"^[a-z_]+:[^\s]+$", max_length=256)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)


class OverrideBody(_Body):
    scope: str
    extra_micros: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)


class ApprovalDecisionBody(_Body):
    approve: bool
    reason: str = Field(min_length=3, max_length=500)


class SimFinding(_Body):
    type: FindingType
    entity: str = Field(default="TEST", max_length=64)
    score: float = Field(default=0.9, ge=0, le=1)


class SimulateBody(_Body):
    """A made-up request, described by the facts the policy looks at (never content)."""

    tier: RiskTier = RiskTier.TIER_3
    autonomy: AutonomyLevel = AutonomyLevel.A2
    channel: Channel = Channel.API
    action: ActionType = ActionType.MODEL_CALL
    model: str | None = Field(default="claude-sonnet", max_length=200)
    tool: str | None = Field(default=None, max_length=200)
    tool_impact: str | None = Field(default=None, pattern="^(low|high)$")
    allowed_models: list[str] = Field(default_factory=list, max_length=100)
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    environment: Environment = Environment.DEVELOPMENT
    streaming: bool = False
    high_risk: bool = False
    findings: list[SimFinding] = Field(default_factory=list, max_length=20)


class RegisterBody(_Body):
    entry: RegisterEntry
    approval_ref: str | None = None


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _iso(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_iso(x) for x in v]
    return v


def _period(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    return end - timedelta(days=days), end + timedelta(seconds=1)


def _install_sso_routes(
    app: FastAPI,
    live: "LiveSso",
    session_of: Callable[[Request], tuple[AdminActor, str] | None],
    invites: InviteService | None,
    on_test: Callable[[AdminActor], None],
) -> None:
    """Sign-in routes. They always exist and follow whatever SSO is on right now (the org
    admin can turn it on from the Setup page), plus the org admin's test sign-in."""

    def fail(code: str, test: bool = False) -> RedirectResponse:
        target = f"/setup?sso_test={code}" if test else f"/?sso_error={code}"
        r = RedirectResponse(target, status_code=303)
        r.delete_cookie(LoginCookie.NAME, path="/admin/v1/auth")
        return r

    @app.get("/admin/v1/auth/login")
    def login(request: Request, test: int = 0) -> Response:
        if test:
            # Only a signed-in org admin may try new settings (it opens no session).
            s, svc = session_of(request), live.draft()
            if s is None or Permission.ORG_WRITE not in permissions_for(s[0].roles):
                return fail("forbidden", True)
            if svc is None:
                return fail("no_settings", True)
        else:
            svc = live.get()
            if svc is None:
                return fail("not_configured")
        try:
            url, sealed = svc.start(test=bool(test))
        except SsoError as e:
            log.warning("SSO login could not start: %s", e)
            return fail(e.code, bool(test))
        except Exception as e:  # noqa: BLE001 - e.g. the provider address is wrong
            log.warning("SSO login could not start: %s", e)
            return fail("unreachable", bool(test))
        r = RedirectResponse(url, status_code=303)
        r.set_cookie(LoginCookie.NAME, sealed, max_age=LOGIN_TTL_S, path="/admin/v1/auth",
                     httponly=True, samesite="lax", secure=svc.cfg.secure_cookies)  # fmt: skip
        return r

    @app.get("/admin/v1/auth/callback")
    def callback(request: Request) -> Response:
        sealed = request.cookies.get(LoginCookie.NAME)
        test = False
        probe = live.get() or live.draft()
        if probe is not None and sealed:
            try:
                test = probe.login_cookie.open(sealed)[0].startswith("t.")
            except SsoError:
                pass
        svc = live.draft() if test else live.get()
        if svc is None:
            return fail("not_configured", test)
        try:
            a, sid, _ = svc.finish(dict(request.query_params), sealed, open_session=not test)
        except SsoError as e:
            log.warning("SSO sign-in refused (%s): %s", e.code, e)
            return fail(e.code, test)
        except AuthenticationError as e:  # ID token failed verification
            log.warning("SSO sign-in refused (token): %s", e)
            return fail("failed", test)
        if test:
            # The test passes only if the person who tried it would be an org admin under
            # SSO: otherwise turning SSO on could lock the company out of its own Captain.
            if Permission.ORG_WRITE not in permissions_for(a.roles):
                return fail("not_admin", True)
            on_test(a)
            r = RedirectResponse("/setup?sso_test=ok", status_code=303)
            r.delete_cookie(LoginCookie.NAME, path="/admin/v1/auth")
            return r
        log.info("SSO sign-in: %s roles=%s", a.id, sorted(a.roles))
        r = RedirectResponse("/", status_code=303)  # fixed target: no open redirect
        r.delete_cookie(LoginCookie.NAME, path="/admin/v1/auth")
        r.set_cookie(svc.cookie_name, sid, max_age=svc.cfg.session_lifetime_s, path="/",
                     httponly=True, samesite="lax", secure=svc.cfg.secure_cookies)  # fmt: skip
        return r

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
        r = JSONResponse({"status": "signed out"})
        svc = live.get()
        if svc is not None and (sid := request.cookies.get(svc.cookie_name)):
            if (s := svc.sessions.get(sid)) is not None:
                svc.check_csrf(request.method, request.headers, s[1])
                svc.sessions.delete(sid)
            r.delete_cookie(svc.cookie_name, path="/", httponly=True, samesite="lax",
                            secure=svc.cfg.secure_cookies)  # fmt: skip
        if invites is not None and (sid := request.cookies.get(invites.cookie_name)):
            if (s := invites.session(sid)) is not None:
                invites.check_csrf(request.method, request.headers, s[1])
                invites.end(sid)
            r.delete_cookie(invites.cookie_name, path="/", httponly=True, samesite="strict")
        return r


def _media(suffix: str) -> str:
    return {
        ".js": "text/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".json": "application/json",
    }.get(suffix, "application/octet-stream")


def create_admin_app(
    built: BuiltCore,
    auth: AdminAuthenticator,
    queries: AdminQueries,
    cors_origins: list[str] | None = None,
    static_dir: Path | None = None,
    sso: SsoService | None = None,
    dev_token_enabled: bool = False,
    admin_key_enabled: bool = False,
    org_store: OrganizationStore | None = None,
    invites: InviteService | None = None,
    signin_mailer: "SignInMailer | None" = None,
    console_url: str | None = None,
    ai_keys: SecretsService | None = None,
    companies: CompanyStore | None = None,
    live_sso: "LiveSso | None" = None,
    company_http: Any = None,
    provision: Any = None,
) -> FastAPI:
    # The SSO in force can change while running (the org admin turns it on in Setup).
    live = live_sso or LiveSso(InMemoryStateStore(ttl_s=12 * 3600), None, fixed=sso)
    app = FastAPI(title="Captain Admin API", version="1", docs_url=None, redoc_url=None)
    app.add_middleware(BodyLimitMiddleware, max_bytes=1_000_000)  # admin bodies are small

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        resp: Response = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"  # the console cannot be framed (clickjacking)
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/admin/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )
    core = built.core
    admin = AdminService(core, built.register, built.infra.budget_catalog)  # type: ignore[arg-type]
    # Erasure (ADR-005): the ledger stores pseudonyms; every view shows people again (or
    # "erased subject"), except the evidence export, which ships the signed bodies as stored.
    raw_queries = queries
    pz = built.pseudonymiser
    if pz is not None:
        queries = ResolvingQueries(queries, pz)

    @app.exception_handler(AuthenticationError)
    async def _auth(_: Request, e: AuthenticationError) -> JSONResponse:
        return JSONResponse({"error": "UNAUTHENTICATED", "message": str(e)}, status_code=401)

    @app.exception_handler(CsrfError)
    async def _csrf(_: Request, e: CsrfError) -> JSONResponse:
        return JSONResponse({"error": "CSRF", "message": str(e)}, status_code=403)

    @app.exception_handler(PermissionDenied)
    async def _perm(_: Request, e: PermissionDenied) -> JSONResponse:
        return JSONResponse(
            {"error": "FORBIDDEN", "message": f"requires {e.permission.value}"}, status_code=403
        )

    @app.exception_handler(ControlDowngradeError)
    async def _down(_: Request, e: ControlDowngradeError) -> JSONResponse:
        return JSONResponse({"error": "APPROVAL_REQUIRED", "message": str(e)}, status_code=409)

    @app.exception_handler(RegistryError)
    async def _reg(_: Request, e: RegistryError) -> JSONResponse:
        return JSONResponse({"error": "INVALID", "message": str(e)}, status_code=400)

    @app.exception_handler(CoreError)
    async def _core(_: Request, e: CoreError) -> JSONResponse:
        return JSONResponse({"error": e.reason_code.value, "message": str(e)}, status_code=409)

    @app.exception_handler(ValueError)
    async def _val(_: Request, e: ValueError) -> JSONResponse:
        return JSONResponse({"error": "INVALID", "message": str(e)}, status_code=400)

    def sso_session_of(request: Request) -> tuple[AdminActor, str] | None:
        sso = live.get()
        if sso is None:
            return None
        sid = request.cookies.get(sso.cookie_name)
        return sso.sessions.get(sid) if sid else None

    def invite_session_of(request: Request) -> tuple[AdminActor, str] | None:
        if invites is None:
            return None  # refused (None) as soon as SSO is configured
        s = invites.session(request.cookies.get(invites.cookie_name))
        if s is None:
            return None
        # Live roles: deactivating someone or changing their roles takes effect at once.
        p = built.infra.identity_store.get_principal(s[0].id)
        if p is None or not p.active or not p.roles:
            return None
        return AdminActor(s[0].id, p.roles), s[1]

    def session_of(request: Request) -> tuple[AdminActor, str] | None:
        return sso_session_of(request) or invite_session_of(request)

    def actor(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> AdminActor:
        if authorization and authorization.startswith("Bearer "):
            host = request.client.host if request.client else None
            return auth.authenticate(authorization[7:].strip(), host)
        if (sso := live.get()) is not None and (s := sso_session_of(request)) is not None:
            a, csrf = s
            sso.check_csrf(request.method, request.headers, csrf)  # cookie => CSRF defence
            return a
        if invites is not None and (s := invite_session_of(request)) is not None:
            a, csrf = s
            invites.check_csrf(request.method, request.headers, csrf)
            return a
        raise AuthenticationError("sign in required")

    # ------------------------------------------------------------------ auth
    @app.get("/admin/v1/auth/config")
    def auth_config() -> dict[str, bool]:
        # stixis_console: this is Stixis's control plane, not a company's own Captain; the
        # console then shows only Companies, Stixis staff and Audit.
        return {
            "sso": live.get() is not None,
            "dev_token": dev_token_enabled,
            "admin_key": admin_key_enabled,
            "stixis_console": companies is not None,
            # Companies without SSO: people get a one-time sign-in link by email.
            "email_link": live.get() is None
            and invites is not None
            and signin_mailer is not None
            and bool(console_url),
        }

    def sso_tested(a: AdminActor) -> None:
        live.mark_tested(a.id)
        built.core.record(admin_event(EventType.ORG_CHANGED, a.id, {"fields": "sso tested"}))

    _install_sso_routes(app, live, session_of, invites, sso_tested)

    Actor = Annotated[AdminActor, Depends(actor)]

    def can(a: AdminActor, p: Permission) -> AdminActor:
        require(a.roles, p)
        return a

    # One company-profile store, shared by the org pages and the platform bootstrap.
    org_store = org_store or InMemoryOrganizationStore()
    install_org_routes(app, built, admin, queries, actor, org_store, invites, live, session_of,
                       signin_mailer, console_url)  # fmt: skip
    install_platform_routes(app, built, queries, org_store, invites)
    if companies is not None:  # Stixis's own install only (CAPTAIN_CONTROL_PLANE=1)
        install_controlplane_routes(app, built, actor, companies, ai_keys, http=company_http,
                                    queries=queries, provision=provision)  # fmt: skip
    install_aikey_routes(app, built, admin, queries, actor, ai_keys)
    install_adopter_routes(app, queries, actor)

    # ------------------------------------------------------------------- me
    @app.get("/admin/v1/me")
    def me(a: Actor) -> dict[str, Any]:
        return {
            "id": a.id,
            "roles": sorted(a.roles),
            "permissions": sorted(p.value for p in permissions_for(a.roles)),
        }

    # ------------------------------------------------------------- overview
    @app.get("/admin/v1/overview")
    def overview(a: Actor, days: int = Query(30, ge=1, le=366)) -> dict[str, Any]:
        can(a, Permission.USAGE_READ)
        since, until = _period(days)
        decisions: Counter[Any] = Counter()
        refusals: Counter[Any] = Counter()
        coverage: Counter[Any] = Counter()
        risk: Counter[Any] = Counter()
        by_model: Counter[str] = Counter()
        daily: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "refused": 0, "spend_micros": 0}
        )
        spend = est = tokens_in = tokens_out = 0
        for (
            day,
            etype,
            decision,
            reason,
            cov,
            risk_level,
            model,
            source,
            n,
            cost,
            tin,
            tout,
        ) in queries.overview_rows(since, until):
            if etype in CALL_EVENTS:
                decisions[decision] += n
                coverage[cov] += n
                risk[risk_level] += n
                daily[day]["calls"] += n
                if etype == "call.refused":
                    refusals[reason] += n
                    daily[day]["refused"] += n
            elif etype == "usage.settled":
                spend += cost
                if source != "PROVIDER_REPORTED":
                    est += cost
                tokens_in += tin
                tokens_out += tout
                daily[day]["spend_micros"] += cost
                by_model[str(model or "unknown")] += cost
        p50, p95 = queries.latency_percentiles(since, until)
        return {
            "period_days": days,
            "governed_calls": coverage.get("GOVERNED", 0),
            "observed_events": coverage.get("OBSERVED", 0),
            "decisions": {k: v for k, v in decisions.items() if k},
            "refusals_by_reason": dict(refusals.most_common()),
            "risk_levels": {k: v for k, v in risk.items() if k},
            "spend_micros": spend,
            "estimated_spend_micros": est,
            "tokens": {"input": tokens_in, "output": tokens_out},
            "latency_ms": {"p50": p50, "p95": p95},
            "spend_by_model": dict(by_model.most_common(10)),
            "daily": [{"day": k, **v} for k, v in sorted(daily.items())],
            "approvals_pending": len(queries.approvals("PENDING", 500)),
            "alerts_recent": [alert_view(x) for x in queries.alerts(5)],
            "kill_switches_active": len(built.infra.killswitch_store.active()),
            "definitions": {
                "governed": "Routed through Captain; policy enforced before the call.",
                "observed": "Reported after the fact by telemetry; no pre-enforcement.",
                "estimated_spend": (
                    "Cost computed from text length when the provider did not report usage."
                ),
            },
        }

    # ------------------------------------------------------------- breakdown
    @app.get("/admin/v1/usage/breakdown")
    def usage_breakdown(
        a: Actor,
        by: str = Query("principal", pattern="^(principal|project|app|cost_centre|team|model)$"),
        days: int = Query(30, ge=1, le=366),
    ) -> dict[str, Any]:
        """Governed usage grouped by person, project, application, cost centre, team or model.
        Built from settled calls (each carries the scopes it was charged to); refusals are
        counted per person only, because a refusal happens before a project is charged."""
        can(a, Permission.USAGE_READ)
        since, until = _period(days)
        rows: dict[str, dict[str, Any]] = {}
        for key, calls, tin, tout, cost, est_cost, refused in queries.usage_by(by, since, until):
            rows[key] = {
                "calls": calls,
                "tokens_in": tin,
                "tokens_out": tout,
                "cost_micros": cost,
                "estimated_cost_micros": est_cost,
                "refused": refused,
            }
        total = sum(r["cost_micros"] for r in rows.values())
        items = [
            {"key": k, **r, "share_pct": round(100 * r["cost_micros"] / total, 1) if total else 0.0}
            for k, r in sorted(rows.items(), key=lambda kv: (-kv[1]["cost_micros"], kv[0]))
        ][:100]
        return {"by": by, "period_days": days, "total_cost_micros": total, "items": items}

    # ----------------------------------------------------------------- policy
    @app.get("/admin/v1/policy")
    def policy(a: Actor) -> dict[str, Any]:
        """The policy that is ACTIVE in OPA right now (bundle version and tier rules). Policy
        changes are made in git, reviewed, signed and deployed (ADR-003); this page shows the
        result and lets you test it."""
        can(a, Permission.USAGE_READ)
        engine = core.m.policy
        reader = getattr(engine, "data", None)
        if reader is None:
            raise HTTPException(
                501, {"error": "NOT_IMPLEMENTED", "message": "policy engine cannot be read"}
            )
        try:
            return {"bundle": reader("bundle"), "tiers": reader("tiers"), "tools": reader("tools")}
        except Exception as e:  # noqa: BLE001 - surface "OPA unreachable" plainly
            raise HTTPException(
                503, {"error": "POLICY_UNAVAILABLE", "message": str(e)[:200]}
            ) from e

    @app.post("/admin/v1/policy/simulate")
    def simulate(a: Actor, body: SimulateBody) -> dict[str, Any]:
        """What would the active policy decide for this kind of request? Nothing is executed,
        charged or recorded: it is a question to OPA, not a call."""
        can(a, Permission.USAGE_READ)
        tool_facts = (
            {"impact": body.tool_impact, "risk": "MEDIUM", "read_only": body.tool_impact == "low"}
            if body.tool_impact
            else None
        )
        payload = {
            "request": {
                "channel": body.channel.value, "action": body.action.value,
                "coverage": "GOVERNED", "model": body.model, "tool": body.tool,
                "max_output_tokens": None, "streaming": body.streaming,
            },
            "principal": {"id": "simulation", "type": "USER", "roles": [], "team_id": None},
            "register": {
                "id": "SIMULATION", "tier": body.tier.value, "autonomy": body.autonomy.value,
                "allowed_models": sorted(body.allowed_models),
                "allowed_tools": sorted(body.allowed_tools), "high_risk": body.high_risk,
            },
            "project_id": "simulation", "application_id": "simulation",
            "environment": body.environment.value,
            "tool": tool_facts,
            "risk": {"level": "MEDIUM", "score": 50},
            "findings": [f.model_dump(mode="json") for f in body.findings],
        }  # fmt: skip
        try:
            r = core.m.policy.evaluate(payload)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                503, {"error": "POLICY_UNAVAILABLE", "message": str(e)[:200]}
            ) from e
        return {**r.model_dump(mode="json"), "simulated": True}

    # ------------------------------------------------------------- csv export
    def _csv(name: str, rows: list[dict[str, Any]], fields: list[str]) -> Response:
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(fields)
        for r in rows:
            w.writerow([_csv_cell(r.get(f)) for f in fields])
        return Response(
            out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/admin/v1/usage/breakdown.csv", response_model=None)
    def usage_breakdown_csv(
        a: Actor,
        by: str = Query("principal", pattern="^(principal|project|app|cost_centre|team|model)$"),
        days: int = Query(30, ge=1, le=366),
    ) -> Response:
        data = usage_breakdown(a, by, days)
        fields = ["key", "calls", "refused", "tokens_in", "tokens_out", "cost_micros",
                  "estimated_cost_micros", "share_pct"]  # fmt: skip
        return _csv(f"captain-usage-by-{by}.csv", data["items"], fields)

    @app.get("/admin/v1/events.csv", response_model=None)
    def events_csv(
        a: Actor,
        days: int = Query(7, ge=1, le=366),
        kind: str = Query("calls", pattern="^(calls|admin|all)$"),
        decision: str | None = None,
        principal_id: str | None = None,
        register_id: str | None = None,
    ) -> Response:
        data = events(a, days, kind, decision, principal_id, register_id, 1000, 0)
        fields = ["recorded_at", "who", "headline", "outcome", "why", "category",
                  "event_type", "channel", "coverage", "principal_id",
                  "register_id", "decision", "reason_code", "policy_version", "model", "tool",
                  "risk_level", "cost_micros", "input_tokens", "output_tokens", "request_id",
                  "trace_id"]  # fmt: skip
        return _csv("captain-events.csv", data["items"], fields)

    # --------------------------------------------------------------- coverage
    @app.get("/admin/v1/coverage")
    def coverage(a: Actor, days: int = Query(30, ge=1, le=366)) -> dict[str, Any]:
        """How much AI activity Captain governs versus only observes (or suspects). One row
        per call decision or observed event; settlement records are not counted again."""
        can(a, Permission.USAGE_READ)
        since, until = _period(days)
        classes = ("GOVERNED", "OBSERVED", "BYPASS_DETECTED", "UNOBSERVED")
        totals: Counter[str] = Counter()
        by_channel: dict[str, Counter[str]] = defaultdict(Counter)
        by_person: dict[str, Counter[str]] = defaultdict(Counter)
        rows = queries.coverage_counts(since, until, CALL_EVENTS, {"admin", "security"})
        for cov, channel, person, n in rows:
            totals[cov] += n
            by_channel[channel][cov] += n
            if person:
                by_person[person][cov] += n
        total = sum(totals.values())
        return {
            "period_days": days,
            "total_events": total,
            "classes": [
                {
                    "coverage": c,
                    "events": totals[c],
                    "share_pct": round(100 * totals[c] / total, 1) if total else 0.0,
                }
                for c in classes
            ],  # fmt: skip
            "by_channel": [
                {"channel": ch, **{c: n[c] for c in classes}}
                for ch, n in sorted(by_channel.items())
            ],
            "by_person": [
                {"principal_id": p, **{c: n[c] for c in classes}}
                for p, n in sorted(by_person.items(), key=lambda kv: -sum(kv[1].values()))
            ][:50],
            "note": (
                "Governed: Captain decided before the call. Observed: reported afterwards by "
                "a vendor or hook. Bypass detected: seen going around Captain (needs the "
                "discovery sources; none are connected unless listed on the Observed page)."
            ),
        }

    # ----------------------------------------------------------------- agents
    @app.get("/admin/v1/agents")
    def agents(a: Actor, days: int = Query(30, ge=1, le=366)) -> list[dict[str, Any]]:
        """Each registered AI system with what it is allowed to do and what it actually did."""
        can(a, Permission.REGISTER_READ)
        since, until = _period(days)
        stat: dict[str, Counter[str]] = defaultdict(Counter)
        last: dict[str, str] = {}
        calls = {"call.allowed", "call.masked"}
        for partition, etype, n, newest in queries.partition_stats(since, until):
            c = stat[partition]
            if etype in calls:
                c["calls"] += n
            elif etype == "call.refused":
                c["refused"] += n
            elif etype == "tool.held":
                c["held"] += n
            elif etype == "tool.executed":
                c["tools_run"] += n
            if etype in CALL_EVENTS | {"tool.executed"}:
                last[partition] = max(last.get(partition, ""), newest.isoformat())
        # Live evaluation (ADR-005): a small sample of Tier 1-2 calls, so loading them is cheap.
        ev: dict[str, list[float]] = defaultdict(list)
        _, evals = queries.events_page(since, until, EVAL_EVENTS, None, None, None, 100_000, 0)
        for e in evals:
            d = e.body.get("details") or {}
            pid = str(e.body.get("register_id") or "")
            if e.event_type == "evaluation.failed":
                stat[pid]["eval_failed"] += 1
            elif isinstance(d.get("overall"), int | float):
                ev[pid].append(float(d["overall"]))
                stat[pid]["eval_low"] += 1 if d.get("low") else 0
        out = []
        for r in queries.register_entries():
            c = stat[r.id]
            out.append(
                {
                    "id": r.id, "name": r.name, "tier": r.risk_tier.value,
                    "autonomy": r.autonomy.value, "owner_id": r.owner_id, "active": r.active,
                    "allowed_models": sorted(r.allowed_models),
                    "allowed_tools": sorted(r.allowed_tools),
                    "calls": c["calls"], "refused": c["refused"], "tools_held": c["held"],
                    "tools_run": c["tools_run"], "last_active": last.get(r.id),
                    "eval_scored": len(ev[r.id]),
                    "eval_mean": round(sum(ev[r.id]) / len(ev[r.id]), 3) if ev[r.id] else None,
                    "eval_low": c["eval_low"], "eval_failed": c["eval_failed"],
                }
            )  # fmt: skip
        return out

    # ------------------------------------------------------------- observed
    @app.get("/admin/v1/observed")
    def observed(a: Actor, days: int = Query(30, ge=1, le=366)) -> dict[str, Any]:
        """Usage Captain did not route: vendor imports and coding-tool hook reports. Kept
        apart from governed spend; the two may overlap and must never be added together."""
        can(a, Permission.USAGE_READ)
        since_day = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        since, until = _period(366)
        by_source: dict[str, dict[str, Any]] = {}
        actors: dict[tuple[str, str], dict[str, Any]] = {}
        daily: dict[str, dict[str, int]] = defaultdict(lambda: {"events": 0, "cost_micros": 0})
        governed = 0
        for e in queries.events(since, until):
            b = e.body
            u = b.get("usage") or {}
            if e.event_type == "usage.settled" and e.recorded_at.date().isoformat() >= since_day:
                governed += int(u.get("cost") or 0)
            if b.get("coverage") != "OBSERVED" or e.event_type not in (
                "call.observed",
                "tool.executed",
            ):
                continue
            d = b.get("details") or {}
            day = str(d.get("period") or e.recorded_at.date().isoformat())
            if day < since_day:
                continue
            source = str(
                d.get("vendor")
                or ("claude_code_hook" if b.get("channel") == "HOOK" else b.get("channel"))
            )
            who = str(b.get("principal_id") or "unattributed")
            tok = u.get("usage") or {}
            cost, t_in, t_out = (
                int(u.get("cost") or 0),
                int(tok.get("input_tokens") or 0),
                int(tok.get("output_tokens") or 0),
            )
            mapped = d.get("principal_mapped") is not False
            s = by_source.setdefault(
                source,
                {
                    "source": source,
                    "kind": "vendor import" if d.get("vendor") else "hook",
                    "events": 0,
                    "unmapped_events": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "cost_micros": 0,
                    "first_day": day,
                    "last_day": day,
                    "_who": set(),
                },
            )
            s["events"] += 1
            s["unmapped_events"] += not mapped
            s["tokens_in"] += t_in
            s["tokens_out"] += t_out
            s["cost_micros"] += cost
            s["first_day"], s["last_day"] = min(s["first_day"], day), max(s["last_day"], day)
            s["_who"].add(who)
            x = actors.setdefault(
                (source, who),
                {
                    "source": source,
                    "principal": who,
                    "mapped": mapped,
                    "events": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "cost_micros": 0,
                },
            )
            x["events"] += 1
            x["tokens_in"] += t_in
            x["tokens_out"] += t_out
            x["cost_micros"] += cost
            daily[day]["events"] += 1
            daily[day]["cost_micros"] += cost
        sources = sorted(by_source.values(), key=lambda s: -s["events"])
        for s in sources:
            s["principals"] = len(s.pop("_who"))
        top = sorted(actors.values(), key=lambda x: (-x["cost_micros"], -x["events"]))[:25]
        return {
            "period_days": days,
            "totals": {
                "events": sum(s["events"] for s in sources),
                "sources": len(sources),
                "principals": len(actors),
                "unmapped_events": sum(s["unmapped_events"] for s in sources),
                "tokens_in": sum(s["tokens_in"] for s in sources),
                "tokens_out": sum(s["tokens_out"] for s in sources),
                "cost_micros": sum(s["cost_micros"] for s in sources),
            },
            "governed_spend_micros": governed,
            "by_source": sources,
            "actors": top,
            "daily": [{"day": k, **v} for k, v in sorted(daily.items())],
            "note": (
                "Observed usage is reported by vendors or coding tools after the fact. Captain "
                "did not route it and could not have stopped it. It is not part of governed spend "
                "and may overlap with it."
            ),
        }

    # --------------------------------------------------------------- events
    @app.get("/admin/v1/events")
    def events(
        a: Actor,
        days: int = Query(7, ge=1, le=366),
        kind: str = Query("calls", pattern="^(calls|admin|all)$"),
        decision: str | None = None,
        principal_id: str | None = None,
        register_id: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        can(a, Permission.AUDIT_READ if kind == "admin" else Permission.USAGE_READ)
        since, until = _period(days)
        types = (
            CALL_EVENTS | {"usage.settled", "tool.executed"}
            if kind == "calls"
            else ADMIN_EVENTS
            if kind == "admin"
            else None
        )
        total, rows = queries.events_page(
            since, until, types, decision, principal_id, register_id, limit, offset
        )
        return {"total": total, "items": [_iso(event_view(e)) for e in rows]}

    @app.get("/admin/v1/requests/{request_id}")
    def request_detail(
        a: Actor, request_id: UUID, days: int = Query(30, ge=1, le=366)
    ) -> dict[str, Any]:
        can(a, Permission.USAGE_READ)
        since, until = _period(days)
        rows = [
            e for e in queries.events(since, until) if e.body.get("request_id") == str(request_id)
        ]
        if not rows:
            raise HTTPException(404, {"error": "NOT_FOUND"})
        return {"request_id": str(request_id), "events": [_iso(event_view(e)) for e in rows]}

    # ----------------------------------------------------------- principals/keys
    @app.get("/admin/v1/principals")
    def principals(a: Actor) -> list[dict[str, Any]]:
        can(a, Permission.KEYS_READ)
        return [principal_view(p) for p in queries.principals()]

    @app.get("/admin/v1/keys")
    def keys(a: Actor, principal_id: str | None = None) -> list[dict[str, Any]]:
        can(a, Permission.KEYS_READ)
        return [_iso(key_view(k)) for k in queries.keys(principal_id)]

    def ensure_key_context(a: AdminActor, pid: str, body: IssueKeyBody) -> tuple[str, str]:
        """A key belongs to a project and an application tied to its AI use. The console
        asks only for a team/project name and a tool, so create what is missing; an
        existing application tied to another project or AI use gets a separate one."""
        require(a.roles, Permission.KEYS_WRITE)
        ctx = built.infra.context_store
        owner = built.infra.identity_store.get_principal(pid)
        org = owner.org_id if owner else "default"
        proj = same_team(queries, body.project_id) or "general"
        p = ctx.project(proj)
        if p is None:
            ctx.put_project(Project(proj, org, proj))
        elif p.org_id != org or not p.active:
            proj = f"{proj}.{org}"[:128]
            if ctx.project(proj) is None:
                ctx.put_project(Project(proj, org, body.project_id.strip() or "general"))
        app_id = body.application_id.strip() or "other"
        existing = ctx.application(app_id)
        if (
            existing is None
            or existing.project_id != proj
            or existing.register_id != (body.register_id)
        ):
            if existing is not None:
                app_id = f"{app_id}.{proj}.{body.register_id}".lower()[:128]
            found = ctx.application(app_id)
            if found is None:
                ctx.put_application(
                    Application(app_id, proj, body.register_id, body.application_id.strip())
                )
            elif not found.active:
                raise HTTPException(409, {"error": "INACTIVE", "message": "that tool is retired"})
        return proj, app_id

    @app.post("/admin/v1/keys")
    def issue_key(a: Actor, body: IssueKeyBody) -> dict[str, Any]:
        pid = body.principal_id.strip()
        if body.app_name:
            require(a.roles, Permission.KEYS_WRITE)
            slug = re.sub(r"[^a-z0-9]+", "-", body.app_name.strip().lower()).strip("-")
            if not slug:
                raise HTTPException(400, {"error": "INVALID", "message": "enter an app name"})
            pid = f"app:{slug}"
            ids = built.infra.identity_store
            if ids.get_principal(pid) is None:
                org = next((x.org_id for x in queries.principals()), "default")
                ids.put_principal(PrincipalRecord(
                    id=pid, type=PrincipalType.APPLICATION.value, org_id=org,
                    display_name=body.app_name.strip(),
                ))  # fmt: skip
        if not pid:
            raise HTTPException(400, {"error": "INVALID", "message": "choose who the key is for"})
        project_id, application_id = ensure_key_context(a, pid, body)
        issued = admin.issue_key(
            a,
            pid,
            body.register_id,
            project_id,
            application_id,
            body.environment,
            timedelta(days=body.ttl_days) if body.ttl_days else None,
            body.label,
        )
        return {
            "key": _iso(key_view(issued.record)),
            "raw_key": issued.raw_key,
            "notice": "Copy this key now. It is shown only once and cannot be recovered.",
        }

    @app.post("/admin/v1/keys/{key_id}/revoke")
    def revoke_key(a: Actor, key_id: str, body: ReasonBody) -> dict[str, str]:
        try:
            admin.revoke_key(a, key_id, body.reason)
        except KeyError as e:
            raise HTTPException(404, {"error": "NOT_FOUND"}) from e
        return {"status": "revoked"}

    @app.post("/admin/v1/subjects/{principal_id}/erase")
    def erase_subject(a: Actor, principal_id: str, body: ReasonBody) -> dict[str, Any]:
        """Crypto-shred a person (ADR-005): delete their key so their evidence can never be
        linked to them again. Their events stay in the chain and in every total, shown as
        "erased subject". Cannot be undone."""
        can(a, Permission.SUBJECT_ERASE)
        if pz is None:
            raise HTTPException(501, {"error": "NOT_CONFIGURED", "message": "erasure is off"})
        ids = built.infra.identity_store
        p = ids.get_principal(principal_id)
        if p is not None and str(p.type) != PrincipalType.USER.value:
            raise HTTPException(
                400, {"error": "NOT_A_PERSON", "message": "only people can be erased"}
            )
        now = datetime.now(UTC)
        if any(k.is_usable(now) for k in ids.keys_for_principal(principal_id)):
            raise HTTPException(
                409,
                {"error": "KEYS_ACTIVE",
                 "message": "Revoke this person's keys first, so no new evidence is written."},
            )  # fmt: skip
        had = pz.store.pseudonym_of(principal_id)
        if had is None and p is None:  # no evidence and no record names this id
            return {"status": "nothing_to_erase"}
        # A person with records but no evidence yet still gets a pseudonym for the scrub.
        pseudonym = had or str(pz.person(principal_id))
        # Recorded BEFORE the key is deleted, and only with the pseudonym.
        core.record(
            admin_event(
                EventType.SUBJECT_ERASED, a.id, {"subject": pseudonym, "reason": body.reason}
            )
        )
        # The editable records get the same pseudonym (one transaction), THEN the key goes:
        # if the scrub fails, nothing is erased yet and the request can simply be repeated.
        changed = built.scrubber.scrub(principal_id, pseudonym) if built.scrubber else {}
        pz.store.delete(principal_id)
        return {
            "status": "erased",
            "subject": pseudonym,
            "records_changed": changed,
            "notice": "Backups taken before now still hold this person's key and records "
            "until they expire (at most 30 days).",
        }

    @app.post("/admin/v1/keys/{key_id}/rotate")
    def rotate_key(a: Actor, key_id: str) -> dict[str, Any]:
        new = admin.rotate_key(a, key_id)
        return {
            "key": _iso(key_view(new.record)),
            "raw_key": new.raw_key,
            "notice": "Copy this key now. It is shown only once and cannot be recovered.",
        }

    # ------------------------------------------------------------- register
    @app.get("/admin/v1/register")
    def register(a: Actor) -> list[dict[str, Any]]:
        can(a, Permission.REGISTER_READ)
        return [e.model_dump(mode="json") for e in queries.register_entries()]

    @app.put("/admin/v1/register/{register_id}")
    def upsert_register(a: Actor, register_id: str, body: RegisterBody) -> dict[str, Any]:
        if body.entry.id != register_id:
            raise ValueError("register id in path and body differ")
        return admin.upsert_register(a, body.entry, body.approval_ref).model_dump(mode="json")

    # --------------------------------------------------------------- budgets
    @app.get("/admin/v1/budgets")
    def budgets(a: Actor) -> list[dict[str, Any]]:
        can(a, Permission.BUDGET_READ)
        out = []
        for d in queries.budget_definitions():
            st = core.m.budget.state(d.bucket())
            out.append(
                {
                    "scope": d.scope,
                    "period": d.period.value,
                    "limit_micros": d.limit,
                    "hard": d.hard,
                    "bucket": d.bucket(),
                    "effective_limit_micros": max(st.limit, d.limit),
                    "spent_micros": st.spent,
                    "reserved_micros": st.reserved,
                    "alert_thresholds": list(d.alert_thresholds),
                }
            )
        return out

    @app.put("/admin/v1/budgets")
    def define_budget(a: Actor, body: BudgetBody) -> dict[str, str]:
        admin.define_budget(
            a, BudgetDefinition(body.scope, body.period, body.limit_micros, body.hard)
        )
        return {"status": "saved"}

    @app.get("/admin/v1/rate-limits")
    def rate_limits(a: Actor) -> list[dict[str, Any]]:
        can(a, Permission.BUDGET_READ)
        return [
            {
                "scope": r.scope,
                "requests_per_minute": r.requests_per_minute,
                "tokens_per_minute": r.tokens_per_minute,
            }
            for r in queries.rate_limit_definitions()
        ]

    @app.put("/admin/v1/rate-limits")
    def define_rate_limit(a: Actor, body: RateLimitBody) -> dict[str, str]:
        admin.define_rate_limit(
            a, RateLimitDefinition(body.scope, body.requests_per_minute, body.tokens_per_minute)
        )
        return {"status": "saved"}

    @app.delete("/admin/v1/rate-limits")
    def remove_rate_limit(
        a: Actor, scope: str = Query(pattern=r"^[a-z_]+:[^\s]+$", max_length=256)
    ) -> dict[str, str]:
        if not admin.remove_rate_limit(a, scope):
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no rate limit for scope"})
        return {"status": "removed"}

    @app.post("/admin/v1/budgets/override")
    def override(a: Actor, body: OverrideBody) -> dict[str, str]:
        try:
            admin.override_budget(a, body.scope, body.extra_micros, body.reason)
        except KeyError as e:
            raise HTTPException(
                404, {"error": "NOT_FOUND", "message": "no budget for scope"}
            ) from e
        return {"status": "override applied"}

    # ----------------------------------------------------------- kill switch
    @app.get("/admin/v1/killswitch")
    def killswitch(a: Actor) -> list[dict[str, Any]]:
        can(a, Permission.USAGE_READ)
        return [
            {
                "scope": e.scope.value,
                "target": e.target,
                "reason": e.reason,
                "actor_id": e.actor_id,
                "activated_at": e.activated_at.isoformat(),
            }
            for e in built.infra.killswitch_store.active()
        ]

    @app.post("/admin/v1/killswitch")
    def kill(a: Actor, body: KillBody) -> dict[str, str]:
        admin.kill(a, body.scope, body.target, body.reason)
        return {"status": "activated"}

    @app.post("/admin/v1/killswitch/release")
    def unkill(a: Actor, body: KillBody) -> dict[str, str]:
        admin.unkill(a, body.scope, body.target, body.reason)
        return {"status": "released"}

    # ------------------------------------------------------------- approvals
    @app.get("/admin/v1/approvals")
    def approvals(
        a: Actor, state: str | None = "PENDING", limit: int = Query(100, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        if Permission.APPROVAL_DECIDE not in permissions_for(a.roles):
            can(a, Permission.AUDIT_READ)
        # Expiry is evaluated on read, so stale PENDING tickets show as EXPIRED.
        return [
            core.approval_status(t.ticket_id).model_dump(mode="json")
            for t in queries.approvals(state, limit)
        ]

    @app.post("/admin/v1/approvals/{ticket_id}/decide")
    def decide_approval(a: Actor, ticket_id: UUID, body: ApprovalDecisionBody) -> dict[str, Any]:
        return admin.decide_approval(a, ticket_id, body.approve, body.reason).model_dump(
            mode="json"
        )

    # ---------------------------------------------------------------- alerts
    @app.get("/admin/v1/alerts")
    def alerts(a: Actor, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
        can(a, Permission.USAGE_READ)
        return [_iso(alert_view(x)) for x in queries.alerts(limit)]

    # -------------------------------------------------------------- registry
    @app.get("/admin/v1/registry/{kind}")
    def registry(a: Actor, kind: str) -> list[dict[str, Any]]:
        can(a, Permission.REGISTER_READ)
        if kind not in ("provider", "model", "route", "server", "tool"):
            raise HTTPException(404, {"error": "NOT_FOUND"})
        return queries.registry(kind)

    @app.put("/admin/v1/registry/{kind}")
    def upsert_registry(a: Actor, kind: str, body: dict[str, Any]) -> dict[str, str]:
        item: Provider | ModelEntry | Route | McpServer | ToolEntry
        try:
            if kind == "provider":
                item = Provider(**{**body, "kind": ProviderKind(body["kind"])})
            elif kind == "model":
                item = ModelEntry(
                    **{**body, "capabilities": frozenset(body.get("capabilities", ["chat"]))}
                )
            elif kind == "route":
                item = Route(**{**body, "targets": tuple(body["targets"])})
            elif kind == "server":
                item = McpServer(**body)
            elif kind == "tool":
                item = ToolEntry(
                    **{**body, "impact": ToolImpact(body["impact"]), "risk": ToolRisk(body["risk"])}
                )
            else:
                raise HTTPException(404, {"error": "NOT_FOUND"})
        except (TypeError, KeyError) as e:
            raise ValueError(f"invalid {kind}: {e}") from e
        admin.upsert_registry(a, item)
        return {"status": "saved"}

    # ------------------------------------------------------------- readiness
    @app.get("/admin/v1/readiness")
    def readiness(a: Actor) -> dict[str, Any]:
        """What a company must set up before its first AI call, in order. Each step is
        checked against the live configuration (nothing stored), so it ticks itself."""
        can(a, Permission.REGISTER_READ)
        reg, cat = core.m.registry, core.m.prices
        providers = [x for x in queries.registry("provider") if x.get("active", True)]
        has_key = any(x.get("credential_ref") for x in providers)
        uses = [e for e in queries.register_entries() if e.active]
        allowed = sorted({m for e in uses for m in e.allowed_models})

        def routable(m: str) -> bool:
            try:
                return reg is not None and bool(reg.resolve_route(m))
            except Exception:  # noqa: BLE001 - an unknown or disabled model is "not ready"
                return False

        def priced(m: str) -> bool:
            try:
                return cat is not None and cat.price_at(m) is not None
            except Exception:  # noqa: BLE001
                return False

        usable = [m for m in allowed if routable(m)]
        unpriced = [m for m in usable if not priced(m)]
        hard = [d for d in queries.budget_definitions() if d.hard]
        now = datetime.now(UTC)
        live_keys = [k for k in queries.keys(None) if k.status.value == "ACTIVE"
                     and (k.expires_at is None or k.expires_at > now)]  # fmt: skip
        steps = [
            {"key": "ai_key", "label": "Add your AI provider key", "done": has_key,
             "detail": "Your OpenAI, Anthropic or Azure key, stored encrypted."},
            {"key": "ai_use", "label": "Describe an approved AI use", "done": bool(uses),
             "detail": "For example: Coding assistant. Its rules apply to every key under it."},
            {"key": "model", "label": "Add a model and allow it", "done": bool(usable),
             "detail": "A model from your provider, allowed for your AI use."},
            {"key": "price", "label": "Set the model's price",
             "done": bool(usable) and not unpriced, "missing": unpriced,
             "detail": "Needed to count spending. Missing: " + ", ".join(unpriced)
             if unpriced else "Needed to count spending."},
            {"key": "budget", "label": "Set a spending limit", "done": bool(hard),
             "detail": "Every AI call needs a limit, e.g. US$100 a month for the company."},
            {"key": "captain_key", "label": "Create a Captain key", "done": bool(live_keys),
             "detail": "Given to a person or app to use AI through Captain."},
        ]  # fmt: skip
        return {"steps": steps, "ready_for_keys": all(x["done"] for x in steps[:5])}

    # ---------------------------------------------------------------- prices
    # Dollars per million tokens, as on the provider's pricing page. Prices drive budgets
    # and cost figures (not what the provider bills). A change is a new version.
    @app.get("/admin/v1/prices")
    def prices(a: Actor) -> list[dict[str, Any]]:
        can(a, Permission.REGISTER_READ)
        cat = core.m.prices
        return [
            {"model": x.model, "input_usd_per_m": x.input_per_m / 1e6,
             "output_usd_per_m": x.output_per_m / 1e6,
             "cached_input_usd_per_m": None if x.cached_input_per_m is None
             else x.cached_input_per_m / 1e6,
             "since": x.effective_from.isoformat(), "version": x.version}
            for x in (cat.latest() if cat else [])
        ]  # fmt: skip

    @app.put("/admin/v1/prices/{alias}")
    def set_price(a: Actor, alias: str, body: PriceBody) -> dict[str, str]:
        can(a, Permission.REGISTER_WRITE)
        cat, reg = core.m.prices, core.m.registry
        model = reg.usable_model(alias) if reg else None
        if cat is None or model is None:
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no such enabled model"})
        now = datetime.now(UTC)

        def micro(v: float | None) -> int | None:
            return None if v is None else round(v * 1_000_000)

        price = ModelPrice(
            alias, model.provider_id, "console-" + now.strftime("%Y%m%d%H%M%S%f"),
            now - timedelta(seconds=1), micro(body.input_usd_per_m) or 0,
            micro(body.output_usd_per_m) or 0,
            cached_input_per_m=micro(body.cached_input_usd_per_m),
        )  # fmt: skip
        core.record(admin_event(EventType.REGISTRY_CHANGED, a.id, {
            "kind": "price", "key": alias, "input_usd_per_m": body.input_usd_per_m,
            "output_usd_per_m": body.output_usd_per_m}))  # fmt: skip
        cat.publish(price)
        return {"status": "saved"}

    # ------------------------------------------------------- audit & reports
    @app.post("/admin/v1/ledger/verify")
    def verify(a: Actor) -> dict[str, Any]:
        can(a, Permission.AUDIT_READ)
        r = core.m.ledger.verify()
        return {
            "ok": r.ok,
            "entries_checked": r.entries_checked,
            "anchors_checked": r.anchors_checked,
            "problems": r.problems[:50],
        }

    @app.get("/admin/v1/evidence")
    def evidence(a: Actor, framework: str, days: int = Query(30, ge=1, le=3660)) -> Response:
        can(a, Permission.AUDIT_EXPORT)
        if framework not in FRAMEWORKS:
            raise ValueError(f"framework must be one of {FRAMEWORKS}")
        since, until = _period(days)
        pkg = export_evidence(raw_queries.events(since, until), framework, since, until)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in pkg.files.items():
                z.writestr(name, data)
            z.writestr("manifest.json", json.dumps(pkg.manifest, indent=2))
        fname = f"captain-evidence-{framework}-{until.date().isoformat()}.zip"
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/admin/v1/reports/monthly", response_model=None)
    def monthly(
        a: Actor,
        year: int = Query(ge=2024, le=2100),
        month: int = Query(ge=1, le=12),
        format: str = Query("json", pattern="^(json|xlsx)$"),
    ) -> Response | dict[str, Any]:
        can(a, Permission.USAGE_READ)
        rep = monthly_report(queries.events(*month_bounds(year, month)), year, month)
        if format == "xlsx":
            return Response(
                to_xlsx(report_sheets(rep)),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="captain-report-{year}-{month:02d}.xlsx"'
                    )
                },
            )
        return rep

    @app.get("/admin/v1/reports/pilot", response_model=None)
    def pilot(
        a: Actor,
        days: int = Query(14, ge=1, le=366),
        format: str = Query("json", pattern="^(json|md)$"),
    ) -> Response | dict[str, Any]:
        """Pilot metrics and the MVP acceptance checklist (see captain/pilot_report.py)."""
        from captain.pilot_report import build_report, render

        can(a, Permission.USAGE_READ)
        rep = build_report(queries, days)
        if format == "md":
            return Response(
                render(rep),
                media_type="text/markdown",
                headers={"Content-Disposition": 'attachment; filename="captain-pilot-report.md"'},
            )
        return rep

    @app.get("/admin/v1/reports/chargeback", response_model=None)
    def charge(
        a: Actor,
        days: int = Query(30, ge=1, le=366),
        format: str = Query("json", pattern="^(json|csv)$"),
    ) -> Response | list[dict[str, Any]]:
        can(a, Permission.BUDGET_READ)
        since, until = _period(days)
        rows = chargeback(settled_costs(queries.events(since, until)), since.date(), until.date())
        if format == "csv":
            return Response(
                to_csv(rows),
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="captain-chargeback.csv"'},
            )
        return rows

    @app.get("/admin/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if static_dir and static_dir.exists():  # built dashboard served from the same origin
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="console-assets")
        index = static_dir / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        def console(path: str) -> Response:
            # Single-page app: any non-API path returns index.html so deep links and
            # refreshes work. API paths never reach here (registered first).
            if path.startswith("admin/"):
                raise HTTPException(404, {"error": "NOT_FOUND"})
            f = (static_dir / path).resolve()
            if path and f.is_file() and static_dir.resolve() in f.parents:
                return Response(f.read_bytes(), media_type=_media(f.suffix))
            return Response(index.read_bytes(), media_type="text/html")

    return app
