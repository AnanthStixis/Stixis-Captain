"""Workload identity: OIDC tokens instead of long-lived keys (CTO credential hierarchy).

Apps and agents present a short-lived OIDC JWT issued by their platform:
Kubernetes projected service-account tokens, AWS IAM (via an OIDC provider),
Azure Managed Identity / Entra workload identity, GitHub Actions, etc.

Verification rules:
- Only explicitly configured issuers are trusted; each has its own audience.
- Only asymmetric algorithms (RS256, ES256, …). `none` and HMAC are refused, so a
  token can't be "signed" with a public key used as an HMAC secret.
- exp, iat, iss, aud and sub are required; 30 s clock leeway.
- Signing keys come from the issuer's JWKS, cached and refreshed on unknown `kid`.
- (issuer, subject) must map to an explicit WorkloadBinding. An unknown workload is
  refused; it is never guessed into a project.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
import jwt
from jwt import PyJWK

from captain.core.decision_api import AuthenticationError
from captain.core.identity.model import KeyRecord
from captain.core.models import Environment

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def is_secure_url(url: str) -> bool:
    """HTTPS, or plain HTTP to this machine only (a local development identity provider)."""
    parts = urlparse(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS)


ALLOWED_ALGS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")


@dataclass(frozen=True, slots=True)
class IssuerConfig:
    issuer: str
    audience: str
    jwks_uri: str


@dataclass(frozen=True, slots=True)
class WorkloadBinding:
    issuer: str
    subject: str  # e.g. system:serviceaccount:fraud:triage-agent
    principal_id: str
    register_id: str
    project_id: str
    application_id: str
    environment: Environment


class JwksProvider(Protocol):
    def key(self, issuer: str, kid: str) -> Any: ...


class StaticJwks:
    """Fixed key set (tests, or air-gapped deployments with a pinned JWKS)."""

    def __init__(self, jwks_by_issuer: dict[str, dict[str, Any]]) -> None:
        self._keys = {
            (iss, k["kid"]): PyJWK(k).key for iss, s in jwks_by_issuer.items() for k in s["keys"]
        }

    def key(self, issuer: str, kid: str) -> Any:
        k = self._keys.get((issuer, kid))
        if k is None:
            raise AuthenticationError("unknown token signing key")
        return k


class HttpJwks:
    """Fetches each issuer's JWKS over HTTPS, caches it, refetches on an unknown kid
    (key rotation) at most once per `min_refresh_s`."""

    def __init__(
        self,
        issuers: dict[str, IssuerConfig],
        ttl_s: float = 3600,
        min_refresh_s: float = 30,
        client: httpx.Client | None = None,
    ) -> None:
        self._issuers = issuers
        self._ttl, self._min = ttl_s, min_refresh_s
        self._client = client or httpx.Client(timeout=3.0)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def _fetch(self, issuer: str) -> dict[str, Any]:
        cfg = self._issuers[issuer]
        if not is_secure_url(cfg.jwks_uri):
            raise AuthenticationError("JWKS must be fetched over HTTPS")
        try:
            r = self._client.get(cfg.jwks_uri)
            r.raise_for_status()
            keys = {k["kid"]: PyJWK(k).key for k in r.json()["keys"] if "kid" in k}
        except (httpx.HTTPError, ValueError, KeyError, jwt.PyJWKError) as e:
            raise AuthenticationError(f"cannot load JWKS for {issuer}") from e
        with self._lock:
            self._cache[issuer] = (time.monotonic(), keys)
        return keys

    def key(self, issuer: str, kid: str) -> Any:
        with self._lock:
            hit = self._cache.get(issuer)
        now = time.monotonic()
        if hit is None or now - hit[0] > self._ttl:
            keys = self._fetch(issuer)
        else:
            keys = hit[1]
            if kid not in keys and now - hit[0] >= self._min:
                keys = self._fetch(issuer)
        if kid not in keys:
            raise AuthenticationError("unknown token signing key")
        return keys[kid]


class WorkloadBindingStore(Protocol):
    def get(self, issuer: str, subject: str) -> WorkloadBinding | None: ...


class InMemoryWorkloadBindings:
    def __init__(self) -> None:
        self._b: dict[tuple[str, str], WorkloadBinding] = {}

    def put(self, b: WorkloadBinding) -> None:
        self._b[(b.issuer, b.subject)] = b

    def get(self, issuer: str, subject: str) -> WorkloadBinding | None:
        return self._b.get((issuer, subject))


def verify_jwt(
    token: str,
    issuers: dict[str, IssuerConfig],
    jwks: JwksProvider,
    leeway_s: int = 30,
    what: str = "token",
) -> dict[str, Any]:
    """Shared OIDC JWT verification for workloads and administrators: configured
    issuers only, asymmetric algorithms only, required exp/iat/iss/aud/sub."""
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise AuthenticationError(f"malformed {what} token") from e
    alg, kid, iss = header.get("alg"), header.get("kid"), unverified.get("iss")
    if alg not in ALLOWED_ALGS:
        raise AuthenticationError(f"token algorithm {alg!r} not allowed")
    if not isinstance(kid, str) or not isinstance(iss, str) or iss not in issuers:
        raise AuthenticationError("untrusted token issuer")
    cfg = issuers[iss]
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=jwks.key(iss, kid),
            algorithms=[alg],
            audience=cfg.audience,
            issuer=cfg.issuer,
            leeway=leeway_s,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise AuthenticationError(f"{what} token rejected: {type(e).__name__}") from e
    return claims


class WorkloadAuthenticator:
    def __init__(
        self,
        issuers: dict[str, IssuerConfig],
        jwks: JwksProvider,
        bindings: WorkloadBindingStore,
        leeway_s: int = 30,
    ) -> None:
        self._issuers = issuers
        self._jwks = jwks
        self._bindings = bindings
        self._leeway = leeway_s

    def authenticate(self, token: str) -> KeyRecord:
        claims = verify_jwt(token, self._issuers, self._jwks, self._leeway, "workload")
        iss = str(claims["iss"])
        b = self._bindings.get(iss, claims["sub"])
        if b is None:
            raise AuthenticationError("workload is not bound to a Captain principal")
        # A synthetic, non-persisted key record lets the rest of the Core treat workload
        # and virtual-key callers identically (context resolution, budgets, kill switch).
        wid = hashlib.sha256(f"{iss}|{claims['sub']}".encode()).hexdigest()[:16]
        return KeyRecord(
            key_id=f"wl_{wid}",
            secret_hash="",
            principal_id=b.principal_id,
            register_id=b.register_id,
            project_id=b.project_id,
            application_id=b.application_id,
            environment=b.environment,
            created_at=datetime.fromtimestamp(claims["iat"], UTC),
            expires_at=datetime.fromtimestamp(claims["exp"], UTC),
            label=f"oidc:{iss}",
        )
