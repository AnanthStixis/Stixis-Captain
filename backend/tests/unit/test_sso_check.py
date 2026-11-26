"""The SSO preflight check: it must catch the mistakes that otherwise surface as a broken
login for every user, and must never print a secret."""

from __future__ import annotations

import json
from typing import Any

import httpx

from captain.sso_check import FAIL, PASS, WARN, Finding, check_config, check_provider
from tests.unit.test_sso_realworld import CLIENT, ISSUER, REDIRECT, SECRET, FakeIdp

GOOD = {
    "issuer": ISSUER,
    "client_id": CLIENT,
    "client_secret": SECRET,
    "redirect_uri": REDIRECT,
    "scopes": "openid profile email",
    "group_roles": {"grp-admins": ["admin", "approver"], "grp-viewers": ["viewer"]},
}
ENV = {"CAPTAIN_ADMIN_OIDC": json.dumps(GOOD), "CAPTAIN_SESSION_SECRET": "ab" * 32}


def levels(fs: list[Finding]) -> dict[str, str]:
    """Worst level per check (a check can report more than once)."""
    rank = {PASS: 0, WARN: 1, FAIL: 2}
    out: dict[str, str] = {}
    for f in fs:
        if f.check not in out or rank[f.level] > rank[out[f.check]]:
            out[f.check] = f.level
    return out


def with_config(**over: Any) -> tuple[dict[str, object] | None, list[Finding]]:
    cfg = {**GOOD, **over}
    return check_config({**ENV, "CAPTAIN_ADMIN_OIDC": json.dumps(cfg)})


def test_a_good_configuration_has_no_failures() -> None:
    c, fs = check_config(ENV)
    assert c is not None and all(f.level == PASS for f in fs)


def test_configuration_mistakes_are_failures() -> None:
    assert levels(check_config({})[1])["configuration"] == FAIL
    assert levels(check_config({"CAPTAIN_ADMIN_OIDC": "{nope"})[1])["configuration"] == FAIL
    assert (
        levels(check_config({"CAPTAIN_ADMIN_OIDC": json.dumps({"issuer": "x"})})[1])[
            "configuration"
        ]
        == FAIL
    )
    assert (
        levels(with_config(redirect_uri="http://captain.example.com/cb")[1])["redirect URI"] == FAIL
    )
    assert levels(with_config(client_secret="short")[1])["client secret"] == FAIL
    assert levels(with_config(group_roles={})[1])["group roles"] == FAIL
    typo = with_config(group_roles={"g": ["adminn"]})[1]
    assert levels(typo)["group roles"] == FAIL and "adminn" in str(typo)  # a role typo is named
    assert (
        levels(check_config({**ENV, "CAPTAIN_SESSION_SECRET": "zz"})[1])["session secret"] == FAIL
    )
    assert (
        levels(check_config({**ENV, "CAPTAIN_SESSION_SECRET": "ab"})[1])["session secret"] == FAIL
    )


def test_warnings_for_development_and_odd_paths() -> None:
    dev = levels(with_config(redirect_uri="http://127.0.0.1:8100/admin/v1/auth/callback")[1])
    assert dev["redirect URI"] == WARN
    odd = with_config(redirect_uri="https://captain.example.com/callback")[1]
    assert any(f.level == WARN and "callback" in f.detail for f in odd)
    no_admin = with_config(group_roles={"g": ["viewer"]})[1]
    assert any(f.level == WARN and "admin" in f.detail for f in no_admin)


def test_no_finding_ever_contains_the_secret() -> None:
    c, fs = check_config(ENV)
    assert c is not None
    idp = FakeIdp()
    fs += check_provider(c, httpx.Client(transport=httpx.MockTransport(idp.handler)))
    assert SECRET not in json.dumps([f.__dict__ for f in fs])


# ------------------------------------------------------------------- provider


def run(idp: FakeIdp) -> dict[str, str]:
    c, _ = check_config(ENV)
    assert c is not None
    return levels(check_provider(c, httpx.Client(transport=httpx.MockTransport(idp.handler))))


def test_a_healthy_provider_passes() -> None:
    idp = FakeIdp(["client_secret_post"])
    res = run(idp)
    assert res["discovery"] == PASS and res["token endpoint auth"] == PASS
    assert res["signing keys"] == PASS and FAIL not in res.values()


def test_provider_problems_are_failures() -> None:
    class Bad(FakeIdp):
        def meta(self) -> dict[str, Any]:
            m = super().meta()
            m.update(self.override)
            return m

        override: dict[str, Any] = {}

    wrong_issuer = Bad()
    wrong_issuer.override = {"issuer": "https://evil.example.com/"}
    assert run(wrong_issuer)["discovery"] == FAIL

    no_s256 = Bad()
    no_s256.override = {"code_challenge_methods_supported": ["plain"]}
    assert run(no_s256)["PKCE"] == FAIL

    weak = Bad()
    weak.override = {"id_token_signing_alg_values_supported": ["HS256", "none"]}
    assert run(weak)["signing algorithms"] == FAIL

    odd_auth = Bad()
    odd_auth.override = {"token_endpoint_auth_methods_supported": ["private_key_jwt"]}
    assert run(odd_auth)["token endpoint auth"] == FAIL

    insecure = Bad()
    insecure.override = {"token_endpoint": "http://login.example.com/token"}
    assert run(insecure)["discovery"] == FAIL


def test_an_unreachable_or_broken_key_set_is_a_failure() -> None:
    class NoKeys(FakeIdp):
        def handler(self, req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/keys"):
                return httpx.Response(500)
            return super().handler(req)

    assert run(NoKeys())["signing keys"] == FAIL

    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c, _ = check_config(ENV)
    assert c is not None
    res = levels(check_provider(c, httpx.Client(transport=httpx.MockTransport(down))))
    assert res == {"discovery": FAIL}


def test_scope_and_group_hints() -> None:
    class Scopes(FakeIdp):
        def meta(self) -> dict[str, Any]:
            return {**super().meta(), "scopes_supported": ["openid", "email", "groups"]}

    res = run(Scopes())
    assert res["scopes"] == WARN  # 'profile' requested but not offered
    assert res["groups"] == WARN  # the provider offers 'groups' and we do not ask for it
    assert "groups" not in run(FakeIdp())  # no noise for providers that do not offer it


def test_unusable_settings_skip_the_provider_checks_instead_of_crashing() -> None:
    res = check_provider({**GOOD, "client_secret": "short"})
    assert [f.level for f in res] == [FAIL] and "skipped" in res[0].detail
