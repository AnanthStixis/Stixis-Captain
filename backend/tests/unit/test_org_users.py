from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from captain.api.admin_auth import AdminSsoConfig, NoRoleError, actor_from_claims
from captain.api.admin_http import create_admin_app
from captain.api.admin_queries import InMemoryAdminQueries
from captain.api.org import INVITE_TTL_S, InMemoryOrganizationStore, InviteService
from captain.api.state import InMemoryStateStore
from captain.core.admin import AdminActor
from captain.core.decision_api import AuthenticationError
from captain.core.identity.model import KeyStatus
from captain.core.identity.oidc import IssuerConfig
from captain.core.identity.rbac import ROLE_PERMISSIONS, Permission, permissions_for
from captain.core.models import Environment, PrincipalType
from tests.harness.adapters import CaptainCoreHarness

ORG_ADMIN = "o" * 40
VIEWER = "v" * 40


class StaticAuth:
    def __init__(self, tokens: dict[str, AdminActor]) -> None:
        self.tokens = tokens

    def authenticate(self, token: str, client_host: str | None) -> AdminActor:
        if token not in self.tokens:
            raise AuthenticationError("invalid admin token")
        return self.tokens[token]


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class Env:
    def __init__(self) -> None:
        self.h = CaptainCoreHarness()
        self.clock = Clock()
        self.sso_on = False
        self.invites = InviteService(
            InMemoryStateStore(ttl_s=INVITE_TTL_S * 2), lambda: self.sso_on, self.clock
        )
        auth = StaticAuth(
            {
                ORG_ADMIN: AdminActor("boss@corp.example", frozenset({"org-admin"})),
                VIEWER: AdminActor("viewer-1", frozenset({"viewer"})),
            }
        )
        self.app = create_admin_app(
            self.h.built,
            auth,
            InMemoryAdminQueries(self.h.built.infra),
            org_store=InMemoryOrganizationStore(),
            invites=self.invites,
        )
        self.c = TestClient(self.app)

    def events(self, kind: str) -> list[dict[str, object]]:
        self.h.core.m.ledger.write_pending()
        out = []
        for e in self.h.built.infra.ledger_store.entries():
            if e.body.get("event_type") == kind:
                out.append(dict(e.body.get("details") or {}))
        return out


def H(tok: str = ORG_ADMIN) -> dict[str, str]:  # noqa: N802
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def env() -> Env:
    return Env()


# ------------------------------------------------------------------ roles
def test_org_admin_has_every_install_permission_and_admin_is_an_alias() -> None:
    org = ROLE_PERMISSIONS["org-admin"]
    assert {Permission.USERS_WRITE, Permission.AIKEYS_WRITE, Permission.ORG_WRITE} <= org
    assert Permission.CONTENT_READ not in org  # content stays an explicit grant
    assert ROLE_PERMISSIONS["admin"] == org
    assert permissions_for(frozenset({"viewer"})).isdisjoint(
        {Permission.USERS_WRITE, Permission.ORG_WRITE}
    )


# ----------------------------------------------------------- organization
def test_organization_profile_round_trip(env: Env) -> None:
    r = env.c.get("/admin/v1/organization", headers=H(VIEWER))
    assert r.status_code == 200 and r.json()["company_name"] == ""
    assert r.json()["checklist_items"][0] == {
        "key": "sso",
        "label": "Connect your sign-in (SSO)",
        "done": False,
    }
    body = {
        "company_name": "Acme",
        "legal_name": "Acme Holdings Ltd",
        "domains": ["Acme.example", "acme.example"],
        "country": "IN",
        "industry": "Finance",
        "contacts": [{"role": "security", "name": "Sam", "email": "Sam@Acme.example"}],
        "adopters": ["claude-code", "browser"],
        "checklist": {"profile": True, "sso": True},
    }
    assert env.c.put("/admin/v1/organization", json=body, headers=H(VIEWER)).status_code == 403
    r = env.c.put("/admin/v1/organization", json=body, headers=H())
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["domains"] == ["acme.example"]
    assert got["contacts"][0]["email"] == "sam@acme.example"
    assert got["updated_by"] == "boss@corp.example"
    done = {i["key"]: i["done"] for i in got["checklist_items"]}
    assert done == {"sso": False, "profile": True, "users": False, "adopters": False}
    assert env.events("org.changed")


@pytest.mark.parametrize(
    "bad",
    [{"domains": ["not a domain"]}, {"checklist": {"nope": True}}, {"surprise": 1}],
)
def test_organization_rejects_bad_input(env: Env, bad: dict[str, object]) -> None:
    assert env.c.put("/admin/v1/organization", json=bad, headers=H()).status_code in (400, 422)


# ------------------------------------------------------------------ users
def test_add_list_and_update_users(env: Env) -> None:
    new = {"email": "Priya@Corp.example", "name": "Priya", "team": "data", "roles": ["finops"]}
    assert env.c.post("/admin/v1/users", json=new, headers=H(VIEWER)).status_code == 403
    r = env.c.post("/admin/v1/users", json=new, headers=H())
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": "priya@corp.example",
        "email": "priya@corp.example",
        "name": "Priya",
        "team": "data",
        "roles": ["finops"],
        "active": True,
    }
    assert env.c.post("/admin/v1/users", json=new, headers=H()).status_code == 409
    assert env.c.get("/admin/v1/users", headers=H(VIEWER)).status_code == 403
    users = env.c.get("/admin/v1/users", headers=H()).json()
    assert [u["id"] for u in users] == ["priya@corp.example"]  # USER principals only
    r = env.c.put(
        "/admin/v1/users/priya@corp.example", json={"roles": ["auditor"], "team": ""}, headers=H()
    )
    assert r.json()["roles"] == ["auditor"] and r.json()["team"] is None
    assert [e["action"] for e in env.events("user.changed")] == ["created", "updated"]
    assert env.c.put("/admin/v1/users/nobody@x.example", json={}, headers=H()).status_code == 404


def test_unknown_roles_and_bad_email_are_refused(env: Env) -> None:
    r = env.c.post(
        "/admin/v1/users", json={"email": "a@b.example", "name": "A", "roles": ["god"]}, headers=H()
    )
    assert r.status_code == 400
    r = env.c.post("/admin/v1/users", json={"email": "nope", "name": "A"}, headers=H())
    assert r.status_code == 400


def test_nobody_can_grant_more_than_they_hold(env: Env) -> None:
    ROLE_PERMISSIONS["users-only"] = frozenset({Permission.USERS_WRITE, Permission.KEYS_READ})
    try:
        env.c = TestClient(
            create_admin_app(
                env.h.built,
                StaticAuth({VIEWER: AdminActor("u", frozenset({"users-only"}))}),
                InMemoryAdminQueries(env.h.built.infra),
            )
        )
        r = env.c.post(
            "/admin/v1/users",
            json={"email": "x@corp.example", "name": "X", "roles": ["org-admin"]},
            headers=H(VIEWER),
        )
        assert r.status_code == 403 and "cannot grant" in r.text
    finally:
        del ROLE_PERMISSIONS["users-only"]


def test_deactivating_revokes_keys_and_is_recorded(env: Env) -> None:
    env.c.post(
        "/admin/v1/users", json={"email": "dev@corp.example", "name": "Dev"}, headers=H()
    ).raise_for_status()
    ident = env.h.built.core.m.identity
    k1 = ident.issue_key("dev@corp.example", "reg", "p", "a", Environment.DEVELOPMENT, None)
    k2 = ident.issue_key("dev@corp.example", "reg", "p", "a", Environment.DEVELOPMENT, None)
    r = env.c.put("/admin/v1/users/dev@corp.example", json={"active": False}, headers=H())
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["keys_revoked"] == 2
    store = env.h.built.infra.identity_store
    for k in (k1, k2):
        assert store.get_key(k.record.key_id).status is KeyStatus.REVOKED  # type: ignore[union-attr]
    with pytest.raises(ValueError):  # inactive principals get no new keys
        ident.issue_key("dev@corp.example", "reg", "p", "a", Environment.DEVELOPMENT, None)
    ev = env.events("user.changed")[-1]
    assert ev["action"] == "deactivated" and ev["keys_revoked"] == 2
    assert len(env.events("key.revoked")) == 2
    r = env.c.put("/admin/v1/users/dev@corp.example", json={"active": True}, headers=H())
    assert r.json()["active"] is True and env.events("user.changed")[-1]["action"] == "reactivated"


def test_cannot_lock_yourself_out(env: Env) -> None:
    env.c.post(
        "/admin/v1/users",
        json={"email": "boss@corp.example", "name": "Boss", "roles": ["org-admin"]},
        headers=H(),
    ).raise_for_status()
    for body in ({"active": False}, {"roles": ["viewer"]}):
        r = env.c.put("/admin/v1/users/boss@corp.example", json=body, headers=H())
        assert r.status_code == 400


# ------------------------------------------------------ sign-in role merge
def _cfg(lookup: dict[str, tuple[bool, frozenset[str]]]) -> AdminSsoConfig:
    return AdminSsoConfig(
        IssuerConfig("https://idp.example", "c", ""),
        group_roles={"grp-viewers": ("viewer",)},
        user_lookup=lookup.get,
    )


def test_sign_in_roles_are_idp_roles_plus_captain_user_roles() -> None:
    cfg = _cfg({"priya@corp.example": (True, frozenset({"finops"}))})
    a = actor_from_claims(
        cfg, {"sub": "s", "preferred_username": "Priya@Corp.example", "groups": ["grp-viewers"]}
    )
    assert a.roles == {"viewer", "finops"}
    # No IdP group at all: the Captain record alone is enough.
    a = actor_from_claims(cfg, {"sub": "s", "email": "priya@corp.example", "groups": []})
    assert a.roles == {"finops"}
    with pytest.raises(NoRoleError):
        actor_from_claims(cfg, {"sub": "s", "email": "other@corp.example", "groups": []})


def test_deactivated_user_is_refused_even_with_idp_groups() -> None:
    cfg = _cfg({"gone@corp.example": (False, frozenset({"finops"}))})
    with pytest.raises(NoRoleError, match="deactivated"):
        actor_from_claims(
            cfg, {"sub": "s", "email": "gone@corp.example", "groups": ["grp-viewers"]}
        )


# ----------------------------------------------------------------- invites
def _invite(env: Env, email: str = "first@acme.example") -> str:
    r = env.c.post("/admin/v1/invites", json={"email": email}, headers=H())
    assert r.status_code == 200, r.text
    path = r.json()["link_path"]
    assert path.startswith("/accept-invite#token=")
    return str(path.split("=", 1)[1])


def test_invite_is_hashed_single_use_and_opens_an_org_admin_session(env: Env) -> None:
    assert (
        env.c.post(
            "/admin/v1/invites", json={"email": "x@acme.example"}, headers=H(VIEWER)
        ).status_code
        == 403
    )
    token = _invite(env)
    stored = env.invites._store._d  # type: ignore[attr-defined]  # noqa: SLF001
    assert all(token not in k[1] for k in stored)  # only the hash is kept
    browser = TestClient(env.app)
    r = browser.post("/admin/v1/auth/invite/redeem", json={"token": token})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["id"] == "first@acme.example" and s["roles"] == ["org-admin"]
    assert "httponly" in r.headers["set-cookie"].lower()
    me = browser.get("/admin/v1/auth/session")
    assert me.status_code == 200 and me.json()["csrf_token"] == s["csrf_token"]
    # State changes need the CSRF token.
    org = {"company_name": "Acme"}
    assert browser.put("/admin/v1/organization", json=org).status_code == 403
    r = browser.put("/admin/v1/organization", json=org, headers={"X-CSRF-Token": s["csrf_token"]})
    assert r.status_code == 200
    # The invited person is now a Captain user holding org-admin (kept after SSO).
    p = env.h.built.infra.identity_store.get_principal("first@acme.example")
    assert p is not None and p.type == PrincipalType.USER.value and "org-admin" in p.roles
    # Single use.
    r = TestClient(env.app).post("/admin/v1/auth/invite/redeem", json={"token": token})
    assert r.status_code == 409
    assert env.events("invite.created") and env.events("invite.redeemed")
    # Logout ends it.
    browser.post("/admin/v1/auth/logout", headers={"X-CSRF-Token": s["csrf_token"]})
    assert browser.get("/admin/v1/auth/session").status_code == 401


def test_invite_expires_after_seven_days(env: Env) -> None:
    token = _invite(env)
    env.clock.t += INVITE_TTL_S + 1
    r = env.c.post("/admin/v1/auth/invite/redeem", json={"token": token})
    assert r.status_code == 409 and "expired" in r.text


def test_wrong_token_is_refused(env: Env) -> None:
    _invite(env)
    r = env.c.post("/admin/v1/auth/invite/redeem", json={"token": "x" * 43})
    assert r.status_code == 409 and r.json()["error"] == "INVALID"


def test_sso_on_refuses_invites_and_ends_invite_sessions(env: Env) -> None:
    token = _invite(env)
    browser = TestClient(env.app)
    browser.post("/admin/v1/auth/invite/redeem", json={"token": token}).raise_for_status()
    other = _invite(env, "second@acme.example")
    env.sso_on = True
    assert browser.get("/admin/v1/auth/session").status_code == 401
    assert browser.get("/admin/v1/organization").status_code == 401
    r = env.c.post("/admin/v1/auth/invite/redeem", json={"token": other})
    assert r.status_code == 409 and r.json()["error"] == "SSO_ON"
    r = env.c.post("/admin/v1/invites", json={"email": "x@acme.example"}, headers=H())
    assert r.status_code == 409


def test_invite_session_ids_are_not_sso_sessions() -> None:
    """An invite session id must never be accepted as an SSO session (separate kind)."""
    store = InMemoryStateStore(ttl_s=INVITE_TTL_S)
    svc = InviteService(store, lambda: False)
    token, _ = svc.create("a@b.example", "t")
    _, sid, _ = svc.redeem(token)
    from captain.api.admin_sso import SessionManager

    assert SessionManager(store, 3600).get(sid) is None
    assert svc.session(sid) is not None
