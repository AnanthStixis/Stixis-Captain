"""Stixis control plane: the client companies and their installs (plan step 5).

Runs on Stixis's own Captain install (CAPTAIN_CONTROL_PLANE=1), so Stixis staff sign in with
Stixis SSO and every change lands in Stixis's evidence ledger. Role `platform-admin`.

    GET  /admin/v1/companies                     the company list (with last health check)
    POST /admin/v1/companies                     add a company (makes its platform token)
    GET  /admin/v1/companies/{code}              one company
    PUT  /admin/v1/companies/{code}              details, status, org admin
    PUT  /admin/v1/companies/{code}/install      where its install is reached
    GET  /admin/v1/companies/{code}/install-files  tfvars, backend file, Helm values, token
    POST /admin/v1/companies/{code}/bootstrap    push details + create the org admin invite
    POST /admin/v1/companies/{code}/refresh      fetch health and counts from the install

Each company keeps its own isolated install; nothing of a company's evidence comes here.
The platform token is stored encrypted (captain.core.secrets_store, CAPTAIN_SECRETS_KEY).
"""

# No `from __future__ import annotations`: FastAPI must resolve the local `Actor` alias.
import io
import json
import re
import secrets as pysecrets
import threading
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from captain.api.org import Contact, Organization
from captain.core.admin import AdminActor
from captain.core.engine import admin_event
from captain.core.factory import BuiltCore
from captain.core.identity.rbac import Permission, require
from captain.core.models import EventType
from captain.core.secrets_store import SecretsService
from captain.envfile import current_env

CODE = r"^[a-z][a-z0-9]{1,11}$"  # also the OpenTofu `client` code


class CompanyDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_name: str = Field(min_length=1, max_length=200)
    legal_name: str = Field(default="", max_length=300)
    domains: list[str] = Field(default_factory=list, max_length=50)
    country: str = Field(default="", max_length=64)
    industry: str = Field(default="", max_length=120)
    contacts: list[Contact] = Field(default_factory=list, max_length=20)

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        # The same rule the company's install applies at bootstrap: catch it here, at entry.
        return Organization.model_validate({"domains": v}).domains


class NewCompany(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=CODE)
    details: CompanyDetails
    region: str = Field(default="westeurope", max_length=40)
    environment: Literal["pilot", "prod"] = "pilot"
    org_admin_email: str = Field(min_length=3, max_length=128)
    org_admin_name: str = Field(default="", max_length=200)


class CompanyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    details: CompanyDetails | None = None
    status: Literal["onboarding", "active", "suspended"] | None = None
    org_admin_email: str | None = Field(default=None, max_length=128)
    org_admin_name: str | None = Field(default=None, max_length=200)


class InstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    admin_url: str = Field(max_length=300)  # the install's Admin API / console address


class CompanyStore(Protocol):
    def all(self) -> list[dict[str, Any]]: ...
    def get(self, code: str) -> dict[str, Any] | None: ...
    def put(self, doc: dict[str, Any]) -> None: ...


class InMemoryCompanyStore:
    def __init__(self) -> None:
        self._d: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def all(self) -> list[dict[str, Any]]:
        return [dict(v) for _, v in sorted(self._d.items())]

    def get(self, code: str) -> dict[str, Any] | None:
        v = self._d.get(code)
        return dict(v) if v else None

    def put(self, doc: dict[str, Any]) -> None:
        with self._lock:
            self._d[doc["code"]] = dict(doc)


def _check_url(url: str) -> str:
    u = url.strip().rstrip("/")
    if re.match(r"^https://[^\s/]+", u) or re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", u):
        return u
    # QA / test only: hosts listed in CAPTAIN_INSECURE_INSTALL_HOSTS may use plain http
    # (e.g. the client install's container name on a private Docker network).
    allowed = {h.strip().lower() for h in current_env().get("CAPTAIN_INSECURE_INSTALL_HOSTS", "")
               .split(",") if h.strip()}  # fmt: skip
    m = re.match(r"^http://([a-z0-9.-]+)(:\d+)?$", u.lower())
    if m and m.group(1) in allowed:
        return u
    raise HTTPException(400, {"error": "BAD_URL", "message": "the address must be https://"})


def _fail(error: str, message: str) -> None:
    raise HTTPException(502, {"error": error, "message": message})


def company_view(doc: dict[str, Any]) -> dict[str, Any]:
    """Never the token, never its secret id."""
    return {k: v for k, v in doc.items() if k != "token_secret_id"} | {
        "shared": "provisioned" in doc
    }


def install_files(doc: dict[str, Any], token: str) -> bytes:
    code, env, region = doc["code"], doc["environment"], doc["region"]
    tfvars = (
        f'# Generated by the Stixis control plane for {doc["details"]["company_name"]}.\n'
        'subscription_id = "00000000-0000-0000-0000-000000000000" # CHANGE: the client\'s\n'
        f'client          = "{code}"\n'
        f'environment     = "{env}"\n'
        f'location        = "{region}"\n'
        'address_space   = "10.40.0.0/16"     # CHANGE if it overlaps the client network\n'
        'admin_cidrs     = ["203.0.113.0/24"] # CHANGE: admin / VPN ranges\n'
        + ("postgres_high_availability = true\n" if env == "prod" else "")
    )
    backend = (
        'resource_group_name  = "rg-captain-tfstate" # CHANGE\n'
        'storage_account_name = "stcaptaintfstate"   # CHANGE\n'
        'container_name       = "tfstate"\n'
        f'key                  = "{code}-{env}.tfstate"\n'
        "use_azuread_auth     = true\n"
    )
    values = (
        "# Add `tofu output -raw helm_values` from the install, then these lines.\n"
        "azure:\n  secrets:\n"
        "    CAPTAIN_DATABASE_URL: captain-database-url\n"
        "    CAPTAIN_KEY_PEPPER: captain-key-pepper\n"
        "    CAPTAIN_PLATFORM_TOKEN: captain-platform-token\n"
    )
    readme = (
        f"Captain install files for {doc['details']['company_name']} ({code}, {env}).\n\n"
        "1. Copy client.tfvars and client.backend.hcl to deploy/opentofu/azure/clients/\n"
        f"   as {code}.tfvars and {code}.backend.hcl, fill in the CHANGE lines, then follow\n"
        "   deploy/opentofu/azure/README.md (tofu apply, helm install).\n"
        "2. Store platform-token.txt in the install's Key Vault as the secret\n"
        "   'captain-platform-token', then DELETE platform-token.txt. Merge\n"
        "   helm-values-extra.yaml into the Helm values so the install reads it.\n"
        "3. In the Stixis console, set the install's address, then press Bootstrap to\n"
        "   create the org admin's one-time sign-in link.\n"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", readme)
        z.writestr("client.tfvars", tfvars)
        z.writestr("client.backend.hcl", backend)
        z.writestr("helm-values-extra.yaml", values)
        z.writestr("platform-token.txt", token + "\n")
    return out.getvalue()


def install_controlplane_routes(
    app: FastAPI,
    built: BuiltCore,
    actor_dep: Callable[..., AdminActor],
    store: CompanyStore,
    secrets: SecretsService | None,
    http: httpx.Client | None = None,
    queries: Any = None,  # AdminQueries: recent company changes on the dashboard
    provision: Callable[[str], str] | None = None,
) -> None:
    """`provision` is set on a shared Captain (ADR-008): adding a company creates its own
    schema and address at once; no install files, no separate deployment."""
    Actor = Annotated[AdminActor, Depends(actor_dep)]

    def do_provision(a: AdminActor, doc: dict[str, Any]) -> None:
        assert provision is not None
        try:
            doc["admin_url"] = provision(doc["code"])
        except Exception as e:  # noqa: BLE001 - shown to the platform admin, retryable
            doc["last_error"] = f"Could not create the company's storage: {e}"
            store.put(doc)
            raise HTTPException(502, {"error": "PROVISION_FAILED",
                                      "message": doc["last_error"]}) from e  # fmt: skip
        doc["provisioned"], doc["last_error"] = True, None
        store.put(doc)
        record(a, doc["code"], "provisioned")

    client = http or httpx.Client(timeout=20)

    def need_secrets() -> SecretsService:
        if secrets is None:
            raise HTTPException(501, {"error": "NOT_CONFIGURED",
                                      "message": "CAPTAIN_SECRETS_KEY is required to keep "
                                                 "platform tokens."})  # fmt: skip
        return secrets

    def record(a: AdminActor, code: str, action: str) -> None:
        built.core.record(
            admin_event(EventType.COMPANY_CHANGED, a.id, {"company": code, "action": action})
        )

    def load(code: str) -> dict[str, Any]:
        doc = store.get(code)
        if doc is None:
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no such company"})
        return doc

    def call_install(doc: dict[str, Any], method: str, path: str, body: Any = None) -> Any:
        url = doc.get("admin_url")
        if not url:
            raise HTTPException(409, {"error": "NO_INSTALL",
                                      "message": "Set the install's address first."})  # fmt: skip
        token = need_secrets().reveal(doc["token_secret_id"])
        try:
            r = client.request(method, f"{url}{path}", json=body,
                               headers={"X-Captain-Platform-Token": token})  # fmt: skip
        except httpx.HTTPError as e:
            raise HTTPException(502, {"error": "UNREACHABLE",
                                      "message": f"The install could not be reached "
                                                 f"({type(e).__name__})."}) from e  # fmt: skip
        if r.status_code in (401, 404):
            _fail(
                "TOKEN_REJECTED",
                "The install did not accept the platform token "
                "(not set there, or a different one).",
            )
        if r.status_code >= 400:
            try:
                why = r.json().get("detail")
            except ValueError:
                why = None
            if isinstance(why, list) and why and isinstance(why[0], dict):  # validation error
                why = why[0].get("msg")
            elif isinstance(why, dict):
                why = why.get("message")
            _fail(
                "INSTALL_ERROR",
                f"The install answered {r.status_code}" + (f": {why}" if why else "."),
            )
        return r.json()

    @app.get("/admin/v1/companies/dashboard")
    def dashboard(a: Actor) -> dict[str, Any]:
        """The Stixis dashboard: built from the company list and each company's last health
        summary (counts only). No company's evidence is read."""
        require(a.roles, Permission.COMPANIES_WRITE)
        docs = store.all()
        status = {k: 0 for k in ("onboarding", "active", "suspended")}
        health = {k: 0 for k in ("healthy", "problem", "not_installed", "not_checked")}
        pipeline = {"added": 0, "address_set": 0, "bootstrapped": 0, "healthy": 0}
        totals = {"users": 0, "org_admins": 0, "active_keys": 0, "calls_last_7_days": 0}
        attention: list[dict[str, Any]] = []
        usage: list[dict[str, Any]] = []
        for d in docs:
            name = d["details"]["company_name"]
            status[d["status"]] = status.get(d["status"], 0) + 1
            s = d.get("last_summary")
            pipeline["added"] += 1
            if d.get("admin_url"):
                pipeline["address_set"] += 1
            if d.get("bootstrapped_at"):
                pipeline["bootstrapped"] += 1
            if not d.get("admin_url"):
                health["not_installed"] += 1
                if d["status"] != "suspended":
                    attention.append({"code": d["code"], "name": name,
                                      "reason": "Install not connected yet"})  # fmt: skip
            elif d.get("last_error"):
                health["problem"] += 1
                attention.append({"code": d["code"], "name": name, "reason": d["last_error"]})
            elif s:
                health["healthy"] += 1
                pipeline["healthy"] += 1
                for k in totals:
                    totals[k] += int(s.get(k) or 0)
                calls = int(s.get("calls_last_7_days") or 0)
                usage.append({"code": d["code"], "name": name, "calls_last_7_days": calls})
                if not d.get("bootstrapped_at") and not s.get("org_admins"):
                    attention.append({"code": d["code"], "name": name,
                                      "reason": "No org admin yet: press Bootstrap"})  # fmt: skip
            else:
                health["not_checked"] += 1
                attention.append({"code": d["code"], "name": name,
                                  "reason": "Not checked yet: press Check now"})  # fmt: skip
        recent: list[dict[str, Any]] = []
        if queries is not None:
            _, rows = queries.events_page(None, None, {"company.changed"}, None, None, None, 8, 0)
            from captain.api.admin_queries import event_view

            recent = [{"when": e.recorded_at.isoformat(), "headline": v["headline"]}
                      for e in rows for v in [event_view(e)]]  # fmt: skip
        return {
            "companies": len(docs), "status": status, "health": health, "pipeline": pipeline,
            "totals": totals, "needs_attention": attention[:10],
            "top_usage": sorted(usage, key=lambda u: -u["calls_last_7_days"])[:5],
            "recent_changes": recent,
        }  # fmt: skip

    @app.get("/admin/v1/companies")
    def list_companies(a: Actor) -> list[dict[str, Any]]:
        require(a.roles, Permission.COMPANIES_WRITE)
        return [company_view(d) for d in store.all()]

    @app.post("/admin/v1/companies")
    def add_company(a: Actor, body: NewCompany) -> dict[str, Any]:
        require(a.roles, Permission.COMPANIES_WRITE)
        svc = need_secrets()
        if store.get(body.code) is not None:
            raise HTTPException(409, {"error": "EXISTS", "message": "that code is taken"})
        token = pysecrets.token_urlsafe(32)
        stored = svc.put(f"platform-{body.code}", token, a.id)
        now = datetime.now(UTC).isoformat()
        doc: dict[str, Any] = {
            "code": body.code, "details": body.details.model_dump(), "status": "onboarding",
            "region": body.region, "environment": body.environment,
            "org_admin_email": body.org_admin_email.strip().lower(),
            "org_admin_name": body.org_admin_name.strip(), "admin_url": None,
            "token_secret_id": stored.id, "created_at": now, "created_by": a.id,
            "bootstrapped_at": None, "last_summary": None, "last_checked": None,
            "last_error": None,
        }  # fmt: skip
        if provision is not None:
            doc["provisioned"] = False  # shared Captain: storage is created next
        record(a, body.code, "added")
        store.put(doc)
        if provision is not None:
            do_provision(a, doc)
        return company_view(doc)

    @app.post("/admin/v1/companies/{code}/provision")
    def reprovision(a: Actor, code: str) -> dict[str, Any]:
        """Shared Captain: create (or finish creating) the company's storage."""
        require(a.roles, Permission.COMPANIES_WRITE)
        if provision is None:
            raise HTTPException(404, {"error": "NOT_SHARED"})
        doc = load(code)
        do_provision(a, doc)
        return company_view(doc)

    @app.get("/admin/v1/companies/{code}")
    def get_company(a: Actor, code: str) -> dict[str, Any]:
        require(a.roles, Permission.COMPANIES_WRITE)
        return company_view(load(code))

    @app.put("/admin/v1/companies/{code}")
    def update_company(a: Actor, code: str, body: CompanyUpdate) -> dict[str, Any]:
        require(a.roles, Permission.COMPANIES_WRITE)
        doc = load(code)
        if body.details is not None:
            doc["details"] = body.details.model_dump()
        if body.status is not None:
            doc["status"] = body.status
        if body.org_admin_email is not None:
            doc["org_admin_email"] = body.org_admin_email.strip().lower()
        if body.org_admin_name is not None:
            doc["org_admin_name"] = body.org_admin_name.strip()
        record(a, code, f"updated (status {doc['status']})")
        store.put(doc)
        return company_view(doc)

    @app.put("/admin/v1/companies/{code}/install")
    def set_install(a: Actor, code: str, body: InstallBody) -> dict[str, Any]:
        require(a.roles, Permission.COMPANIES_WRITE)
        doc = load(code)
        doc["admin_url"] = _check_url(body.admin_url)
        record(a, code, "set the install address of")
        store.put(doc)
        return company_view(doc)

    @app.get("/admin/v1/companies/{code}/install-files")
    def get_install_files(a: Actor, code: str) -> Response:
        require(a.roles, Permission.COMPANIES_WRITE)
        doc = load(code)
        token = need_secrets().reveal(doc["token_secret_id"])
        record(a, code, "downloaded the install files (with platform token) of")
        return Response(
            install_files(doc, token), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="captain-install-{code}.zip"'},
        )  # fmt: skip

    @app.post("/admin/v1/companies/{code}/bootstrap")
    def bootstrap(a: Actor, code: str) -> dict[str, Any]:
        require(a.roles, Permission.COMPANIES_WRITE)
        doc = load(code)
        body = {"company": {**doc["details"], "adopters": [], "checklist": {}},
                "org_admin_email": doc["org_admin_email"],
                "org_admin_name": doc["org_admin_name"]}  # fmt: skip
        result = call_install(doc, "POST", "/admin/v1/platform/bootstrap", body)
        doc["bootstrapped_at"] = datetime.now(UTC).isoformat()
        record(a, code, "bootstrapped")
        store.put(doc)
        invite = result.get("invite")
        return {
            "invite_url": f"{doc['admin_url']}{invite['link_path']}" if invite else None,
            "expires_at": invite.get("expires_at") if invite else None,
            "message": result.get("message")
            or "Send this link to the org admin. It works "
            "once, for 7 days. The link is not kept: press Bootstrap again for a new one.",
        }

    @app.post("/admin/v1/companies/{code}/refresh")
    def refresh(a: Actor, code: str) -> dict[str, Any]:
        require(a.roles, Permission.COMPANIES_WRITE)
        doc = load(code)
        doc["last_checked"] = datetime.now(UTC).isoformat()
        try:
            doc["last_summary"] = call_install(doc, "GET", "/admin/v1/platform/summary")
            doc["last_error"] = None
        except HTTPException as e:
            detail: dict[str, Any] = e.detail if isinstance(e.detail, dict) else {}
            doc["last_error"] = str(detail.get("message") or e.detail)
        store.put(doc)
        return company_view(doc)


class PgCompanyStore:
    def __init__(self, engine: Any) -> None:
        self._e = engine

    def all(self) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from captain.db import schema as s

        with self._e.connect() as c:
            rows = c.execute(select(s.cp_companies.c.doc).order_by(s.cp_companies.c.code))
            return [dict(r.doc) for r in rows]

    def get(self, code: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from captain.db import schema as s

        t = s.cp_companies
        with self._e.connect() as c:
            r = c.execute(select(t.c.doc).where(t.c.code == code)).first()
        return dict(r.doc) if r else None

    def put(self, doc: dict[str, Any]) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from captain.db import schema as s

        t = s.cp_companies
        now = datetime.now(UTC)
        stmt = insert(t).values(code=doc["code"], doc=json.loads(json.dumps(doc)), updated_at=now)
        with self._e.begin() as c:
            up = {"doc": stmt.excluded.doc, "updated_at": now}
            c.execute(stmt.on_conflict_do_update(index_elements=[t.c.code], set_=up))
