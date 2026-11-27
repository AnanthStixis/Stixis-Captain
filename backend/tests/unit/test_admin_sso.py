"""Browser SSO end to end: the real Captain SSO code against the mock OIDC provider
(in process), driving the same redirects a browser would follow."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain.api.admin_auth import AdminSsoConfig, ChainAuthenticator, DevTokenAuthenticator
from captain.api.admin_http import create_admin_app
from captain.api.admin_queries import InMemoryAdminQueries
from captain.api.admin_sso import LoginCookie, SsoClientConfig, SsoService, pkce_challenge
from captain.api.state import InMemoryStateStore
from captain.core.identity.oidc import IssuerConfig
from captain.devidp import create_idp_app
from tests.harness.adapters import CaptainCoreHarness

ISSUER = "http://127.0.0.1:8200"
ADMIN = "http://127.0.0.1:8100"
REDIRECT = f"{ADMIN}/admin/v1/auth/callback"
SECRET = "s" * 40
SESSION_SECRET = b"k" * 32
DEV = "d" * 40
ROLES = {
    "grp-captain-admins": ("admin", "approver", "auditor"),
    "grp-finops": ("finops", "viewer"),
}


class Clock:
    t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Env:
    admin_app: FastAPI
    idp_app: FastAPI
    idp: TestClient
    sso: SsoService
    store: InMemoryStateStore
    clock: Clock

    def browser(self) -> TestClient:
        return TestClient(self.admin_app, base_url=ADMIN, follow_redirects=False)


def make_env(issuer_cfg: str = ISSUER, lifetime: int = 3600) -> Env:
    h = CaptainCoreHarness()
    idp_app = create_idp_app(ISSUER, "captain-console", SECRET, [REDIRECT])
    idp = TestClient(idp_app, base_url=ISSUER, follow_redirects=False)
    clock, store = Clock(), InMemoryStateStore(ttl_s=10**9)
    cfg = SsoClientConfig(
        issuer_cfg,
        "captain-console",
        SECRET,
        REDIRECT,
        group_roles=ROLES,
        session_lifetime_s=lifetime,
    )
    mapping = AdminSsoConfig(IssuerConfig(issuer_cfg, "captain-console", ""), group_roles=ROLES)
    sso = SsoService(cfg, mapping, store, SESSION_SECRET, http=idp, now=clock)
    app = create_admin_app(
        h.built,
        ChainAuthenticator(DevTokenAuthenticator(DEV)),
        InMemoryAdminQueries(h.built.infra),
        sso=sso,
        dev_token_enabled=True,
    )
    return Env(app, idp_app, idp, sso, store, clock)


@pytest.fixture
def env() -> Env:
    return make_env()


def authorize(env: Env, browser: TestClient) -> tuple[str, dict[str, str]]:
    """Browser hits /auth/login and follows the redirect to the IdP; returns the form fields."""
    r = browser.get("/admin/v1/auth/login")
    assert r.status_code == 303 and r.headers["location"].startswith(ISSUER + "/authorize?")
    page = env.idp.get(r.headers["location"])
    assert page.status_code == 200
    fields = {
        k: html.unescape(v)
        for k, v in re.findall(r'name="(\w+)" value="([^"]*)"', page.text)
        if k != "user"
    }
    return r.headers["location"], fields


def finish(env: Env, browser: TestClient, fields: dict[str, str], user: str) -> Any:
    """The user picks an identity at the IdP; the browser follows the redirect back."""
    back = env.idp.post("/authorize/approve", data={**fields, "user": user})
    assert back.status_code == 303
    u = urlparse(back.headers["location"])
    assert f"{u.scheme}://{u.netloc}{u.path}" == REDIRECT
    return browser.get(f"{u.path}?{u.query}")


def sign_in(env: Env, user: str = "priya") -> tuple[TestClient, Any]:
    b = env.browser()
    _, fields = authorize(env, b)
    return b, finish(env, b, fields, user)


def test_full_sign_in_flow_and_cookie_attributes(env: Env) -> None:
    b, r = sign_in(env, "priya")
    assert r.status_code == 303 and r.headers["location"] == "/"  # fixed target
    cookies = r.headers.get_list("set-cookie")
    sess = next(c for c in cookies if c.startswith("captain_session="))
    assert (
        "httponly" in sess.lower() and "samesite=lax" in sess.lower() and "path=/" in sess.lower()
    )
    assert any(
        c.startswith("captain_login=") and ("max-age=0" in c.lower() or "expires=" in c.lower())
        for c in cookies
    )
    me = b.get("/admin/v1/me").json()
    assert me["id"] == "priya@corp.example" and set(me["roles"]) == {"admin", "approver", "auditor"}
    s = b.get("/admin/v1/auth/session").json()
    assert s["csrf_token"] and "keys:write" in s["permissions"]
    assert b.get("/admin/v1/overview").status_code == 200


def test_only_a_hash_of_the_session_id_is_stored(env: Env) -> None:
    b, _ = sign_in(env)
    sid = b.cookies["captain_session"]
    keys = [k for kind, k in env.store._d]  # noqa: SLF001
    assert sid not in keys and all(len(k) == 64 for k in keys)  # sha256 hex only


def test_role_mapping_for_other_users(env: Env) -> None:
    b, _ = sign_in(env, "ravi")
    assert set(b.get("/admin/v1/me").json()["roles"]) == {"finops", "viewer"}
    assert b.get("/admin/v1/budgets").status_code == 200
    assert b.get("/admin/v1/keys").status_code == 403  # finops cannot read keys


def test_user_without_a_role_is_refused(env: Env) -> None:
    b, r = sign_in(env, "asha")
    assert r.headers["location"] == "/?sso_error=no_role"
    assert "captain_session" not in b.cookies and b.get("/admin/v1/me").status_code == 401


def test_user_cancelling_at_idp(env: Env) -> None:
    b, r = sign_in(env, "")  # cancel button
    assert r.headers["location"] == "/?sso_error=denied" and "captain_session" not in b.cookies


# ------------------------------------------------------------------- attacks
def test_wrong_state_is_rejected(env: Env) -> None:
    b = env.browser()
    _, fields = authorize(env, b)
    r = finish(env, b, {**fields, "state": "attacker-chosen-state"}, "priya")
    assert r.headers["location"] == "/?sso_error=state" and "captain_session" not in b.cookies


def test_callback_without_login_cookie_is_rejected(env: Env) -> None:
    victim = env.browser()
    _, fields = authorize(env, victim)  # attacker's login started elsewhere
    stranger = env.browser()  # a browser that never started this login
    r = finish(env, stranger, fields, "priya")
    assert (
        r.headers["location"] == "/?sso_error=state" and "captain_session" not in stranger.cookies
    )


def test_tampered_login_cookie_is_rejected(env: Env) -> None:
    b = env.browser()
    _, fields = authorize(env, b)
    b.cookies.set("captain_login", "tampered", domain="127.0.0.1")
    assert finish(env, b, fields, "priya").headers["location"] == "/?sso_error=state"


def test_authorization_code_replay_fails(env: Env) -> None:
    b = env.browser()
    _, fields = authorize(env, b)
    login_cookie = b.cookies["captain_login"]
    back = env.idp.post("/authorize/approve", data={**fields, "user": "priya"})
    cb = urlparse(back.headers["location"])
    assert b.get(f"{cb.path}?{cb.query}").headers["location"] == "/"  # first use works
    thief = env.browser()
    thief.cookies.set(
        "captain_login", login_cookie, domain="127.0.0.1"
    )  # even with a copied login cookie
    r = thief.get(f"{cb.path}?{cb.query}")
    assert r.headers["location"] == "/?sso_error=failed" and "captain_session" not in thief.cookies


@pytest.mark.parametrize(
    "hook",
    [
        ("break_nonce", True),
        ("audience", "some-other-app"),
        ("issuer_claim", "http://evil.example"),
    ],
    ids=["wrong-nonce", "wrong-audience", "wrong-issuer"],
)
def test_forged_id_tokens_are_rejected(env: Env, hook: tuple[str, Any]) -> None:
    setattr(env.idp_app.state, *hook)
    b, r = sign_in(env, "priya")
    assert r.headers["location"] == "/?sso_error=failed" and "captain_session" not in b.cookies


def test_discovery_issuer_mismatch_refuses_to_start() -> None:
    e = make_env(issuer_cfg="http://127.0.0.1:9999")  # IdP document will say :8200
    r = e.browser().get("/admin/v1/auth/login")
    assert r.status_code == 303 and r.headers["location"] == "/?sso_error=failed"


def test_login_ignores_return_to_and_never_redirects_off_site(env: Env) -> None:
    b = env.browser()
    r = b.get("/admin/v1/auth/login?return_to=https://evil.example/steal")
    assert r.headers["location"].startswith(ISSUER) and "evil" not in r.headers["location"]


def test_idp_refuses_unregistered_redirect_and_wrong_pkce_verifier(env: Env) -> None:
    bad = env.idp.get(
        "/authorize",
        params={
            "client_id": "captain-console",
            "redirect_uri": "https://evil.example/cb",
            "response_type": "code",
            "code_challenge": "x",
            "code_challenge_method": "S256",
        },
    )
    assert bad.status_code == 400
    verifier = "v" * 60
    approve = env.idp.post(
        "/authorize/approve",
        data={
            "user": "priya",
            "redirect_uri": REDIRECT,
            "state": "s",
            "nonce": "n",
            "code_challenge": pkce_challenge(verifier),
        },
    )
    code = parse_qs(urlparse(approve.headers["location"]).query)["code"][0]
    tok = env.idp.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": "captain-console",
            "client_secret": SECRET,
            "code_verifier": "wrong" * 12,
        },
    )
    assert tok.status_code == 400 and tok.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------- CSRF
KILL = {"scope": "model", "target": "claude-sonnet", "reason": "csrf test"}


def test_csrf_protection_for_cookie_sessions(env: Env) -> None:
    b, _ = sign_in(env)
    csrf = b.get("/admin/v1/auth/session").json()["csrf_token"]
    assert (
        b.post("/admin/v1/killswitch", json=KILL).status_code == 403
    )  # cookie alone is not enough
    assert (
        b.post("/admin/v1/killswitch", json=KILL, headers={"X-CSRF-Token": "wrong"}).status_code
        == 403
    )
    evil = {"X-CSRF-Token": csrf, "Origin": "https://evil.example"}
    assert b.post("/admin/v1/killswitch", json=KILL, headers=evil).status_code == 403
    ok = b.post("/admin/v1/killswitch", json=KILL, headers={"X-CSRF-Token": csrf, "Origin": ADMIN})
    assert ok.status_code == 200
    assert b.get("/admin/v1/me").status_code == 200  # a CSRF failure never signs the user out


def test_bearer_tokens_do_not_need_csrf(env: Env) -> None:
    anon = env.browser()
    r = anon.post("/admin/v1/killswitch", json=KILL, headers={"Authorization": f"Bearer {DEV}"})
    assert r.status_code == 200


# ------------------------------------------------------------ sessions & misc
def test_logout_invalidates_the_session_even_if_cookie_is_kept(env: Env) -> None:
    b, _ = sign_in(env)
    sid = b.cookies["captain_session"]
    csrf = b.get("/admin/v1/auth/session").json()["csrf_token"]
    assert b.post("/admin/v1/auth/logout").status_code == 403  # logout needs the CSRF token too
    assert b.post("/admin/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    stolen = env.browser()
    stolen.cookies.set("captain_session", sid, domain="127.0.0.1")
    assert stolen.get("/admin/v1/me").status_code == 401  # the server-side session is gone


def test_sessions_expire_after_the_absolute_lifetime() -> None:
    e = make_env(lifetime=60)
    b, _ = sign_in(e)
    assert b.get("/admin/v1/me").status_code == 200
    e.clock.t += 61
    assert b.get("/admin/v1/me").status_code == 401


def test_two_logins_get_different_sessions(env: Env) -> None:
    a, _ = sign_in(env)
    b, _ = sign_in(env)
    assert a.cookies["captain_session"] != b.cookies["captain_session"]


def test_auth_config_and_security_headers(env: Env) -> None:
    b = env.browser()
    r = b.get("/admin/v1/auth/config")
    assert r.json() == {
        "sso": True,
        "dev_token": True,
        "admin_key": False,
        "stixis_console": False,
        "email_link": False,
    }
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_no_session_means_401(env: Env) -> None:
    b = env.browser()
    assert b.get("/admin/v1/me").status_code == 401
    assert b.get("/admin/v1/auth/session").status_code == 401


# --------------------------------------------------------------------- units
def test_login_cookie_is_authenticated_and_secret_dependent() -> None:
    sealed = LoginCookie(SESSION_SECRET).seal("state", "nonce", "verifier")
    assert "verifier" not in sealed  # encrypted, not just signed
    assert LoginCookie(SESSION_SECRET).open(sealed) == ("state", "nonce", "verifier")
    with pytest.raises(Exception, match="expired or invalid"):
        LoginCookie(b"x" * 32).open(sealed)
    with pytest.raises(ValueError):
        LoginCookie(b"short")


def test_config_rules() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        SsoClientConfig(ISSUER, "c", SECRET, "http://captain.example.com/cb")
    with pytest.raises(ValueError, match="too short"):
        SsoClientConfig(ISSUER, "c", "short", REDIRECT)
    prod = SsoClientConfig(
        "https://idp.example", "c", SECRET, "https://captain.example.com/admin/v1/auth/callback"
    )
    assert prod.secure_cookies and prod.origin == "https://captain.example.com"
    svc = SsoService(
        prod,
        AdminSsoConfig(IssuerConfig("https://idp.example", "c", "")),
        InMemoryStateStore(),
        SESSION_SECRET,
    )
    assert (
        svc.cookie_name == "__Host-captain_session"
    )  # browser-enforced Secure + Path=/ + no Domain
