"""Company AI keys entered on screen and stored encrypted (plan step 3)."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from captain.api.admin_aikeys import install_aikey_routes
from captain.api.admin_queries import InMemoryAdminQueries
from captain.core.admin import AdminActor, AdminService
from captain.core.identity.rbac import PermissionDenied
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import ProviderTarget
from captain.core.registry.service import InMemoryRegistryStore, RegistryService
from captain.core.secrets_store import (
    InMemorySecretStore,
    LocalKeyWrapper,
    SecretsService,
    SecretsUnavailableError,
    build_wrapper,
)
from captain.integrations.api_gateway.provider import (
    ChainSecretResolver,
    EnvSecretResolver,
    OpenAICompatibleClient,
    ProviderError,
    StoredSecretResolver,
)
from tests.harness.adapters import CaptainCoreHarness

KEY = "fake-ai-key-for-tests-0123WXYZ"
MASTER = bytes(range(32))


def svc() -> SecretsService:
    return SecretsService(InMemorySecretStore(), LocalKeyWrapper(MASTER))


# ------------------------------------------------------------------ encryption


def test_round_trip_and_nothing_readable_at_rest() -> None:
    s = svc()
    stored = s.put("openai", KEY, "priya")
    assert s.reveal(stored.id) == KEY and stored.last4 == "WXYZ"
    raw = stored.ciphertext + stored.nonce + stored.wrapped_dek
    assert KEY.encode() not in raw and b"0123456789" not in raw


def test_wrong_master_key_cannot_decrypt() -> None:
    store = InMemorySecretStore()
    stored = SecretsService(store, LocalKeyWrapper(MASTER)).put("openai", KEY, "priya")
    other = SecretsService(store, LocalKeyWrapper(bytes(32)))
    with pytest.raises(SecretsUnavailableError):
        other.reveal(stored.id)


def test_ciphertext_is_bound_to_its_provider() -> None:
    """A key copied onto another provider's row fails its integrity check."""
    store = InMemorySecretStore()
    s = SecretsService(store, LocalKeyWrapper(MASTER))
    stored = s.put("openai", KEY, "priya")
    store.put(replace(stored, id="moved", provider_id="anthropic"))
    with pytest.raises(SecretsUnavailableError):
        s.reveal("moved")


def test_short_or_empty_keys_are_refused_and_deleted_keys_are_gone() -> None:
    s = svc()
    with pytest.raises(ValueError):
        s.put("openai", "  abc ", "priya")
    stored = s.put("openai", KEY, "priya")
    s.reveal(stored.id)  # cached
    assert s.delete(stored.id)
    with pytest.raises(KeyError):
        s.reveal(stored.id)  # delete also drops the in-memory copy


def test_master_key_spec() -> None:
    assert isinstance(build_wrapper("hex:" + MASTER.hex()), LocalKeyWrapper)
    for bad in ("hex:abcd", "plain:x"):
        with pytest.raises(ValueError):
            build_wrapper(bad)


# ------------------------------------------------------------------ admin API


def rig(
    roles: frozenset[str] = frozenset({"org-admin"}),
) -> tuple[CaptainCoreHarness, TestClient, SecretsService, list[httpx.Request]]:  # noqa: E501
    h = CaptainCoreHarness()
    reg = RegistryService(InMemoryRegistryStore())
    h.core.m.registry = reg
    h.built.infra.registry = reg
    s = svc()
    seen: list[httpx.Request] = []

    def provider(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        ok = req.headers.get("authorization") == f"Bearer {KEY}"
        return httpx.Response(200 if ok else 401, json={"data": []})

    app = FastAPI()

    @app.exception_handler(PermissionDenied)
    async def _denied(_: Any, e: PermissionDenied) -> JSONResponse:  # as the Admin API does
        return JSONResponse({"error": "FORBIDDEN"}, status_code=403)

    admin = AdminService(h.core, h.built.register, h.built.infra.budget_catalog)  # type: ignore[arg-type]
    install_aikey_routes(
        app, h.built, admin, InMemoryAdminQueries(h.built.infra),
        lambda: AdminActor("priya@corp.example", roles), s,
        httpx.Client(transport=httpx.MockTransport(provider)),
    )  # fmt: skip
    return h, TestClient(app), s, seen


def ledger(h: CaptainCoreHarness) -> str:
    store = h.built.infra.ledger_store
    assert isinstance(store, InMemoryLedgerStore)
    return json.dumps([b for (_p, _t, b) in store.outbox.values()])


def test_add_test_list_and_remove_a_key_without_ever_showing_it() -> None:
    h, c, s, seen = rig()
    r = c.post(
        "/admin/v1/ai-keys", json={"provider_id": "openai-main", "kind": "openai", "key": KEY}
    )
    assert r.status_code == 200, r.text
    assert KEY not in r.text and r.json()["key"]["last4"] == "WXYZ"
    p = h.core.m.registry.provider("openai-main")  # type: ignore[union-attr]
    assert p is not None and p.credential_ref.startswith("captain:")
    assert p.base_url == "https://api.openai.com/v1"

    listed = c.get("/admin/v1/ai-keys")
    assert KEY not in listed.text
    item = next(i for i in listed.json()["items"] if i["provider_id"] == "openai-main")
    assert item["key_source"] == "captain" and item["key"]["last4"] == "WXYZ"

    t = c.post("/admin/v1/ai-keys/openai-main/test").json()
    assert t == {"ok": True, "message": "The key works."}
    assert str(seen[0].url) == "https://api.openai.com/v1/models"

    text = ledger(h)
    assert "aikey.changed" in text and KEY not in text and "WXYZ" not in text

    sid = p.credential_ref.split(":", 1)[1]
    assert c.delete("/admin/v1/ai-keys/openai-main").json()["status"] == "removed"
    after = h.core.m.registry.provider("openai-main")  # type: ignore[union-attr]
    assert after is not None and after.active is False
    with pytest.raises(KeyError):
        s.reveal(sid)


def test_replacing_a_key_destroys_the_old_one() -> None:
    h, c, s, _ = rig()
    c.post("/admin/v1/ai-keys", json={"provider_id": "oa", "kind": "openai", "key": KEY})
    old = h.core.m.registry.provider("oa").credential_ref.split(":", 1)[1]  # type: ignore[union-attr]
    c.post("/admin/v1/ai-keys", json={"provider_id": "oa", "kind": "openai", "key": KEY + "2"})
    with pytest.raises(KeyError):
        s.reveal(old)
    assert '"action": "replaced"' in ledger(h)


def test_bad_inputs_and_viewers_are_refused() -> None:
    _, c, _, _ = rig()
    assert c.post("/admin/v1/ai-keys", json={"provider_id": "az", "kind": "azure_openai",
                                             "key": KEY}).status_code == 400  # fmt: skip
    bad_url = {"provider_id": "xx", "kind": "openai", "key": KEY, "base_url": "http://evil.example"}
    assert c.post("/admin/v1/ai-keys", json=bad_url).status_code == 400
    assert c.post("/admin/v1/ai-keys", json={"provider_id": "bb", "kind": "bedrock",
                                             "key": KEY}).status_code == 400  # fmt: skip
    _, viewer, _, _ = rig(frozenset({"viewer"}))
    r = viewer.post("/admin/v1/ai-keys", json={"provider_id": "oa", "kind": "openai", "key": KEY})
    assert r.status_code == 403


def test_switched_off_without_a_master_key() -> None:
    h = CaptainCoreHarness()
    reg = RegistryService(InMemoryRegistryStore())
    h.core.m.registry = reg
    h.built.infra.registry = reg
    app = FastAPI()
    admin = AdminService(h.core, h.built.register, h.built.infra.budget_catalog)  # type: ignore[arg-type]
    install_aikey_routes(app, h.built, admin, InMemoryAdminQueries(h.built.infra),
                         lambda: AdminActor("p", frozenset({"org-admin"})), None)  # fmt: skip
    c = TestClient(app)
    assert c.get("/admin/v1/ai-keys").json()["enabled"] is False
    r = c.post("/admin/v1/ai-keys", json={"provider_id": "oa", "kind": "openai", "key": KEY})
    assert r.status_code == 501


# ------------------------------------------------------------------ gateway


def test_gateway_calls_the_provider_with_the_stored_key() -> None:
    s = svc()
    stored = s.put("oa", KEY, "priya")
    resolver = ChainSecretResolver({"env": EnvSecretResolver({"X": "env-key"}),
                                    "captain": StoredSecretResolver(s)})  # fmt: skip
    seen: dict[str, Any] = {}

    def provider(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {}})

    client = OpenAICompatibleClient(resolver, transport=httpx.MockTransport(provider))
    target = ProviderTarget(
        alias="m", provider_id="oa", provider_kind="openai", provider_model="gpt",
        base_url="https://api.openai.com/v1", credential_ref=f"captain:{stored.id}",
    )  # fmt: skip
    client.complete(target, {"messages": [{"role": "user", "content": "x"}]})
    assert seen["auth"] == f"Bearer {KEY}"
    assert resolver.secret("env:X") == "env-key"  # existing refs keep working
    s.delete(stored.id)
    with pytest.raises(ProviderError, match="removed"):
        resolver.secret(f"captain:{stored.id}")
    with pytest.raises(ProviderError):
        resolver.secret("vault:other")
