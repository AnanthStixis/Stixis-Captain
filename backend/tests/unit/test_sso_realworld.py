"""SSO behaviour that real identity providers need and the mock one does not exercise:
token endpoint authentication method, authorized party, Entra group overage, group-id
case, and app roles. A fake provider on httpx.MockTransport signs real RS256 ID tokens."""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from captain.api.admin_auth import (
    AdminSsoConfig,
    GroupsOverageError,
    NoRoleError,
    actor_from_claims,
)
from captain.api.admin_sso import OidcClient, SsoClientConfig, SsoError
from captain.core.identity.oidc import IssuerConfig

ISSUER = "https://login.example.com/tenant-1/v2.0"
CLIENT = "captain-console"
SECRET = "s3cret/with+odd&chars=" + "x" * 20
REDIRECT = "https://captain.example.com/admin/v1/auth/callback"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {
    **json.loads(RSAAlgorithm.to_jwk(KEY.public_key())),
    "kid": "k1",
    "alg": "RS256",
    "use": "sig",
}


class FakeIdp:
    def __init__(self, auth_methods: Any = "absent", claims: dict[str, Any] | None = None) -> None:
        self.auth_methods = auth_methods
        self.claims = claims or {}
        self.token_requests: list[httpx.Request] = []

    def meta(self) -> dict[str, Any]:
        m: dict[str, Any] = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/keys",
        }
        if self.auth_methods != "absent":
            m["token_endpoint_auth_methods_supported"] = self.auth_methods
        return m

    def id_token(self, **over: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT,
            "sub": "u1",
            "iat": now,
            "exp": now + 300,
            "nonce": "n-1",
            **self.claims,
            **over,
        }
        return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("openid-configuration"):
            return httpx.Response(200, json=self.meta())
        if path.endswith("/keys"):
            return httpx.Response(200, json={"keys": [JWK]})
        if path.endswith("/token"):
            self.token_requests.append(req)
            return httpx.Response(200, json={"id_token": self.id_token()})
        return httpx.Response(404)

    def client(self) -> OidcClient:
        cfg = SsoClientConfig(ISSUER, CLIENT, SECRET, REDIRECT)
        return OidcClient(cfg, httpx.Client(transport=httpx.MockTransport(self.handler)))


def form(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


# ------------------------------------------------------- token endpoint auth


def test_missing_auth_method_list_means_basic_per_the_discovery_spec() -> None:
    idp = FakeIdp()
    c = idp.client()
    assert c.token_auth() == "client_secret_basic"
    c.exchange("code-1", "verifier", "n-1")
    (req,) = idp.token_requests
    assert "client_secret" not in form(req)  # the secret is not in the body
    scheme, _, blob = req.headers["authorization"].partition(" ")
    user, _, pw = base64.b64decode(blob).decode().partition(":")
    from urllib.parse import unquote

    assert scheme == "Basic" and unquote(user) == CLIENT and unquote(pw) == SECRET
    assert (
        form(req)["code_verifier"] == "verifier" and form(req)["grant_type"] == "authorization_code"
    )


def test_post_is_preferred_when_offered_and_unknown_methods_are_refused() -> None:
    idp = FakeIdp(["client_secret_basic", "client_secret_post"])
    idp.client().exchange("c", "v", "n-1")
    req = idp.token_requests[0]
    assert form(req)["client_secret"] == SECRET and "authorization" not in req.headers
    with pytest.raises(SsoError, match="no client secret authentication"):
        FakeIdp(["private_key_jwt", "tls_client_auth"]).client().token_auth()


# --------------------------------------------------------------- ID token


def test_valid_token_passes_and_wrong_nonce_audience_or_issuer_fail() -> None:
    assert FakeIdp().client().exchange("c", "v", "n-1")["sub"] == "u1"
    with pytest.raises(SsoError, match="nonce"):
        FakeIdp().client().exchange("c", "v", "another-nonce")

    class WrongAud(FakeIdp):
        def handler(self, req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/token"):
                return httpx.Response(200, json={"id_token": self.id_token(aud="someone-else")})
            return super().handler(req)

    from captain.core.decision_api import AuthenticationError

    with pytest.raises(AuthenticationError):
        WrongAud().client().exchange("c", "v", "n-1")


def test_several_audiences_require_this_client_as_authorized_party() -> None:
    class Multi(FakeIdp):
        azp: str | None = None

        def handler(self, req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/token"):
                extra = {"azp": self.azp} if self.azp else {}
                return httpx.Response(
                    200, json={"id_token": self.id_token(aud=[CLIENT, "api://other"], **extra)}
                )
            return super().handler(req)

    idp = Multi()
    with pytest.raises(SsoError, match="authorized party"):
        idp.client().exchange("c", "v", "n-1")
    idp.azp = "someone-else"
    with pytest.raises(SsoError, match="authorized party"):
        idp.client().exchange("c", "v", "n-1")
    idp.azp = CLIENT
    assert idp.client().exchange("c", "v", "n-1")["sub"] == "u1"


# ------------------------------------------------------------- group mapping

MAP = {
    "11111111-AAAA-bbbb-cccc-000000000001": ("admin", "approver"),
    "Captain Viewers": ("viewer",),
}
CFG = AdminSsoConfig(IssuerConfig(ISSUER, CLIENT, ""), group_roles=MAP)


def test_group_ids_match_regardless_of_case_but_names_still_work() -> None:
    a = actor_from_claims(CFG, {"sub": "u", "groups": ["11111111-aaaa-BBBB-cccc-000000000001"]})
    assert a.roles == {"admin", "approver"}
    assert actor_from_claims(CFG, {"sub": "u", "groups": ["Captain Viewers"]}).roles == {"viewer"}
    with pytest.raises(NoRoleError):
        actor_from_claims(CFG, {"sub": "u", "groups": ["some-other-group"]})
    with pytest.raises(NoRoleError):  # an empty list is "no groups", not overage
        actor_from_claims(CFG, {"sub": "u", "groups": []})


def test_entra_group_overage_is_reported_as_overage_not_as_no_role() -> None:
    for claims in (
        {"sub": "u", "_claim_names": {"groups": "src1"}, "_claim_sources": {"src1": {}}},
        {"sub": "u", "hasgroups": True},
    ):
        with pytest.raises(GroupsOverageError):
            actor_from_claims(CFG, claims)
    # overage only matters for the groups claim; app roles are not affected by it
    roles_cfg = AdminSsoConfig(
        IssuerConfig(ISSUER, CLIENT, ""),
        roles_claim="roles",
        group_roles={"Captain.Admin": ("admin",)},
    )
    ok = actor_from_claims(roles_cfg, {"sub": "u", "roles": ["Captain.Admin"], "hasgroups": True})
    assert ok.roles == {"admin"}


def test_overage_shows_the_console_a_specific_error_code() -> None:
    from captain.api.admin_sso import SsoService
    from captain.api.state import InMemoryStateStore

    idp = FakeIdp(claims={"_claim_names": {"groups": "src1"}})
    idp.claims["nonce"] = "n-1"
    cfg = SsoClientConfig(ISSUER, CLIENT, SECRET, REDIRECT)
    sso = SsoService(
        cfg,
        CFG,
        InMemoryStateStore(),
        b"k" * 32,
        http=httpx.Client(transport=httpx.MockTransport(idp.handler)),
    )
    sealed = sso.login_cookie.seal("st", "n-1", "ver")
    with pytest.raises(SsoError) as e:
        sso.finish({"state": "st", "code": "c"}, sealed)
    assert e.value.code == "groups_overage"
