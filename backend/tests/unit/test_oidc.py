from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from captain.core.decision_api import AuthenticationError
from captain.core.identity.oidc import (
    HttpJwks,
    InMemoryWorkloadBindings,
    IssuerConfig,
    StaticJwks,
    WorkloadAuthenticator,
    WorkloadBinding,
)
from captain.core.models import (
    AutonomyLevel,
    ContextHints,
    Credential,
    Environment,
    PrincipalType,
    RiskTier,
)
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness

ISS = "https://oidc.eks.example/id/ABC"
AUD = "captain"
SUB = "system:serviceaccount:fraud:triage"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(k: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    d: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(k.public_key()))
    return {**d, "kid": kid, "alg": "RS256", "use": "sig"}


def token(
    claims: dict[str, Any] | None = None, key: Any = KEY, kid: str = "k1", alg: str = "RS256"
) -> str:
    now = int(time.time())
    base = {"iss": ISS, "aud": AUD, "sub": SUB, "iat": now, "exp": now + 600}
    return jwt.encode({**base, **(claims or {})}, key, algorithm=alg, headers={"kid": kid})


def make_auth(bound: bool = True) -> WorkloadAuthenticator:
    b = InMemoryWorkloadBindings()
    if bound:
        b.put(
            WorkloadBinding(ISS, SUB, "svc", "AI-svc", "proj-1", "app-svc", Environment.DEVELOPMENT)
        )
    return WorkloadAuthenticator(
        {ISS: IssuerConfig(ISS, AUD, "https://x/jwks")},
        StaticJwks({ISS: {"keys": [jwk(KEY, "k1")]}}),
        b,
    )


def test_valid_token_maps_to_binding() -> None:
    rec = make_auth().authenticate(token())
    assert (rec.principal_id, rec.project_id, rec.application_id) == ("svc", "proj-1", "app-svc")
    assert rec.key_id.startswith("wl_") and rec.secret_hash == ""


PUB_PEM = KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(lambda: token({"aud": "someone-else"}), id="wrong-audience"),
        pytest.param(lambda: token({"exp": int(time.time()) - 120}), id="expired"),
        pytest.param(lambda: token({"iss": "https://evil"}), id="untrusted-issuer"),
        pytest.param(lambda: token(key=OTHER), id="wrong-key"),
        pytest.param(lambda: token(kid="unknown"), id="unknown-kid"),
        pytest.param(
            lambda: jwt.encode(
                {"iss": ISS, "aud": AUD, "sub": SUB, "iat": 1, "exp": 2**40},
                None,
                algorithm="none",
                headers={"kid": "k1"},
            ),
            id="alg-none",
        ),
        pytest.param(lambda: _hs256_with_public_key(), id="hmac-key-confusion"),
        pytest.param(lambda: "not.a.jwt", id="garbage"),
    ],
)
def test_rejections(bad: Any) -> None:
    with pytest.raises(AuthenticationError):
        make_auth().authenticate(bad())


def _hs256_with_public_key() -> str:
    """Classic attack: sign with HMAC using the issuer's PUBLIC key as the secret."""
    import base64
    import hashlib
    import hmac

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    now = int(time.time())
    h = b64(json.dumps({"alg": "HS256", "kid": "k1", "typ": "JWT"}).encode())
    p = b64(json.dumps({"iss": ISS, "aud": AUD, "sub": SUB, "iat": now, "exp": now + 600}).encode())
    sig = b64(hmac.new(PUB_PEM, f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


def test_missing_claims_rejected() -> None:
    now = int(time.time())
    t = jwt.encode(
        {"iss": ISS, "aud": AUD, "exp": now + 60, "iat": now},
        KEY,
        algorithm="RS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(AuthenticationError):
        make_auth().authenticate(t)


def test_unbound_workload_refused() -> None:
    with pytest.raises(AuthenticationError, match="not bound"):
        make_auth(bound=False).authenticate(token())


def test_http_jwks_refetches_on_rotation() -> None:
    served = {"keys": [jwk(KEY, "k1")]}
    calls: list[int] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=served)

    cfg = {ISS: IssuerConfig(ISS, AUD, "https://idp/jwks")}
    jw = HttpJwks(cfg, min_refresh_s=0, client=httpx.Client(transport=httpx.MockTransport(h)))
    b = InMemoryWorkloadBindings()
    b.put(WorkloadBinding(ISS, SUB, "svc", "AI-svc", "proj-1", "app-svc", Environment.DEVELOPMENT))
    auth = WorkloadAuthenticator(cfg, jw, b)
    auth.authenticate(token())
    auth.authenticate(token())
    assert len(calls) == 1  # cached
    served["keys"].append(jwk(OTHER, "k2"))  # issuer rotates keys
    auth.authenticate(token(key=OTHER, kid="k2"))
    assert len(calls) == 2


def test_jwks_requires_https() -> None:
    cfg = {ISS: IssuerConfig(ISS, AUD, "http://idp.example/jwks")}
    with pytest.raises(AuthenticationError):
        HttpJwks(cfg).key(ISS, "k1")


def test_engine_resolves_workload_and_kill_switch_applies() -> None:
    from captain.core.identity.killswitch import KillScope
    from captain.core.models import ActionType, Channel, GovernanceRequest, ReasonCode

    h = CaptainCoreHarness()
    h.register_key(
        "unused", make_context("svc", RiskTier.TIER_3, AutonomyLevel.A2, PrincipalType.APPLICATION)
    )
    h.core.m.workload = make_auth()
    ctx = h.resolve(Credential(kind="workload_oidc", secret=token()), ContextHints())
    assert ctx.principal.id == "svc" and ctx.key_id and ctx.key_id.startswith("wl_")
    h.core.kill(KillScope.KEY, ctx.key_id, "compromised workload", "admin")
    req = GovernanceRequest(
        trace_id="t",
        context=ctx,
        channel=Channel.API,
        action=ActionType.MODEL_CALL,
        model="claude-sonnet",
    )
    assert h.core.decide(req).reason_code is ReasonCode.KILL_SWITCH_ACTIVE


def test_engine_refuses_oidc_when_not_configured() -> None:
    h = CaptainCoreHarness()
    with pytest.raises(AuthenticationError):
        h.resolve(Credential(kind="workload_oidc", secret=token()), ContextHints())
