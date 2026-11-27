"""Administrator authentication for the Admin API and dashboard.

Production: SSO through the client's identity provider (Entra ID, Okta, …). The
dashboard signs the user in (OIDC Authorization Code + PKCE) and sends the ID/access
token; this module verifies it with the IdP's keys and maps IdP groups to Captain
roles. Captain never sees user passwords.

Development: a static dev admin token, enabled only when CAPTAIN_DEV_ADMIN_TOKEN is
set. It is refused for any non-loopback client so it cannot be used remotely.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from captain.core.admin import AdminActor
from captain.core.decision_api import AuthenticationError
from captain.core.identity.oidc import IssuerConfig, JwksProvider, verify_jwt

log = logging.getLogger("captain.admin.auth")


class AdminAuthenticator(Protocol):
    def authenticate(self, token: str, client_host: str | None) -> AdminActor: ...


@dataclass(frozen=True)
class AdminSsoConfig:
    issuer: IssuerConfig
    roles_claim: str = "groups"  # Entra ID: "groups" or "roles"; Okta: "groups"
    group_roles: Mapping[str, tuple[str, ...]] | None = None  # IdP group -> Captain roles
    name_claim: str = "preferred_username"
    # Looks up the person's Captain user record by email / user name (lowercased):
    # None = no record; otherwise (active, roles). Roles granted there ADD to the IdP's.
    user_lookup: Callable[[str], tuple[bool, frozenset[str]] | None] | None = None


class NoRoleError(AuthenticationError):
    """The user signed in at the identity provider but none of their groups maps to a
    Captain role. Deliberately not a generic failure: the user needs to ask for access."""


class GroupsOverageError(AuthenticationError):
    """The identity provider did not put the user's groups in the token because there are
    too many (Microsoft Entra ID: more than 200 for a JWT). Silently treating that as "no
    roles" would send the user to the wrong helpdesk; the fix is on the IdP side."""


def _groups_overage(claims: Mapping[str, object]) -> bool:
    """Entra signals overage with `_claim_names: {"groups": ...}` (or `hasgroups`), never by
    putting the groups in the token. Only the presence matters, not the value."""
    names = claims.get("_claim_names")
    return bool(claims.get("hasgroups")) or (isinstance(names, dict) and "groups" in names)


def actor_from_claims(cfg: AdminSsoConfig, claims: Mapping[str, object]) -> AdminActor:
    groups = claims.get(cfg.roles_claim)
    if groups is None and cfg.roles_claim == "groups" and _groups_overage(claims):
        raise GroupsOverageError(
            "too many groups for the token; use app roles or limit the groups sent to Captain"
        )
    names: list[object] = (
        [groups] if isinstance(groups, str) else list(groups) if isinstance(groups, list) else []
    )
    # Exact match first; group ids (GUIDs) are case-insensitive, so fall back to casefold.
    mapping = cfg.group_roles or {}
    folded = {k.casefold(): v for k, v in mapping.items()}
    roles = frozenset(
        r for g in names for r in (mapping.get(str(g)) or folded.get(str(g).casefold(), ()))
    )
    name = str(claims.get(cfg.name_claim) or claims["sub"])
    if cfg.user_lookup is not None:
        seen: set[str] = set()
        for v in (claims.get("email"), claims.get(cfg.name_claim)):
            if not isinstance(v, str) or not v.strip() or v.strip().lower() in seen:
                continue
            key = v.strip().lower()
            seen.add(key)
            rec = cfg.user_lookup(key)
            if rec is None:
                continue
            active, extra = rec
            if not active:  # deactivated in Captain: refused whatever the IdP says
                raise NoRoleError("your Captain access has been deactivated")
            roles = roles | extra
    if not roles:
        raise NoRoleError("signed in, but no Captain role is mapped to your groups")
    return AdminActor(name, roles)


class OidcAdminAuthenticator:
    """Verifies an IdP-issued token sent as a Bearer credential (API clients)."""

    def __init__(self, cfg: AdminSsoConfig, jwks: JwksProvider) -> None:
        self._cfg = cfg
        self._jwks = jwks

    def authenticate(self, token: str, client_host: str | None) -> AdminActor:
        claims = verify_jwt(
            token, {self._cfg.issuer.issuer: self._cfg.issuer}, self._jwks, what="admin"
        )
        return actor_from_claims(self._cfg, claims)


class DevTokenAuthenticator:
    """Local development only. Loopback clients only; constant-time comparison."""

    LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}

    def __init__(
        self, token: str, actor_id: str = "dev-admin", extra_roles: frozenset[str] = frozenset()
    ) -> None:
        if len(token) < 32:
            raise ValueError("dev admin token must be at least 32 characters")
        self._token = token
        self._actor = AdminActor(
            actor_id, frozenset({"admin", "approver", "auditor"}) | extra_roles
        )
        log.warning("DEV admin token authentication is ENABLED (development only)")

    def authenticate(self, token: str, client_host: str | None) -> AdminActor:
        if client_host not in self.LOOPBACK:
            raise AuthenticationError("dev admin token is accepted from this machine only")
        if not hmac.compare_digest(token.encode(), self._token.encode()):
            raise AuthenticationError("invalid admin token")
        return self._actor


class AdminKeyAuthenticator:
    """The Stixis super admin's sign-in key (CAPTAIN_PLATFORM_ADMIN_KEY): accepted from any
    address, on the Stixis console only. For installs without SSO yet (QA); keep it long and
    secret, and remove it once SSO is set up."""

    def __init__(self, key: str) -> None:
        if len(key) < 32:
            raise ValueError("CAPTAIN_PLATFORM_ADMIN_KEY must be at least 32 characters")
        self._key = key
        self._actor = AdminActor(
            "stixis-admin-key", frozenset({"admin", "approver", "auditor", "platform-admin"})
        )
        log.warning("Stixis admin key sign-in is ENABLED")

    def authenticate(self, token: str, client_host: str | None) -> AdminActor:
        if not hmac.compare_digest(token.encode(), self._key.encode()):
            raise AuthenticationError("invalid admin key")
        return self._actor


class ChainAuthenticator:
    """Try SSO first, then the dev token (if configured)."""

    def __init__(self, *auths: AdminAuthenticator) -> None:
        if not auths:
            raise ValueError("at least one admin authenticator is required")
        self._auths = auths

    def authenticate(self, token: str, client_host: str | None) -> AdminActor:
        last: AuthenticationError | None = None
        for a in self._auths:
            try:
                return a.authenticate(token, client_host)
            except AuthenticationError as e:
                last = e
        assert last is not None
        raise last
