"""Check an SSO setup before users try it:  python -m captain.sso_check

Reads CAPTAIN_ADMIN_OIDC and CAPTAIN_SESSION_SECRET (environment or backend/.env), asks the
identity provider for its discovery document and signing keys, and reports PASS / WARN /
FAIL for everything that can be checked without a person signing in. It never prints a
secret. It cannot prove the group claim reaches the token for a given user: after this
passes, sign in once with a test user and read the Activity page's sign-in line.

With --url it also prints an authorization URL (random state, no secret) to open in a
browser, which shows the identity provider's consent and login pages work.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from captain.api.admin_sso import OidcClient, SsoClientConfig, SsoError
from captain.core.identity.oidc import ALLOWED_ALGS, HttpJwks, IssuerConfig, is_secure_url
from captain.core.identity.rbac import ROLE_PERMISSIONS
from captain.envfile import DEFAULT_ENV_FILE, dotenv_values

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True)
class Finding:
    level: str
    check: str
    detail: str


def check_config(env: Mapping[str, str]) -> tuple[dict[str, object] | None, list[Finding]]:
    out: list[Finding] = []
    raw = env.get("CAPTAIN_ADMIN_OIDC", "").strip()
    if not raw:
        return None, [Finding(FAIL, "configuration", "CAPTAIN_ADMIN_OIDC is not set")]
    try:
        c = json.loads(raw)
        if not isinstance(c, dict):
            raise ValueError("not an object")
    except ValueError as e:
        return None, [Finding(FAIL, "configuration", f"CAPTAIN_ADMIN_OIDC is not valid JSON: {e}")]
    missing = [k for k in ("issuer", "client_id", "client_secret", "redirect_uri") if not c.get(k)]
    if missing:
        return None, [Finding(FAIL, "configuration", f"missing: {', '.join(missing)}")]
    out.append(Finding(PASS, "configuration", "required settings present"))

    redirect = str(c["redirect_uri"])
    if redirect.startswith("https://"):
        out.append(Finding(PASS, "redirect URI", "https"))
    elif is_secure_url(redirect):
        out.append(Finding(WARN, "redirect URI", "plain http to this machine: development only"))
    else:
        out.append(Finding(FAIL, "redirect URI", "must be https (http only for localhost)"))
    if not redirect.endswith("/admin/v1/auth/callback"):
        out.append(
            Finding(
                WARN,
                "redirect URI",
                "does not end with /admin/v1/auth/callback (the path Captain serves)",
            )  # noqa: E501
        )
    if len(str(c["client_secret"])) < 16:
        out.append(Finding(FAIL, "client secret", "looks too short"))
    try:
        sec = bytes.fromhex(env.get("CAPTAIN_SESSION_SECRET", ""))
        out.append(
            Finding(PASS, "session secret", "ok")
            if len(sec) >= 32
            else Finding(
                FAIL, "session secret", "CAPTAIN_SESSION_SECRET must be at least 32 bytes of hex"
            )  # noqa: E501
        )
    except ValueError:
        out.append(Finding(FAIL, "session secret", "CAPTAIN_SESSION_SECRET must be hex"))

    gr = c.get("group_roles")
    if not isinstance(gr, dict) or not gr:
        out.append(Finding(FAIL, "group roles", "group_roles is empty: nobody could sign in"))
    else:
        known = set(ROLE_PERMISSIONS)
        wanted = {r for roles in gr.values() for r in (roles if isinstance(roles, list) else [])}
        unknown = sorted(wanted - known)
        if unknown:
            out.append(
                Finding(FAIL, "group roles", f"unknown Captain role(s): {', '.join(unknown)}")
            )
        else:
            out.append(Finding(PASS, "group roles", f"{len(gr)} group(s) mapped"))
        if "admin" not in wanted:
            out.append(Finding(WARN, "group roles", "no group maps to the admin role"))
    return c, out


def _client_config(c: Mapping[str, object]) -> SsoClientConfig:
    return SsoClientConfig(
        issuer=str(c["issuer"]),
        client_id=str(c["client_id"]),
        client_secret=str(c["client_secret"]),
        redirect_uri=str(c["redirect_uri"]),
        scopes=str(c.get("scopes", "openid profile email")),
        roles_claim=str(c.get("roles_claim", "groups")),
        name_claim=str(c.get("name_claim", "preferred_username")),
    )


def check_provider(c: Mapping[str, object], http: httpx.Client | None = None) -> list[Finding]:
    out: list[Finding] = []
    try:
        cfg = _client_config(c)
    except ValueError as e:
        return [Finding(FAIL, "configuration", f"provider checks skipped: {e}")]
    client = OidcClient(cfg, http or httpx.Client(timeout=8.0))
    try:
        meta = client.metadata()
    except SsoError as e:
        return [Finding(FAIL, "discovery", str(e))]
    out.append(Finding(PASS, "discovery", "issuer matches and endpoints are https"))

    pk = meta.get("code_challenge_methods_supported")
    if pk is None:
        out.append(
            Finding(WARN, "PKCE", "provider does not advertise PKCE methods; S256 is always sent")
        )  # noqa: E501
    elif "S256" in pk:
        out.append(Finding(PASS, "PKCE", "S256 supported"))
    else:
        out.append(Finding(FAIL, "PKCE", f"S256 not offered (got {pk})"))

    try:
        out.append(Finding(PASS, "token endpoint auth", f"will use {client.token_auth()}"))
    except SsoError as e:
        out.append(Finding(FAIL, "token endpoint auth", str(e)))

    algs = meta.get("id_token_signing_alg_values_supported")
    if isinstance(algs, list):
        usable = [a for a in algs if a in ALLOWED_ALGS]
        out.append(
            Finding(PASS, "signing algorithms", f"usable: {', '.join(usable)}")
            if usable
            else Finding(FAIL, "signing algorithms", f"none of {algs} is accepted by Captain")
        )

    scopes = meta.get("scopes_supported")
    if isinstance(scopes, list):
        absent = [s for s in cfg.scopes.split() if s not in scopes]
        if absent:
            out.append(
                Finding(
                    WARN, "scopes", f"requested but not listed by the provider: {', '.join(absent)}"
                )
            )  # noqa: E501
        else:
            out.append(Finding(PASS, "scopes", "all requested scopes are offered"))
    if (
        cfg.roles_claim == "groups"
        and isinstance(scopes, list)
        and "groups" in scopes
        and "groups" not in cfg.scopes.split()
    ):
        out.append(
            Finding(
                WARN,
                "groups",
                "provider offers a 'groups' scope; add it to scopes (Okta needs it for groups)",
            )
        )
    claims = meta.get("claims_supported")
    if isinstance(claims, list) and cfg.roles_claim not in claims:
        out.append(
            Finding(
                WARN,
                "claims",
                f"'{cfg.roles_claim}' is not in claims_supported (many providers still send it)",
            )
        )  # noqa: E501

    try:
        icfg = IssuerConfig(cfg.issuer, cfg.client_id, str(meta["jwks_uri"]))
        jw = HttpJwks({cfg.issuer: icfg}, client=client._http)  # noqa: SLF001
        keys = jw._fetch(cfg.issuer)  # noqa: SLF001
        out.append(Finding(PASS, "signing keys", f"{len(keys)} key(s) fetched"))
    except Exception as e:  # network or format problems all mean "cannot verify tokens"
        out.append(Finding(FAIL, "signing keys", f"could not load the JWKS: {type(e).__name__}"))
    return out


def main(argv: list[str]) -> int:
    env = {**dotenv_values(DEFAULT_ENV_FILE), **os.environ}
    c, findings = check_config(env)
    if c is not None:
        findings += check_provider(c)
    for f in findings:
        print(f"{f.level:<5} {f.check:<20} {f.detail}")
    fails = sum(f.level == FAIL for f in findings)
    warns = sum(f.level == WARN for f in findings)
    print(f"\n{fails} failure(s), {warns} warning(s)")
    if c is not None and "--url" in argv and not fails:
        url = OidcClient(_client_config(c)).authorization_url(
            secrets.token_urlsafe(16), secrets.token_urlsafe(16), secrets.token_urlsafe(48)
        )
        print(
            "\nOpen in a browser to test the provider's login page (the sign-in will end at "
            "Captain's callback with a state error, which is expected here):\n" + url
        )  # noqa: E501
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
