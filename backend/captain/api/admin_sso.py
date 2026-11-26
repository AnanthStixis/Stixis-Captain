"""Browser SSO for the Captain Console (OIDC Authorization Code + PKCE, server side).

Captain is a confidential OIDC client. The browser never holds an identity token:

  1. GET  /admin/v1/auth/login     create state, nonce and a PKCE verifier; keep them in a
                                   short-lived ENCRYPTED HttpOnly cookie; redirect to the IdP.
  2. GET  /admin/v1/auth/callback  check state, exchange the code (client secret + PKCE
                                   verifier) at the token endpoint, verify the ID token
                                   (signature, issuer, audience, expiry, nonce), map IdP
                                   groups to Captain roles, create a server-side session,
                                   set an HttpOnly session cookie.
  3. GET  /admin/v1/auth/session   the console learns who is signed in and gets its CSRF token.
  4. POST /admin/v1/auth/logout    deletes the server-side session.

Session cookies are random ids; only their SHA-256 is stored, so a database leak cannot
be replayed. State-changing requests that rely on the cookie also need the session's
CSRF token (double submit) and an allowed Origin. Sessions end after an absolute
lifetime; a deleted or expired session is useless even if the cookie is kept.

Not covered: RP-initiated logout at the IdP (the user stays signed in at the IdP) and
refresh tokens (a session simply ends and the user signs in again).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from captain.api.admin_auth import (
    AdminSsoConfig,
    GroupsOverageError,
    NoRoleError,
    actor_from_claims,
)
from captain.api.state import StateStore
from captain.core.admin import AdminActor
from captain.core.decision_api import AuthenticationError
from captain.core.identity.oidc import HttpJwks, IssuerConfig, is_secure_url, verify_jwt

log = logging.getLogger("captain.admin.sso")

LOGIN_TTL_S = 600
SESSION_KIND = "admin_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class SsoClientConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str  # https://<console host>/admin/v1/auth/callback (register at the IdP)
    scopes: str = "openid profile email"
    roles_claim: str = "groups"
    name_claim: str = "preferred_username"
    group_roles: dict[str, tuple[str, ...]] | None = None
    session_lifetime_s: int = 8 * 3600

    def __post_init__(self) -> None:
        if len(self.client_secret) < 16:
            raise ValueError("SSO client secret looks too short")
        if not is_secure_url(self.redirect_uri):
            raise ValueError("SSO redirect_uri must be HTTPS (HTTP only for localhost development)")

    @property
    def secure_cookies(self) -> bool:
        return self.redirect_uri.startswith("https://")

    @property
    def origin(self) -> str:
        u = urlparse(self.redirect_uri)
        return f"{u.scheme}://{u.netloc}"


class CsrfError(Exception):
    """A cookie-authenticated request failed CSRF checks. 403, not 401: the user is still
    signed in, the request is just not accepted."""


class SsoError(AuthenticationError):
    """Safe-to-show failure code for the console: denied | state | no_role | failed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


class OidcClient:
    def __init__(self, cfg: SsoClientConfig, http: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self._http = http or httpx.Client(timeout=5.0)
        self._meta: dict[str, Any] | None = None
        self._jwks: HttpJwks | None = None

    def metadata(self) -> dict[str, Any]:
        if self._meta is None:
            url = self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
            if not is_secure_url(url):
                raise SsoError("failed", "identity provider must use HTTPS")
            try:
                r = self._http.get(url)
                r.raise_for_status()
                meta: dict[str, Any] = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise SsoError(
                    "failed", f"identity provider discovery failed: {type(e).__name__}"
                ) from e
            # Mix-up protection: the document must describe the issuer we configured.
            if meta.get("issuer") != self.cfg.issuer:
                raise SsoError("failed", "identity provider issuer mismatch")
            for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not is_secure_url(str(meta.get(k, ""))):
                    raise SsoError("failed", f"identity provider {k} missing or insecure")
            self._meta = meta
        return self._meta

    def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        q = {
            "response_type": "code",
            "client_id": self.cfg.client_id,
            "redirect_uri": self.cfg.redirect_uri,
            "scope": self.cfg.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        endpoint = str(self.metadata()["authorization_endpoint"])
        return endpoint + "?" + urlencode(q)

    def token_auth(self) -> str:
        """How this client authenticates at the token endpoint. Per OpenID Connect Discovery
        a provider that does not say defaults to client_secret_basic; we prefer
        client_secret_post when it is offered."""
        offered = self.metadata().get("token_endpoint_auth_methods_supported")
        methods = offered if isinstance(offered, list) and offered else ["client_secret_basic"]
        if "client_secret_post" in methods:
            return "client_secret_post"
        if "client_secret_basic" in methods:
            return "client_secret_basic"
        raise SsoError("failed", "identity provider supports no client secret authentication")

    def exchange(self, code: str, verifier: str, nonce: str) -> dict[str, Any]:
        meta = self.metadata()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.cfg.redirect_uri,
            "client_id": self.cfg.client_id,
            "code_verifier": verifier,
        }
        headers = {"Accept": "application/json"}
        basic: dict[str, Any] = {}
        if self.token_auth() == "client_secret_post":
            form["client_secret"] = self.cfg.client_secret
        else:  # RFC 6749 §2.3.1: the id and secret are form-urlencoded before Basic encoding
            basic["auth"] = (
                quote(self.cfg.client_id, safe=""),
                quote(self.cfg.client_secret, safe=""),
            )
        try:
            r = self._http.post(meta["token_endpoint"], data=form, headers=headers, **basic)
        except httpx.HTTPError as e:
            raise SsoError("failed", f"token endpoint unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            raise SsoError("failed", f"token endpoint refused the code (HTTP {r.status_code})")
        id_token = (r.json() or {}).get("id_token")
        if not isinstance(id_token, str):
            raise SsoError("failed", "token response has no id_token")
        if self._jwks is None:
            icfg = IssuerConfig(self.cfg.issuer, self.cfg.client_id, meta["jwks_uri"])
            self._jwks = HttpJwks({self.cfg.issuer: icfg}, client=self._http)
        icfg = IssuerConfig(self.cfg.issuer, self.cfg.client_id, meta["jwks_uri"])
        claims = verify_jwt(id_token, {self.cfg.issuer: icfg}, self._jwks, what="ID")
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise SsoError("failed", "ID token nonce does not match this login")
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.cfg.client_id:
            # OIDC Core §3.1.3.7: with several audiences the token must name us as authorized party.
            raise SsoError("failed", "ID token authorized party is not this client")
        return claims


class LoginCookie:
    """Encrypted + authenticated (Fernet) login transaction: state, nonce, PKCE verifier."""

    NAME = "captain_login"

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("session secret must be at least 32 bytes")
        key = HKDF(algorithm=SHA256(), length=32, salt=None, info=b"captain-login-cookie").derive(
            secret
        )
        self._f = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, state: str, nonce: str, verifier: str) -> str:
        return self._f.encrypt(
            json.dumps({"s": state, "n": nonce, "v": verifier}).encode()
        ).decode()

    def open(self, token: str) -> tuple[str, str, str]:
        try:
            d = json.loads(self._f.decrypt(token.encode(), ttl=LOGIN_TTL_S))
            return str(d["s"]), str(d["n"]), str(d["v"])
        except (InvalidToken, ValueError, KeyError) as e:
            raise SsoError("state", "login session expired or invalid; start again") from e


class SessionManager:
    def __init__(self, store: StateStore, lifetime_s: int, now: Callable[[], float] = time.time):
        self._s = store
        self._life = lifetime_s
        self._now = now

    @staticmethod
    def _key(session_id: str) -> str:
        return hashlib.sha256(session_id.encode()).hexdigest()

    def create(self, actor: AdminActor) -> tuple[str, str]:
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)  # fresh id per login
        self._s.put(
            SESSION_KIND,
            self._key(sid),
            "session",
            {
                "id": actor.id,
                "roles": sorted(actor.roles),
                "csrf": csrf,
                "exp": self._now() + self._life,
            },
        )
        return sid, csrf

    def get(self, session_id: str) -> tuple[AdminActor, str] | None:
        d = self._s.get(SESSION_KIND, self._key(session_id), "session")
        if d is None or d["exp"] <= self._now():
            return None
        return AdminActor(d["id"], frozenset(d["roles"])), str(d["csrf"])

    def delete(self, session_id: str) -> None:
        self._s.delete(SESSION_KIND, self._key(session_id))


class SsoService:
    def __init__(
        self,
        cfg: SsoClientConfig,
        sso_cfg: AdminSsoConfig,
        store: StateStore,
        session_secret: bytes,
        http: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
        extra_origins: tuple[str, ...] = (),
    ) -> None:
        self.cfg, self.mapping = cfg, sso_cfg
        self.client = OidcClient(cfg, http)
        self.login_cookie = LoginCookie(session_secret)
        self.sessions = SessionManager(store, cfg.session_lifetime_s, now)
        self.allowed_origins = {cfg.origin, *extra_origins}
        prefix = "__Host-" if cfg.secure_cookies else ""
        self.cookie_name = f"{prefix}captain_session"

    def start(self) -> tuple[str, str]:
        """Returns (authorization URL, sealed login cookie value)."""
        state, nonce, verifier = (
            secrets.token_urlsafe(24),
            secrets.token_urlsafe(24),
            secrets.token_urlsafe(48),
        )
        return self.client.authorization_url(state, nonce, verifier), self.login_cookie.seal(
            state, nonce, verifier
        )

    def finish(self, query: dict[str, str], sealed: str | None) -> tuple[AdminActor, str, str]:
        """Returns (actor, session id, csrf token) or raises SsoError."""
        if query.get("error"):
            raise SsoError("denied", f"identity provider returned: {query['error'][:60]}")
        if not sealed:
            raise SsoError("state", "login session expired or invalid; start again")
        state, nonce, verifier = self.login_cookie.open(sealed)
        if not hmac.compare_digest(query.get("state", ""), state):
            raise SsoError("state", "state mismatch (possible cross-site request)")
        code = query.get("code")
        if not code:
            raise SsoError("failed", "no authorization code returned")
        claims = self.client.exchange(code, verifier, nonce)
        try:
            actor = actor_from_claims(self.mapping, claims)
        except GroupsOverageError as e:
            raise SsoError("groups_overage", str(e)) from e
        except NoRoleError as e:
            raise SsoError("no_role", str(e)) from e
        sid, csrf = self.sessions.create(actor)
        return actor, sid, csrf

    def check_csrf(self, method: str, headers: Any, expected: str) -> None:
        """Cookie-authenticated, state-changing requests need the CSRF token and a
        same-site Origin. (Bearer-token requests are not cookie-based and skip this.)"""
        if method in SAFE_METHODS:
            return
        if not hmac.compare_digest(headers.get("x-csrf-token", ""), expected):
            raise CsrfError("missing or invalid CSRF token")
        origin = headers.get("origin")
        if origin is not None and origin not in self.allowed_origins:
            raise CsrfError("request origin is not allowed")
