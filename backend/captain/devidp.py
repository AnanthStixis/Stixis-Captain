"""Mock OpenID Connect identity provider for LOCAL DEVELOPMENT AND TESTS ONLY.

    python -m uvicorn --factory captain.devidp:create_dev_idp_from_env --port 8200

It implements the real protocol so Captain's SSO code is genuinely exercised:
discovery, authorization endpoint (PKCE S256 required, exact redirect_uri match),
token endpoint (client authentication, single-use codes, PKCE verification) and RS256
ID tokens with nonce and groups. Instead of a password it shows a list of fictional
users. NEVER expose it beyond this machine: anyone who can reach it can sign in as
anyone.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jwt.algorithms import RSAAlgorithm

BUTTON_STYLE = "display:block;width:100%;margin:6px 0;padding:10px;font-size:15px;cursor:pointer"

DEV_USERS = {
    "priya": ("priya@corp.example", "Priya Sharma — platform admin", ["grp-captain-admins"]),
    "approver": ("approver-1", "Approver One — approves agent actions", ["grp-approvers"]),
    "ravi": ("ravi@corp.example", "Ravi Kumar — FinOps", ["grp-finops"]),
    "meera": ("meera@corp.example", "Meera Iyer — auditor", ["grp-auditors"]),
    "asha": ("asha@corp.example", "Asha Nair — signed in, but no Captain role", ["grp-everyone"]),
}


@dataclass
class _Code:
    user: str
    client_id: str
    redirect_uri: str
    nonce: str
    challenge: str
    expires: float


async def _form(request: Request) -> dict[str, str]:
    """application/x-www-form-urlencoded body (avoids the python-multipart dependency)."""
    return {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}


def create_idp_app(
    issuer: str, client_id: str, client_secret: str, redirect_uris: list[str]
) -> FastAPI:
    app = FastAPI(title="Captain mock identity provider (dev only)", docs_url=None, redoc_url=None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = {
        **json.loads(RSAAlgorithm.to_jwk(key.public_key())),
        "kid": "dev-idp-1",
        "alg": "RS256",
        "use": "sig",
    }
    codes: dict[str, _Code] = {}
    # Test hooks: let tests make the IdP misbehave to prove Captain rejects it.
    app.state.break_nonce = False
    app.state.audience = client_id
    app.state.issuer_claim = issuer

    @app.get("/.well-known/openid-configuration")
    def discovery() -> dict[str, Any]:
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "jwks_uri": f"{issuer}/jwks",
            "response_types_supported": ["code"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "subject_types_supported": ["public"],
        }

    @app.get("/jwks")
    def jwks() -> dict[str, Any]:
        return {"keys": [jwk]}

    def _redirect_error(redirect_uri: str, state: str, error: str) -> Response:
        return RedirectResponse(
            f"{redirect_uri}?{urlencode({'error': error, 'state': state})}", status_code=303
        )

    @app.get("/authorize", response_class=HTMLResponse)
    def authorize(request: Request) -> Response:
        q = request.query_params
        # Never redirect to an unregistered URI (open-redirect protection).
        if q.get("client_id") != client_id or q.get("redirect_uri") not in redirect_uris:
            return HTMLResponse("Invalid client or redirect_uri", status_code=400)
        if (
            q.get("response_type") != "code"
            or q.get("code_challenge_method") != "S256"
            or not q.get("code_challenge")
        ):
            return _redirect_error(q["redirect_uri"], q.get("state", ""), "invalid_request")
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(q.get(k, ""))}">'
            for k in ("redirect_uri", "state", "nonce", "code_challenge")
        )
        buttons = "".join(
            f'<button name="user" value="{u}" style="{BUTTON_STYLE}">{html.escape(label)}</button>'
            for u, (_, label, _) in DEV_USERS.items()
        )
        return HTMLResponse(
            "<!doctype html><title>Mock identity provider</title>"
            "<body style='font-family:system-ui;max-width:460px;margin:60px auto'>"
            "<h2>Mock identity provider</h2><p style='color:#b00'><b>Development only.</b> "
            "Choose who to sign in as (no password).</p>"
            f"<form method='post' action='/authorize/approve'>{hidden}{buttons}"
            "<button name='user' value='' style='margin-top:14px'>Cancel</button></form></body>"
        )

    @app.post("/authorize/approve")
    async def approve(request: Request) -> Response:
        f = await _form(request)
        user, redirect_uri, state = f.get("user", ""), f.get("redirect_uri", ""), f.get("state", "")
        nonce, code_challenge = f.get("nonce", ""), f.get("code_challenge", "")
        if redirect_uri not in redirect_uris or not code_challenge:
            return HTMLResponse("Invalid redirect_uri", status_code=400)
        if user not in DEV_USERS:
            return _redirect_error(redirect_uri, state, "access_denied")
        code = secrets.token_urlsafe(32)
        codes[code] = _Code(user, client_id, redirect_uri, nonce, code_challenge, time.time() + 60)
        return RedirectResponse(
            f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=303
        )

    @app.post("/token")
    async def token(request: Request) -> Response:
        f = await _form(request)
        grant_type, code, redirect_uri = (
            f.get("grant_type", ""),
            f.get("code", ""),
            f.get("redirect_uri", ""),
        )
        client_id_f, client_secret_f = f.get("client_id", ""), f.get("client_secret", "")
        code_verifier = f.get("code_verifier", "")

        def err(e: str, status: int = 400) -> Response:
            return JSONResponse({"error": e}, status_code=status)

        if client_id_f != client_id or not hmac.compare_digest(client_secret_f, client_secret):
            return err("invalid_client", 401)
        c = codes.pop(code, None)  # single use, even on failure
        if grant_type != "authorization_code" or c is None or c.expires < time.time():
            return err("invalid_grant")
        if c.redirect_uri != redirect_uri:
            return err("invalid_grant")
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if not hmac.compare_digest(digest, c.challenge):
            return err("invalid_grant")  # PKCE verifier does not match
        sub, _, groups = DEV_USERS[c.user]
        now = int(time.time())
        claims = {
            "iss": app.state.issuer_claim,
            "aud": app.state.audience,
            "sub": f"dev-{c.user}",
            "iat": now,
            "exp": now + 300,
            "nonce": "tampered" if app.state.break_nonce else c.nonce,
            "preferred_username": sub,
            "email": f"{c.user}@corp.example",
            "groups": groups,
        }
        id_token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "dev-idp-1"})
        return JSONResponse(
            {
                "id_token": id_token,
                "access_token": secrets.token_urlsafe(16),
                "token_type": "Bearer",
                "expires_in": 300,
            }
        )

    return app


def create_dev_idp_from_env() -> FastAPI:
    from captain.envfile import current_env

    env = current_env()
    secret = env.get("CAPTAIN_DEV_IDP_CLIENT_SECRET")
    if not secret:
        raise SystemExit(
            "CAPTAIN_DEV_IDP_CLIENT_SECRET is not set (run: python -m captain.devsetup)"
        )
    uris = [
        u
        for u in env.get(
            "CAPTAIN_DEV_IDP_REDIRECT_URIS",
            "http://127.0.0.1:8100/admin/v1/auth/callback,http://127.0.0.1:5173/admin/v1/auth/callback",
        ).split(",")
        if u
    ]
    return create_idp_app("http://127.0.0.1:8200", "captain-console", secret, uris)
