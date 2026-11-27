"""Shared Captain: one deployment serving many companies (ADR-008).

Every company keeps its own data in its own PostgreSQL schema (t_<code>), its own ledger
signing key and anchors, its own users, AI keys, models, budgets and SSO. Only the
programs are shared. A request is matched to its company by the web address it was sent
to (abc.stixis.com -> company "abc"); Stixis's own console is at the platform address
(captain.stixis.com). Each company gets its own copy of the Captain app, built with that
company's settings, so the code inside a company is exactly the single-company code.

Settings (on top of the normal ones):
  CAPTAIN_MULTI_TENANT=1          turn this on
  CAPTAIN_BASE_DOMAIN             e.g. stixis.com  -> companies at <code>.stixis.com
  CAPTAIN_PLATFORM_HOST           default captain.<base domain> (the Stixis console)
  CAPTAIN_PUBLIC_SCHEME           https (default) or http (closed QA network only)
  CAPTAIN_TENANT_DATA_DIR         ledger keys and anchors, one folder per company (/data)
  CAPTAIN_TENANT_DB_POOL_SIZE     database connections per company per service (default 3)
  CAPTAIN_TENANT_RPM              AI calls per minute per company per gateway copy (600)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from captain.envfile import DEFAULT_ENV_FILE, _overlay, current_env, tenant_env

log = logging.getLogger("captain.tenancy")

PLATFORM = "_platform"  # Stixis's own console (the control plane), in the public schema
CODE_RE = re.compile(r"^[a-z][a-z0-9]{1,11}$")  # the same rule as company codes

ASGIApp = Callable[[dict[str, Any], Any, Any], Awaitable[None]]


def schema_of(code: str) -> str | None:
    return None if code == PLATFORM else f"t_{code}"


class Tenancy:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        from captain.api.controlplane import PgCompanyStore
        from captain.config import build_secrets, load_settings

        self.env = dict(env if env is not None else current_env())
        self.domain = self.env.get("CAPTAIN_BASE_DOMAIN", "").strip().lower().strip(".")
        if not self.domain:
            raise SystemExit("CAPTAIN_BASE_DOMAIN is required (e.g. stixis.com)")
        self.platform_host = (
            self.env.get("CAPTAIN_PLATFORM_HOST") or f"captain.{self.domain}"
        ).lower()
        self.scheme = self.env.get("CAPTAIN_PUBLIC_SCHEME", "https")
        self.data = Path(self.env.get("CAPTAIN_TENANT_DATA_DIR", "/data"))
        self.base_url = self.env["CAPTAIN_DATABASE_URL"]
        self.engine = create_engine(self.base_url, pool_size=2, max_overflow=2, pool_pre_ping=True)
        self.companies = PgCompanyStore(self.engine)
        self._known: dict[str, tuple[float, bool]] = {}
        # No global ledger key or anchor folder in shared mode: each company (Stixis
        # included) has its own, so read the shared settings with Stixis's.
        self.secrets = None
        platform = {**self.env, **self.overrides(PLATFORM)}
        self.secrets = build_secrets(load_settings(platform), self.engine)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ who is it
    def host_of(self, code: str) -> str:
        return self.platform_host if code == PLATFORM else f"{code}.{self.domain}"

    def url_of(self, code: str) -> str:
        return f"{self.scheme}://{self.host_of(code)}"

    def code_for_host(self, host: str) -> str | None:
        h = host.split(":", 1)[0].strip().lower().rstrip(".")
        if h == self.platform_host:
            return PLATFORM
        if not h.endswith("." + self.domain):
            return None
        code = h[: -len(self.domain) - 1]
        return code if CODE_RE.match(code) and self._active(code) else None

    def _active(self, code: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hit = self._known.get(code)
            if hit and now - hit[0] < 10:
                return hit[1]
        doc = self.companies.get(code)
        ok = doc is not None and doc.get("status") != "suspended" and bool(doc.get("provisioned"))
        with self._lock:
            self._known[code] = (now, ok)
        return ok

    def forget(self, code: str) -> None:
        with self._lock:
            self._known.pop(code, None)

    def all_codes(self) -> list[str]:
        return [PLATFORM] + sorted(
            d["code"]
            for d in self.companies.all()
            if d.get("provisioned") and d.get("status") != "suspended"
        )

    # ------------------------------------------------------------- settings
    def database_url(self, code: str) -> str:
        schema = schema_of(code)
        if schema is None:
            return self.base_url
        url = make_url(self.base_url).update_query_dict({"options": f"-csearch_path={schema}"})
        return url.render_as_string(hide_password=False)

    def overrides(self, code: str) -> dict[str, str]:
        """Everything that differs per company. Applied on top of the shared settings."""
        url = self.url_of(code)
        keys, anchors = self.data / "keys", self.data / "anchors" / code
        anchors.mkdir(parents=True, exist_ok=True)
        key = keys / f"{code}.pem"
        if not key.exists():
            from captain.core.ledger.signers import PemFileSigner

            keys.mkdir(parents=True, exist_ok=True)
            PemFileSigner.generate(key)
        o = {
            "CAPTAIN_DATABASE_URL": self.database_url(code),
            "CAPTAIN_DB_POOL_SIZE": self.env.get("CAPTAIN_TENANT_DB_POOL_SIZE", "3"),
            "CAPTAIN_LEDGER_KEY": f"pem:{key}",
            "CAPTAIN_LEDGER_KEY_ID": f"{code.strip('_')}-ledger-1",
            "CAPTAIN_WORM_DIR": str(anchors),
            "CAPTAIN_PUBLIC_CONSOLE_URL": url,
            "CAPTAIN_PUBLIC_GATEWAY_URL": url,
            "CAPTAIN_PUBLIC_CORE_URL": url,
            "CAPTAIN_PUBLIC_MCP_URL": f"{url}/mcp",
            "CAPTAIN_PUBLIC_HOSTS": self.host_of(code),
            "CAPTAIN_CONSOLE_ORIGINS": url,
            "CAPTAIN_CONTROL_PLANE": "1" if code == PLATFORM else "",
            "CAPTAIN_PLATFORM_TOKEN": "",
            "CAPTAIN_TENANT": code,
        }
        o.pop("CAPTAIN_WORM", None)
        if code != PLATFORM:
            doc = self.companies.get(code)
            if doc and self.secrets is not None and doc.get("token_secret_id"):
                o["CAPTAIN_PLATFORM_TOKEN"] = self.secrets.reveal(doc["token_secret_id"])
        return o

    # ------------------------------------------------------------ new company
    def provision(self, code: str) -> str:
        """Create the company's schema and tables (idempotent). Returns its address."""
        if not CODE_RE.match(code):
            raise ValueError("invalid company code")
        with self.engine.begin() as c:
            c.execute(text(f'CREATE SCHEMA IF NOT EXISTS "t_{code}"'))
        from alembic import command
        from alembic.config import Config

        ini = Path(
            self.env.get("CAPTAIN_ALEMBIC_INI")
            or (Path.cwd() / "alembic.ini" if (Path.cwd() / "alembic.ini").exists() else "")
            or DEFAULT_ENV_FILE.parent / "alembic.ini"
        )
        cfg = Config(str(ini))
        cfg.set_main_option("script_location", str(ini.parent / "migrations"))
        with tenant_env({"CAPTAIN_DATABASE_URL": self.database_url(code)}):
            command.upgrade(cfg, "head")
        self.overrides(code)  # creates its ledger key and anchor folder
        self.forget(code)
        log.info("company %s provisioned", code)
        return self.url_of(code)


# ======================================================================== router
class TenantRouter:
    """One ASGI app in front of every company's copy of a Captain service."""

    def __init__(
        self,
        tenancy: Tenancy,
        factory: Callable[[], Any],
        kind: str,
        rpm: int | None = None,
    ) -> None:
        self.tenancy, self.factory, self.kind, self.rpm = tenancy, factory, kind, rpm
        self._apps: dict[str, tuple[Any, dict[str, str]]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._bucket: dict[str, tuple[float, float]] = {}

    def app_for(self, code: str) -> tuple[Any, dict[str, str]]:
        hit = self._apps.get(code)
        if hit is not None:
            return hit
        with self._guard:
            lock = self._locks.setdefault(code, threading.Lock())
        with lock:
            hit = self._apps.get(code)
            if hit is None:
                o = self.tenancy.overrides(code)
                with tenant_env(o):
                    app = self.factory()
                hit = (app, o)
                self._apps[code] = hit
                log.info("%s for %s ready", self.kind, code)
            return hit

    def _allow(self, code: str) -> bool:
        """Fair share: a token bucket of `rpm` calls per minute per company."""
        if not self.rpm:
            return True
        now = time.monotonic()
        with self._guard:
            tokens, at = self._bucket.get(code, (float(self.rpm), now))
            tokens = min(float(self.rpm), tokens + (now - at) * self.rpm / 60.0)
            ok = tokens >= 1.0
            self._bucket[code] = (tokens - 1.0 if ok else tokens, now)
        return ok

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        host = ""
        for k, v in scope.get("headers", []):
            if k == b"host":
                host = v.decode("latin-1")
                break
        code = self.tenancy.code_for_host(host)
        if code is None:
            await _json(send, 404, {"error": "UNKNOWN_COMPANY",
                                    "message": "No Captain company at this address."})  # fmt: skip
            return
        if self.kind == "gateway" and not self._allow(code):
            msg = "Too many AI calls for this company; retry shortly."
            await _json(send, 429, {"error": "COMPANY_RATE_LIMIT", "message": msg})
            return
        import anyio

        app, o = await anyio.to_thread.run_sync(self.app_for, code)
        token = _overlay.set(o)  # request-time settings reads see this company's
        try:
            await app(scope, receive, send)
        finally:
            _overlay.reset(token)

    def local_client(self) -> Any:
        """An HTTP client that reaches companies through this router in-process
        (the Stixis console's bootstrap and health checks never leave the server)."""
        from fastapi.testclient import TestClient

        return TestClient(self, raise_server_exceptions=False)


async def _json(send: Any, status: int, body: dict[str, Any]) -> None:
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(data)).encode())]})  # fmt: skip
    await send({"type": "http.response.body", "body": data})


# ========================================================== service factories
_ACTIVE: dict[str, Any] = {}


def active() -> dict[str, Any]:
    """Set when the admin service runs shared: lets the Stixis console provision
    companies and reach them in-process."""
    return _ACTIVE


def create_admin_router() -> TenantRouter:
    from captain.api.admin_main import create_admin_app_from_env

    t = Tenancy()
    router = TenantRouter(t, create_admin_app_from_env, "admin")
    _ACTIVE.update(tenancy=t, router=router)
    return router


def create_gateway_router() -> TenantRouter:
    from captain.api.gateway_main import create_gateway_app_from_env

    t = Tenancy()
    rpm = int(t.env.get("CAPTAIN_TENANT_RPM", "600"))
    return TenantRouter(t, create_gateway_app_from_env, "gateway", rpm=rpm)


def create_core_router() -> TenantRouter:
    from captain.api.core_http import create_app_from_env

    return TenantRouter(Tenancy(), create_app_from_env, "core")


def create_mcp_router() -> TenantRouter:
    from captain.api.mcp_main import create_mcp_app_from_env

    return TenantRouter(Tenancy(), create_mcp_app_from_env, "mcp")


# ===================================================================== workers
def run_all_workers(build: Callable[[], Any], stop: threading.Event, every_s: float = 30) -> None:
    """One worker (its own ledger lease) per company; new companies are picked up."""
    import contextvars

    t = Tenancy()
    running: dict[str, Any] = {}
    while not stop.is_set():
        for code in t.all_codes():
            if code in running:
                continue
            try:
                with tenant_env(t.overrides(code)):
                    w = build()
                    ctx = contextvars.copy_context()
                threading.Thread(target=ctx.run, args=(w.run_forever,),
                                 name=f"worker-{code}", daemon=True).start()  # fmt: skip
                running[code] = w
                log.info("worker for %s started", code)
            except Exception:  # noqa: BLE001 - one company must not stop the others
                log.exception("worker for %s could not start", code)
        stop.wait(every_s)
    for w in running.values():
        w.stop()


# ======================================================================== CLI
def migrate_all() -> None:
    """Bring Stixis's schema and every company's schema to the current version (run on
    every upgrade: the QA/production `init` step does it)."""
    from alembic import command
    from alembic.config import Config

    t = Tenancy()
    ini = Path(t.env.get("CAPTAIN_ALEMBIC_INI") or Path.cwd() / "alembic.ini")
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "migrations"))
    with tenant_env({"CAPTAIN_DATABASE_URL": t.base_url}):
        command.upgrade(cfg, "head")
    for code in t.all_codes():
        if code != PLATFORM:
            t.provision(code)  # idempotent: creates what is missing, migrates the rest
            print(f"company {code}: up to date")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if sys.argv[1:] == ["migrate-all"]:
        migrate_all()
    else:
        print("usage: python -m captain.tenancy migrate-all")
        raise SystemExit(2)
