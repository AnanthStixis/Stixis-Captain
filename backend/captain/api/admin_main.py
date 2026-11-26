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
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import create_engine

from captain.api.admin_auth import (
    AdminAuthenticator,
    AdminSsoConfig,
    ChainAuthenticator,
    DevTokenAuthenticator,
    OidcAdminAuthenticator,
)
from captain.api.admin_http import create_admin_app
from captain.api.admin_queries import PgAdminQueries
from captain.api.admin_sso import OidcClient, SsoClientConfig, SsoService
from captain.api.state import PgStateStore
from captain.config import ConfigError, build_from_env
from captain.core.identity.oidc import HttpJwks, IssuerConfig
from captain.envfile import DEFAULT_ENV_FILE, dotenv_values

CONSOLE_DIST = Path(__file__).resolve().parents[3] / "console" / "dist"
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


def create_admin_app_from_env() -> FastAPI:
    env = {**dotenv_values(DEFAULT_ENV_FILE), **os.environ}
    built, settings = build_from_env()
    engine = create_engine(settings.database_url, pool_size=10, pool_pre_ping=True)
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

    dev_token = env.get("CAPTAIN_DEV_ADMIN_TOKEN")
    if dev_token:
        auths.append(DevTokenAuthenticator(dev_token))
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
    )
