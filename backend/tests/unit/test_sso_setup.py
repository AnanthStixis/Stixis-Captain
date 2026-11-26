"""SSO set up by the org admin on the Setup page: save -> test sign-in -> turn on.
Real Captain SSO code against the in-process mock OIDC provider."""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from captain.api.admin_auth import ChainAuthenticator, DevTokenAuthenticator
from captain.api.admin_http import create_admin_app
from captain.api.admin_main import _user_lookup
from captain.api.admin_queries import InMemoryAdminQueries
from captain.api.org import INVITE_TTL_S, InMemoryOrganizationStore, InviteService
from captain.api.sso_live import SSO_SECRET_ID, LiveSso
from captain.api.state import InMemoryStateStore
from captain.core.secrets_store import InMemorySecretStore, LocalKeyWrapper, SecretsService
from captain.devidp import create_idp_app
from tests.harness.adapters import CaptainCoreHarness

ISSUER = "http://127.0.0.1:8200"
ADMIN = "http://127.0.0.1:8100"
SECRET = "s" * 40
DEV = "d" * 40
SETTINGS = {"provider": "entra", "issuer": ISSUER, "client_id": "captain-console",
            "client_secret": SECRET, "group_roles": {"grp-finops": ["finops"]}}  # fmt: skip


class World:
    def __init__(self) -> None:
        self.h = CaptainCoreHarness()
        self.idp = TestClient(
            create_idp_app(ISSUER, "captain-console", SECRET, [f"{ADMIN}/admin/v1/auth/callback"]),
            base_url=ISSUER, follow_redirects=False,
        )  # fmt: skip
        self.secrets = SecretsService(InMemorySecretStore(), LocalKeyWrapper(bytes(range(32))))
        self.state = InMemoryStateStore(ttl_s=10**9)
        self.live = self.make_live()
        self.invites = InviteService(
            InMemoryStateStore(ttl_s=INVITE_TTL_S), lambda: self.live.get() is not None
        )
        self.app = create_admin_app(
            self.h.built, ChainAuthenticator(DevTokenAuthenticator(DEV), self.live.bearer()),
            InMemoryAdminQueries(self.h.built.infra), dev_token_enabled=True,
            org_store=InMemoryOrganizationStore(), invites=self.invites, live_sso=self.live,
        )  # fmt: skip

    def make_live(self) -> LiveSso:
        return LiveSso(self.state, _user_lookup(self.h.built), secrets=self.secrets,
                       console_url=ADMIN, http=self.idp)  # fmt: skip

    def browser(self) -> TestClient:
        return TestClient(self.app, base_url=ADMIN, follow_redirects=False)

    def org_admin(self, email: str = "priya@corp.example") -> tuple[TestClient, dict[str, str]]:
        """Signed in with the bootstrap invite, as before SSO."""
        token, _ = self.invites.create(email, "stixis-platform")
        b = self.browser()
        r = b.post("/admin/v1/auth/invite/redeem", json={"token": token})
        assert r.status_code == 200, r.text
        return b, {"X-CSRF-Token": r.json()["csrf_token"], "Origin": ADMIN}

    def round_trip(self, b: TestClient, user: str, test: bool) -> Any:
        r = b.get("/admin/v1/auth/login" + ("?test=1" if test else ""))
        assert r.status_code == 303 and r.headers["location"].startswith(ISSUER), r.headers
        page = self.idp.get(r.headers["location"])
        found = re.findall(r'name="(\w+)" value="([^"]*)"', page.text)
        fields = {k: html.unescape(v) for k, v in found if k != "user"}
        back = self.idp.post("/authorize/approve", data={**fields, "user": user})
        u = urlparse(back.headers["location"])
        return b.get(f"{u.path}?{u.query}")


def test_org_admin_sets_up_tests_and_turns_on_sso() -> None:
    w = World()
    b, csrf = w.org_admin()
    assert b.get("/admin/v1/auth/config").json()["sso"] is False
    view = b.get("/admin/v1/sso/settings").json()
    assert view["can_edit"] and view["redirect_uri"] == f"{ADMIN}/admin/v1/auth/callback"

    saved = b.put("/admin/v1/sso/settings", json=SETTINGS, headers=csrf)
    assert saved.status_code == 200, saved.text
    assert saved.json()["draft"]["secret_last4"] == "ssss" and SECRET not in saved.text
    assert b.post("/admin/v1/sso/check", headers=csrf).json()["ok"] is True
    # Not tested yet -> cannot be turned on.
    assert b.post("/admin/v1/sso/turn-on", headers=csrf).status_code == 400

    # A test as someone who would not be an org admin does not count.
    r = w.round_trip(b, "ravi", test=True)
    assert r.headers["location"] == "/setup?sso_test=not_admin"
    # The org admin's own test: no SSO session is opened, the invite session goes on.
    r = w.round_trip(b, "priya", test=True)
    assert r.headers["location"] == "/setup?sso_test=ok"
    assert not any(c.startswith("captain_session=") for c in r.headers.get_list("set-cookie"))
    assert b.get("/admin/v1/me").status_code == 200
    assert b.get("/admin/v1/sso/settings").json()["draft"]["tested_by"] == "priya@corp.example"

    on = b.post("/admin/v1/sso/turn-on", headers=csrf)
    assert on.status_code == 200 and on.json()["on"] is True
    # SSO is on: the invite session ends, invites are refused, SSO sign-in works.
    assert b.get("/admin/v1/me").status_code == 401
    assert b.get("/admin/v1/auth/config").json()["sso"] is True
    s = w.browser()
    r = w.round_trip(s, "priya", test=False)
    assert r.headers["location"] == "/"
    assert "org-admin" in s.get("/admin/v1/me").json()["roles"]
    s2 = w.browser()
    w.round_trip(s2, "ravi", test=False)
    assert s2.get("/admin/v1/me").json()["roles"] == ["finops"]  # from the group mapping

    # Stored encrypted as one blob, and it survives a restart.
    rows = [x for x in w.secrets.store.all() if x.provider_id == SSO_SECRET_ID]
    assert len(rows) == 1 and SECRET.encode() not in rows[0].ciphertext
    assert w.make_live().get() is not None


def test_only_org_admins_and_only_with_valid_settings() -> None:
    w = World()
    viewer = w.browser()
    assert viewer.get("/admin/v1/sso/settings").status_code == 401
    r = viewer.get("/admin/v1/auth/login?test=1")
    assert r.headers["location"] == "/setup?sso_test=forbidden"
    b, csrf = w.org_admin()
    assert b.get("/admin/v1/auth/login?test=1").headers["location"] == "/setup?sso_test=no_settings"
    bad = b.put("/admin/v1/sso/settings", json={**SETTINGS, "issuer": "http://evil.example"},
                headers=csrf)  # fmt: skip
    assert bad.status_code == 422
    short = b.put("/admin/v1/sso/settings", json={**SETTINGS, "client_secret": "abc"}, headers=csrf)
    assert short.status_code == 400 and "too short" in short.text
    bad_roles = {**SETTINGS, "group_roles": {"g": ["platform-admin"]}}
    role = b.put("/admin/v1/sso/settings", json=bad_roles, headers=csrf)
    assert role.status_code == 422
    # Saving again without the secret keeps the stored one.
    assert b.put("/admin/v1/sso/settings", json=SETTINGS, headers=csrf).status_code == 200
    again = b.put("/admin/v1/sso/settings", json={**SETTINGS, "client_secret": ""}, headers=csrf)
    assert again.status_code == 200 and again.json()["draft"]["secret_last4"] == "ssss"


def test_turning_sso_off_brings_back_invites() -> None:
    w = World()
    b, csrf = w.org_admin()
    b.put("/admin/v1/sso/settings", json=SETTINGS, headers=csrf)
    w.round_trip(b, "priya", test=True)
    b.post("/admin/v1/sso/turn-on", headers=csrf)
    s = w.browser()
    w.round_trip(s, "priya", test=False)
    csrf2 = {"X-CSRF-Token": s.get("/admin/v1/auth/session").json()["csrf_token"],
             "Origin": ADMIN}  # fmt: skip
    assert s.post("/admin/v1/sso/turn-off", headers=csrf2).json()["on"] is False
    assert s.get("/admin/v1/auth/config").json()["sso"] is False
    w.org_admin()  # invites work again


def test_settings_from_the_install_are_read_only() -> None:
    w = World()
    fixed = w.make_live()
    fixed.save_draft(SETTINGS, "x")
    fixed.mark_tested("x")
    fixed.turn_on("x")
    locked = LiveSso(w.state, None, fixed=fixed.get())
    assert locked.view()["managed_by_install"] is True and locked.view()["can_edit"] is False
