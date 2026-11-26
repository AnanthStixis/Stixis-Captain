"""Stixis control plane and the platform endpoints on a client install (plan step 5).
Two separate Captains in one test: Stixis's (companies) and a client's (bootstrap/summary)."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from captain.api.admin_platform import install_platform_routes
from captain.api.admin_queries import InMemoryAdminQueries
from captain.api.controlplane import InMemoryCompanyStore, install_controlplane_routes
from captain.api.org import InMemoryOrganizationStore, InviteService, Organization
from captain.api.state import InMemoryStateStore
from captain.core.admin import AdminActor
from captain.core.identity.rbac import ROLE_PERMISSIONS, Permission, PermissionDenied
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.secrets_store import InMemorySecretStore, LocalKeyWrapper, SecretsService
from tests.harness.adapters import CaptainCoreHarness

INSTALL = "http://127.0.0.1:9100"
NEW = {
    "code": "abc",
    "details": {"company_name": "ABC Pvt Ltd", "legal_name": "ABC Private Limited",
                "domains": ["abc.example"], "country": "IN", "industry": "Finance"},
    "region": "centralindia",
    "org_admin_email": "Admin@ABC.example",
    "org_admin_name": "Asha Rao",
}  # fmt: skip


def denied_handler(app: FastAPI) -> None:
    @app.exception_handler(PermissionDenied)
    async def _denied(_: Any, e: PermissionDenied) -> JSONResponse:
        return JSONResponse({"error": "FORBIDDEN"}, status_code=403)


class Client:
    """A client company's install: platform endpoints only, with its own Core."""

    def __init__(self, token: str | None) -> None:
        self.h = CaptainCoreHarness()
        self.org = InMemoryOrganizationStore()
        self.invites = InviteService(InMemoryStateStore(ttl_s=3600), sso_enabled=lambda: False)
        self.app = FastAPI()
        env = {"CAPTAIN_PLATFORM_TOKEN": token} if token else {}
        install_platform_routes(self.app, self.h.built, InMemoryAdminQueries(self.h.built.infra),
                                self.org, self.invites, env)  # fmt: skip
        self.http = TestClient(self.app)


def stixis(
    install: Client | None, roles: frozenset[str] = frozenset({"platform-admin"})
) -> tuple[CaptainCoreHarness, TestClient]:  # noqa: E501
    h = CaptainCoreHarness()
    app = FastAPI()
    denied_handler(app)
    secrets = SecretsService(InMemorySecretStore(), LocalKeyWrapper(bytes(range(32))))
    install_controlplane_routes(app, h.built, lambda: AdminActor("ops@stixis.example", roles),
                                InMemoryCompanyStore(), secrets,
                                install.http if install else None)  # fmt: skip
    return h, TestClient(app)


def token_from_files(c: TestClient, code: str = "abc") -> tuple[str, dict[str, str]]:
    r = c.get(f"/admin/v1/companies/{code}/install-files")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    files = {n: z.read(n).decode() for n in z.namelist()}
    return files["platform-token.txt"].strip(), files


def test_install_files_carry_the_code_region_and_a_platform_token() -> None:
    _, cp = stixis(None)
    assert cp.post("/admin/v1/companies", json=NEW).status_code == 200
    token, files = token_from_files(cp)
    assert 'client          = "abc"' in files["client.tfvars"]
    assert 'location        = "centralindia"' in files["client.tfvars"]
    assert 'key                  = "abc-pilot.tfstate"' in files["client.backend.hcl"]
    assert "CAPTAIN_PLATFORM_TOKEN: captain-platform-token" in files["helm-values-extra.yaml"]
    assert "DELETE platform-token.txt" in files["README.txt"]
    assert len(token) >= 40
    again, _ = token_from_files(cp)
    assert again == token  # the same token every time: it is stored, not regenerated


def test_full_flow() -> None:
    """Create -> install files -> install address -> bootstrap -> invite works -> health."""
    holder: dict[str, Client] = {}

    class Late:  # the install exists only after its token is known
        def request(self, *a: Any, **kw: Any) -> Any:
            return holder["c"].http.request(*a, **kw)

    h = CaptainCoreHarness()
    app = FastAPI()
    denied_handler(app)
    secrets = SecretsService(InMemorySecretStore(), LocalKeyWrapper(bytes(range(32))))
    install_controlplane_routes(app, h.built, lambda: AdminActor("ops@stixis.example",
                                frozenset({"platform-admin"})), InMemoryCompanyStore(), secrets,
                                Late())  # type: ignore[arg-type]  # fmt: skip
    cp = TestClient(app)
    created = cp.post("/admin/v1/companies", json=NEW).json()
    assert created["status"] == "onboarding" and created["org_admin_email"] == "admin@abc.example"
    token, _ = token_from_files(cp)
    holder["c"] = install = Client(token)
    install.org.put(Organization(adopters=["browser"]), "asha")  # the company's own choice

    assert cp.post("/admin/v1/companies/abc/bootstrap").status_code == 409  # no address yet
    assert (
        cp.put("/admin/v1/companies/abc/install", json={"admin_url": INSTALL + "/"}).status_code
        == 200
    )
    boot = cp.post("/admin/v1/companies/abc/bootstrap")
    assert boot.status_code == 200, boot.text
    url = boot.json()["invite_url"]
    assert url.startswith(INSTALL + "/accept-invite#token=")

    org, _, _ = install.org.get()
    assert org.company_name == "ABC Pvt Ltd" and org.adopters == ["browser"]  # kept
    admin = install.h.built.infra.identity_store.get_principal("admin@abc.example")
    assert admin is not None and "org-admin" in admin.roles and admin.display_name == "Asha Rao"
    actor, _, _ = install.invites.redeem(url.split("token=", 1)[1])
    assert actor.id == "admin@abc.example"

    view = cp.post("/admin/v1/companies/abc/refresh").json()
    assert view["last_error"] is None
    s = view["last_summary"]
    assert s["company_name"] == "ABC Pvt Ltd" and s["org_admins"] == 1 and s["users"] == 1
    allowed = {"company_name", "adopters", "checklist_done", "users", "org_admins",
               "active_keys", "calls_last_7_days", "profile_updated_at", "checked_at"}  # fmt: skip
    assert set(s) <= allowed  # counts and settings only, never evidence

    # The token never appears in the company list, the Stixis ledger or the client ledger.
    listed = cp.get("/admin/v1/companies").text
    assert token not in listed and "token_secret_id" not in listed
    for harness in (h, install.h):
        store = harness.built.infra.ledger_store
        assert isinstance(store, InMemoryLedgerStore)
        text = json.dumps([b for (_p, _t, b) in store.outbox.values()])
        assert token not in text
    stixis_text = json.dumps([b for (_p, _t, b) in h.built.infra.ledger_store.outbox.values()])  # type: ignore[attr-defined]
    assert "company.changed" in stixis_text


def test_wrong_or_missing_platform_token() -> None:
    install = Client("the-real-token-0123456789")
    h, cp = stixis(install)
    cp.post("/admin/v1/companies", json=NEW)
    cp.put("/admin/v1/companies/abc/install", json={"admin_url": INSTALL})
    view = cp.post("/admin/v1/companies/abc/refresh").json()
    assert "did not accept the platform token" in view["last_error"]
    assert cp.post("/admin/v1/companies/abc/bootstrap").status_code == 502
    # Directly: no token configured -> the endpoints do not exist; wrong token -> 401.
    assert Client(None).http.get("/admin/v1/platform/summary").status_code == 404
    bad = install.http.get("/admin/v1/platform/summary", headers={"X-Captain-Platform-Token": "x"})
    assert bad.status_code == 401


def test_only_platform_admins_manage_companies() -> None:
    assert Permission.COMPANIES_WRITE not in ROLE_PERMISSIONS["org-admin"]
    assert Permission.COMPANIES_WRITE not in ROLE_PERMISSIONS["admin"]
    _, cp = stixis(None, frozenset({"org-admin"}))
    assert cp.get("/admin/v1/companies").status_code == 403
    assert cp.post("/admin/v1/companies", json=NEW).status_code == 403


@pytest.mark.parametrize("url", ["http://evil.example", "ftp://x", "javascript:alert(1)"])
def test_install_address_must_be_https(url: str) -> None:
    _, cp = stixis(None)
    cp.post("/admin/v1/companies", json=NEW)
    assert cp.put("/admin/v1/companies/abc/install", json={"admin_url": url}).status_code == 400


def test_codes_are_unique_and_shaped_like_opentofu_client_codes() -> None:
    _, cp = stixis(None)
    assert cp.post("/admin/v1/companies", json=NEW).status_code == 200
    assert cp.post("/admin/v1/companies", json=NEW).status_code == 409
    assert cp.post("/admin/v1/companies", json={**NEW, "code": "ABC Ltd"}).status_code == 422


def test_an_email_in_the_domain_field_is_refused_when_the_company_is_added() -> None:
    """Regression (live, 2026-11-26): 'admin@ibm.com' was accepted as a domain, and the
    install refused the bootstrap later with a bare 422."""
    _, cp = stixis(None)
    bad = {**NEW, "details": {**NEW["details"], "domains": ["admin@ibm.com"]}}
    r = cp.post("/admin/v1/companies", json=bad)
    assert r.status_code == 422 and "not a domain name" in r.text
    ok = {**NEW, "details": {**NEW["details"], "domains": ["IBM.com"]}}
    assert cp.post("/admin/v1/companies", json=ok).json()["details"]["domains"] == ["ibm.com"]


def test_an_install_refusal_shows_the_installs_reason() -> None:
    holder: dict[str, Client] = {}

    class Late:
        def request(self, *a: Any, **kw: Any) -> Any:
            return holder["c"].http.request(*a, **kw)

    h = CaptainCoreHarness()
    app = FastAPI()
    denied_handler(app)
    secrets = SecretsService(InMemorySecretStore(), LocalKeyWrapper(bytes(range(32))))
    store = InMemoryCompanyStore()
    ops = AdminActor("ops", frozenset({"platform-admin"}))
    install_controlplane_routes(app, h.built, lambda: ops, store, secrets, Late())  # type: ignore[arg-type]
    cp = TestClient(app)
    cp.post("/admin/v1/companies", json=NEW)
    token, _ = token_from_files(cp)
    holder["c"] = Client(token)
    cp.put("/admin/v1/companies/abc/install", json={"admin_url": INSTALL})
    doc = store.get("abc")
    assert doc is not None
    doc["org_admin_email"] = "not-an-email"  # an old record saved before validation
    store.put(doc)
    r = cp.post("/admin/v1/companies/abc/bootstrap")
    assert r.status_code == 502 and "The install answered 400: a valid email address" in r.text


def test_platform_admin_holds_only_what_the_stixis_console_needs() -> None:
    """Companies, Stixis staff (users) and the audit trail; nothing that governs a
    company's own AI use."""
    assert ROLE_PERMISSIONS["platform-admin"] == {
        Permission.COMPANIES_WRITE, Permission.USERS_WRITE, Permission.KEYS_READ,
        Permission.AUDIT_READ,
    }  # fmt: skip
    for p in (Permission.BUDGET_WRITE, Permission.AIKEYS_WRITE, Permission.REGISTER_WRITE,
              Permission.KILL_SWITCH, Permission.USAGE_READ, Permission.CONTENT_READ):  # fmt: skip
        assert p not in ROLE_PERMISSIONS["platform-admin"]


def test_dashboard_counts_companies_and_flags_what_needs_attention() -> None:
    install = Client("tok-0123456789abcdef0123456789")
    _, cp = stixis(install)
    assert cp.get("/admin/v1/companies/dashboard").json()["companies"] == 0
    cp.post("/admin/v1/companies", json=NEW)
    cp.post("/admin/v1/companies", json={**NEW, "code": "xyz"})
    cp.put("/admin/v1/companies/abc/install", json={"admin_url": INSTALL})
    d = cp.get("/admin/v1/companies/dashboard").json()
    assert d["companies"] == 2 and d["status"]["onboarding"] == 2
    assert d["pipeline"] == {"added": 2, "address_set": 1, "bootstrapped": 0, "healthy": 0}
    assert d["health"]["not_installed"] == 1 and d["health"]["not_checked"] == 1
    reasons = {a["code"]: a["reason"] for a in d["needs_attention"]}
    assert "not connected" in reasons["xyz"] and "Check now" in reasons["abc"]
    _, other = stixis(None, frozenset({"org-admin"}))
    assert other.get("/admin/v1/companies/dashboard").status_code == 403


def test_plain_http_install_addresses_only_for_named_qa_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, cp = stixis(None)
    cp.post("/admin/v1/companies", json=NEW)
    url = "http://captain-abc-admin:8100"
    assert cp.put("/admin/v1/companies/abc/install", json={"admin_url": url}).status_code == 400
    monkeypatch.setenv("CAPTAIN_INSECURE_INSTALL_HOSTS", "captain-abc-admin")
    assert cp.put("/admin/v1/companies/abc/install", json={"admin_url": url}).status_code == 200
    other = {"admin_url": "http://elsewhere:8100"}
    assert cp.put("/admin/v1/companies/abc/install", json=other).status_code == 400
