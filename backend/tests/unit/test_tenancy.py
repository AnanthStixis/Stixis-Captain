"""Shared Captain (ADR-008): the router picks the company by web address, each company's
copy of the app is built with that company's own settings only, unknown addresses get
nothing, and one company cannot use up another's share of the gateway."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain.db.bus import channel_for
from captain.db.lease import lease_name
from captain.envfile import current_env
from captain.tenancy import PLATFORM, TenantRouter


class FakeTenancy:
    known = {"captain.stixis.test": PLATFORM, "abc.stixis.test": "abc", "xyz.stixis.test": "xyz"}

    def code_for_host(self, host: str) -> str | None:
        return self.known.get(host.split(":", 1)[0])

    def overrides(self, code: str) -> dict[str, str]:
        schema = "public" if code == PLATFORM else f"t_{code}"
        return {"CAPTAIN_TENANT": code, "CAPTAIN_DATABASE_URL": f"postgresql://x/db?{schema}"}


def factory() -> FastAPI:
    built_with = dict(current_env())  # what the company's app was built with
    app = FastAPI()

    @app.get("/whoami")
    def whoami() -> dict[str, Any]:
        return {
            "built": built_with.get("CAPTAIN_TENANT"),
            "now": current_env().get("CAPTAIN_TENANT"),
        }

    return app


def client(router: TenantRouter, host: str) -> TestClient:
    return TestClient(router, base_url=f"http://{host}")


def test_each_address_reaches_its_own_company_only() -> None:
    r = TenantRouter(FakeTenancy(), factory, "admin")  # type: ignore[arg-type]
    for host, code in (("abc.stixis.test", "abc"), ("xyz.stixis.test", "xyz"),
                       ("captain.stixis.test", PLATFORM)):  # fmt: skip
        body = client(r, host).get("/whoami").json()
        assert body == {"built": code, "now": code}, host
    # The process itself never carries a company's settings.
    assert current_env().get("CAPTAIN_TENANT") is None


def test_unknown_or_suspended_addresses_get_nothing() -> None:
    r = TenantRouter(FakeTenancy(), factory, "admin")  # type: ignore[arg-type]
    for host in ("evil.example", "nope.stixis.test", "stixis.test"):
        res = client(r, host).get("/whoami")
        assert res.status_code == 404 and res.json()["error"] == "UNKNOWN_COMPANY"


def test_one_app_per_company_reused_across_requests() -> None:
    built: list[str] = []

    def counting() -> FastAPI:
        built.append(current_env()["CAPTAIN_TENANT"])
        return factory()

    r = TenantRouter(FakeTenancy(), counting, "admin")  # type: ignore[arg-type]
    for _ in range(3):
        client(r, "abc.stixis.test").get("/whoami")
    client(r, "xyz.stixis.test").get("/whoami")
    assert built == ["abc", "xyz"]


def test_a_busy_company_is_limited_without_slowing_the_others() -> None:
    r = TenantRouter(FakeTenancy(), factory, "gateway", rpm=3)  # type: ignore[arg-type]
    abc = client(r, "abc.stixis.test")
    codes = [abc.get("/whoami").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    assert client(r, "xyz.stixis.test").get("/whoami").status_code == 200


def test_messages_and_ledger_locks_are_per_company() -> None:
    base = "postgresql+psycopg://u:p@h/db"
    abc = base + "?options=-csearch_path%3Dt_abc"
    xyz = base + "?options=-csearch_path%3Dt_xyz"
    assert channel_for(base) == "captain_events"
    assert channel_for(abc) == "captain_events_t_abc" != channel_for(xyz)
    assert lease_name(abc) != lease_name(xyz) != lease_name(base)
