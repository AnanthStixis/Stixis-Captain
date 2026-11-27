"""Composition root for the Admin API + dashboard:

    uvicorn --factory captain.api.admin_main:create_admin_app_from_env --port 8100

Settings (environment or backend/.env):

CAPTAIN_ADMIN_OIDC     JSON, enables SSO (browser sign-in AND Bearer tokens):
    {"issuer": "https://login.microsoftonline.com/<tenant>/v2.0",
     "client_id": "...", "client_secret": "...",
     "redirect_uri": "https://captain.example.com/admin/v1/auth/callback",
     "scopes": "openid profile email",            (optional)
     "roles_claim": "groups", "name_claim": "preferred_username",
     "group_roles": {"<IdP group id>": ["admin", "approver", "auditor"]},
     "session_lifetime_s": 28800}                 (optional)
CAPTAIN_SESSION_SECRET hex, >= 32 bytes; encrypts the login cookie (required for SSO)
CAPTAIN_DEV_ADMIN_TOKEN  development only, accepted from this machine only
CAPTAIN_CONSOLE_ORIGINS  comma list of extra dashboard origins (e.g. the Vite dev server)
At least one of SSO or the dev token must be configured.
"""

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import create_engine

from captain.api.admin_auth import (
    AdminAuthenticator,
    AdminSsoConfig,
    ChainAuthenticator,
    AdminKeyAuthenticator,
    DevTokenAuthenticator,
    OidcAdminAuthenticator,
)
from captain.api.admin_http import create_admin_app
from captain.api.admin_queries import PgAdminQueries
from captain.api.admin_sso import OidcClient, SsoClientConfig, SsoService
from captain.api.controlplane import PgCompanyStore
from captain.api.org import INVITE_TTL_S, InviteService, PgOrganizationStore
from captain.api.signin_mail import mailer_from_env
from captain.api.sso_live import LiveSso
from captain.api.state import PgStateStore
from captain.config import ConfigError, build_from_env, build_secrets
from captain.core.identity.oidc import HttpJwks, IssuerConfig
from captain.envfile import current_env

CONSOLE_DIST = Path(
    current_env().get("CAPTAIN_CONSOLE_DIR")
    or Path(__file__).resolve().parents[3] / "console" / "dist"
)
DEV_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


class _DiscoveredJwks:
    """JWKS location comes from the IdP's discovery document, fetched on first use."""

    def __init__(self, client: OidcClient) -> None:
        self._client = client
        self._inner: HttpJwks | None = None

    def key(self, issuer: str, kid: str) -> Any:
        if self._inner is None:
            cfg = IssuerConfig(
                issuer, self._client.cfg.client_id, self._client.metadata()["jwks_uri"]
            )
            self._inner = HttpJwks({issuer: cfg})
        return self._inner.key(issuer, kid)


def _user_lookup(built: Any) -> Any:
    """Console roles on the person's Captain user record (matched by email)."""
    ids = built.infra.identity_store

    def lookup(email: str) -> tuple[bool, frozenset[str]] | None:
        p = ids.get_principal(email)
        if p is None or str(p.type) != "USER":
            return None
        return p.active, p.roles

    return lookup


def shared_control_plane(env: dict[str, str]) -> dict[str, Any]:
    """On a shared Captain the Stixis console provisions companies itself and reaches
    them in-process (ADR-008)."""
    if env.get("CAPTAIN_CONTROL_PLANE") != "1" or env.get("CAPTAIN_MULTI_TENANT") != "1":
        return {}
    from captain.tenancy import active

    a = active()
    if "router" not in a:
        return {}
    return {"company_http": a["router"].local_client(), "provision": a["tenancy"].provision}


def create_admin_app_from_env() -> FastAPI:
    env = current_env()
    built, settings = build_from_env()
    engine = create_engine(
        settings.database_url,
        pool_size=int(current_env().get("CAPTAIN_DB_POOL_SIZE", "10")),
        pool_pre_ping=True,
    )
    auths: list[AdminAuthenticator] = []
    sso: SsoService | None = None

    if raw := env.get("CAPTAIN_ADMIN_OIDC"):
        c = json.loads(raw)
        try:
            secret = bytes.fromhex(env.get("CAPTAIN_SESSION_SECRET", ""))
        except ValueError as e:
            raise ConfigError("CAPTAIN_SESSION_SECRET must be hex") from e
        if len(secret) < 32:
            raise ConfigError("CAPTAIN_SESSION_SECRET (hex, >= 32 bytes) is required for SSO")
        client_cfg = SsoClientConfig(
            issuer=c["issuer"],
            client_id=c["client_id"],
            client_secret=c["client_secret"],
            redirect_uri=c["redirect_uri"],
            scopes=c.get("scopes", "openid profile email"),
            roles_claim=c.get("roles_claim", "groups"),
            name_claim=c.get("name_claim", "preferred_username"),
            session_lifetime_s=int(c.get("session_lifetime_s", 8 * 3600)),
        )
        mapping = AdminSsoConfig(
            IssuerConfig(c["issuer"], c["client_id"], ""),
            roles_claim=client_cfg.roles_claim,
            name_claim=client_cfg.name_claim,
            group_roles={g: tuple(r) for g, r in c.get("group_roles", {}).items()},
            user_lookup=_user_lookup(built),
        )
        extra = tuple(
            o.strip()
            for o in env.get(
                "CAPTAIN_CONSOLE_ORIGINS", "" if client_cfg.secure_cookies else DEV_ORIGINS
            ).split(",")
            if o.strip()
        )
        sso = SsoService(
            client_cfg, mapping, PgStateStore(engine, ttl_s=12 * 3600), secret, extra_origins=extra
        )
        auths.append(OidcAdminAuthenticator(mapping, _DiscoveredJwks(sso.client)))

    # SSO: fixed by CAPTAIN_ADMIN_OIDC, or set up by the org admin on the Setup page.
    console_url = env.get("CAPTAIN_PUBLIC_CONSOLE_URL")
    secrets_svc = build_secrets(settings, engine)
    live = LiveSso(
        PgStateStore(engine, ttl_s=12 * 3600),
        _user_lookup(built),
        secrets=secrets_svc,
        console_url=console_url,
        fixed=sso,
        extra_origins=tuple(
            o.strip() for o in env.get("CAPTAIN_CONSOLE_ORIGINS", DEV_ORIGINS).split(",")
            if o.strip()
        ),
    )  # fmt: skip
    if sso is None:
        auths.append(live.bearer())

    dev_token = env.get("CAPTAIN_DEV_ADMIN_TOKEN")
    if dev_token:
        # On the Stixis install the dev token may also manage client companies.
        cp = (
            frozenset({"platform-admin"})
            if env.get("CAPTAIN_CONTROL_PLANE") == "1"
            else frozenset()
        )
        auths.append(DevTokenAuthenticator(dev_token, extra_roles=cp))
    admin_key = env.get("CAPTAIN_PLATFORM_ADMIN_KEY")
    if admin_key and env.get("CAPTAIN_CONTROL_PLANE") == "1":
        auths.append(AdminKeyAuthenticator(admin_key))
    else:
        admin_key = None
    if not auths:
        raise ConfigError(
            "configure CAPTAIN_ADMIN_OIDC (SSO) or CAPTAIN_DEV_ADMIN_TOKEN (dev only)"
        )
    origins = [
        o.strip() for o in env.get("CAPTAIN_CONSOLE_ORIGINS", DEV_ORIGINS).split(",") if o.strip()
    ]
    if sso is not None and sso.cfg.secure_cookies and "CAPTAIN_CONSOLE_ORIGINS" not in env:
        origins = []  # production: same origin only, no CORS
    return create_admin_app(
        built,
        ChainAuthenticator(*auths),
        PgAdminQueries(engine),
        origins,
        CONSOLE_DIST,
        sso=sso,
        dev_token_enabled=bool(dev_token),
        admin_key_enabled=bool(admin_key),
        org_store=PgOrganizationStore(engine),
        ai_keys=secrets_svc,
        live_sso=live,
        companies=PgCompanyStore(engine) if env.get("CAPTAIN_CONTROL_PLANE") == "1" else None,
        **shared_control_plane(env),
        signin_mailer=mailer_from_env(env),
        console_url=console_url,
        invites=InviteService(
            PgStateStore(engine, ttl_s=INVITE_TTL_S),
            sso_enabled=lambda: live.get() is not None,
            secure_cookies=(sso is not None and sso.cfg.secure_cookies)
            or (console_url or "").startswith("https://"),
        ),
    )
