"""Admin API: the company's AI (provider) keys, entered on screen, stored encrypted.

    GET    /admin/v1/ai-keys                       providers and whether each has a key
    POST   /admin/v1/ai-keys                       add or replace the key for a provider
    POST   /admin/v1/ai-keys/{provider_id}/test    one cheap call to check the key works
    GET    /admin/v1/ai-keys/{provider_id}/models  the provider's chat models, live, with
                                                   suggested prices and what is added
    DELETE /admin/v1/ai-keys/{provider_id}         remove the key (the provider stops)

The key is never returned, logged or written to the evidence ledger: only the provider,
the action, and who did it. The dashboard shows the last 4 characters so people can tell
keys apart. Needs `CAPTAIN_SECRETS_KEY` (see captain.core.secrets_store); without it these
endpoints answer 501 and keys stay in the server's vault as before.
"""

# No `from __future__ import annotations`: FastAPI must resolve the local `Actor` alias.
import re
from collections.abc import Callable
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from captain.api.admin_queries import AdminQueries
from captain.core.admin import AdminActor, AdminService
from captain.core.engine import admin_event
from captain.core.factory import BuiltCore
from captain.core.identity.rbac import Permission, require
from captain.core.models import EventType
from captain.core.registry.service import Provider, ProviderKind
from captain.core.secrets_store import REF_PREFIX, SecretsService, secret_view
from captain.core.usage.list_prices import PRICE_FEED_URL, PriceFeed, is_chat_model

# Where each kind of provider is reached when the admin does not give an address.
DEFAULT_URLS: dict[str, str] = {
    ProviderKind.OPENAI: "https://api.openai.com/v1",
    ProviderKind.ANTHROPIC: "https://api.anthropic.com/v1",
    ProviderKind.GEMINI: "https://generativelanguage.googleapis.com/v1beta/openai",
    ProviderKind.MISTRAL: "https://api.mistral.ai/v1",
}
REJECTED = "The provider rejected this key."
NEEDS_URL = {ProviderKind.AZURE_OPENAI, ProviderKind.SELF_HOSTED, ProviderKind.COHERE}
NOT_KEY_BASED = {ProviderKind.BEDROCK}  # AWS credentials, not an API key


class AiKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
    kind: ProviderKind
    key: SecretStr
    base_url: str | None = Field(default=None, max_length=300)
    region: str = Field(default="external", max_length=40)
    # Save even when the provider cannot be reached right now (a rejected key never saves).
    skip_check: bool = False


def _check_url(url: str) -> str:
    if not re.match(r"^https://[^\s/]+", url) and not re.match(
        r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", url
    ):
        raise HTTPException(400, {"error": "BAD_URL", "message": "the address must be https://"})
    return url.rstrip("/")


def install_aikey_routes(
    app: FastAPI,
    built: BuiltCore,
    admin: AdminService,
    queries: AdminQueries,
    actor_dep: Callable[..., AdminActor],
    secrets: SecretsService | None,
    http: httpx.Client | None = None,
    price_feed_url: str | None = None,
) -> None:
    Actor = Annotated[AdminActor, Depends(actor_dep)]
    client = http or httpx.Client(timeout=10)
    if price_feed_url is None:  # CAPTAIN_PRICE_FEED_URL="" switches the public list off
        from captain.envfile import current_env

        price_feed_url = current_env().get("CAPTAIN_PRICE_FEED_URL", PRICE_FEED_URL)
    feed = PriceFeed(client, price_feed_url)

    def ask_provider(kind: ProviderKind, base: str, key: str) -> tuple[bool, str, list[str]]:
        """List the provider's models with this key: (works, message, model ids)."""
        if kind is ProviderKind.ANTHROPIC:
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        elif kind is ProviderKind.AZURE_OPENAI:
            headers = {"api-key": key}
        else:
            headers = {"Authorization": f"Bearer {key}"}
        url = f"{base.rstrip('/')}/models"
        if kind is ProviderKind.ANTHROPIC:
            url += "?limit=1000"
        try:
            r = client.get(url, headers=headers)
        except httpx.HTTPError as e:
            return False, f"The provider could not be reached ({type(e).__name__}).", []
        if r.status_code in (401, 403):
            return False, REJECTED, []
        if r.status_code >= 400:
            return False, f"The provider answered with an error ({r.status_code}).", []
        try:
            body = r.json()
            data = body.get("data") or body.get("models") or []
            ids = sorted({str(m.get("id") or m.get("name") or "") for m in data} - {""})
        except (ValueError, AttributeError):
            ids = []
        return True, "The key works.", ids

    def registry() -> Any:
        reg = built.core.m.registry
        if reg is None:
            raise HTTPException(501, {"error": "NOT_CONFIGURED", "message": "no model registry"})
        return reg

    def need() -> SecretsService:
        if secrets is None:
            raise HTTPException(
                501,
                {"error": "NOT_CONFIGURED",
                 "message": "Storing AI keys in Captain is not switched on for this install "
                            "(CAPTAIN_SECRETS_KEY)."},
            )  # fmt: skip
        return secrets

    def record(actor: AdminActor, provider_id: str, action: str) -> None:
        built.core.record(
            admin_event(EventType.AIKEY_CHANGED, actor.id,
                        {"provider_id": provider_id, "action": action})
        )  # fmt: skip

    def stored_id(p: Provider) -> str | None:
        ref = p.credential_ref or ""
        return ref[len(REF_PREFIX) :] if ref.startswith(REF_PREFIX) else None

    @app.get("/admin/v1/ai-keys")
    def list_keys(a: Actor) -> dict[str, Any]:
        require(a.roles, Permission.REGISTER_READ)
        views = {s.id: secret_view(s) for s in secrets.store.all()} if secrets else {}
        items = []
        for doc in queries.registry("provider"):
            ref = str(doc.get("credential_ref") or "")
            sid = ref[len(REF_PREFIX) :] if ref.startswith(REF_PREFIX) else None
            items.append({
                "provider_id": doc.get("id"), "kind": doc.get("kind"),
                "base_url": doc.get("base_url"), "active": doc.get("active", True),
                "region": doc.get("region"),
                "key": views.get(sid) if sid else None,
                "key_source": "captain" if sid else (
                    "none" if not ref or ref.startswith("none:") else "server"),
            })  # fmt: skip
        return {
            "enabled": secrets is not None,
            "items": items,
            "kinds": [k.value for k in ProviderKind if k not in NOT_KEY_BASED],
        }

    @app.post("/admin/v1/ai-keys")
    def put_key(a: Actor, body: AiKeyBody) -> dict[str, Any]:
        require(a.roles, Permission.AIKEYS_WRITE)
        svc = need()
        if body.kind in NOT_KEY_BASED:
            raise HTTPException(
                400, {"error": "NOT_KEY_BASED", "message": "Bedrock uses AWS credentials."}
            )
        url = body.base_url or DEFAULT_URLS.get(body.kind)
        if body.kind in NEEDS_URL and not body.base_url:
            raise HTTPException(
                400, {"error": "BAD_URL", "message": "This provider needs its base URL."}
            )
        url = _check_url(url) if url else None
        reg = registry()
        old = reg.provider(body.provider_id)
        # Check the key with the provider before storing it.
        ok, msg, _ = ask_provider(body.kind, url or "", body.key.get_secret_value())
        if not ok and (msg == REJECTED or not body.skip_check):
            raise HTTPException(400, {"error": "KEY_NOT_ACCEPTED", "message": msg})
        try:
            new = svc.put(body.provider_id, body.key.get_secret_value(), a.id)
        except ValueError as e:
            raise HTTPException(400, {"error": "BAD_KEY", "message": str(e)}) from e
        admin.upsert_registry(
            a,
            Provider(id=body.provider_id, kind=body.kind, region=body.region,
                     credential_ref=REF_PREFIX + new.id, base_url=url, active=True),
        )  # fmt: skip
        if old is not None and (old_id := stored_id(old)):
            svc.delete(old_id)  # the replaced key is destroyed
        record(a, body.provider_id, "replaced" if old is not None else "added")
        return {"provider_id": body.provider_id, "key": secret_view(new)}

    @app.post("/admin/v1/ai-keys/{provider_id}/test")
    def test_key(a: Actor, provider_id: str) -> dict[str, Any]:
        require(a.roles, Permission.AIKEYS_WRITE)
        svc = need()
        p = registry().provider(provider_id)
        if p is None or not (sid := stored_id(p)):
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no stored key"})
        base = p.base_url or DEFAULT_URLS.get(p.kind) or ""
        ok, msg, _ = ask_provider(p.kind, base, svc.reveal(sid))
        return {"ok": ok, "message": msg}

    @app.get("/admin/v1/ai-keys/{provider_id}/models")
    def provider_models(a: Actor, provider_id: str) -> dict[str, Any]:
        """The provider's chat models, fetched live with the stored key, each with a
        suggested list price (to check) and whether Captain already has it."""
        require(a.roles, Permission.REGISTER_READ)
        svc = need()
        p = registry().provider(provider_id)
        if p is None or not (sid := stored_id(p)):
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no stored key"})
        base = p.base_url or DEFAULT_URLS.get(p.kind) or ""
        ok, msg, ids = ask_provider(p.kind, base, svc.reveal(sid))
        if not ok:
            raise HTTPException(502, {"error": "PROVIDER", "message": msg})
        have = {
            str(m.get("provider_model")): str(m.get("alias"))
            for m in queries.registry("model")
            if m.get("provider_id") == provider_id and m.get("active", True)
        }
        out = []
        for mid in ids:
            if not is_chat_model(mid):
                continue
            name = mid.removeprefix("models/")
            found = feed.price(str(p.kind.value), name)
            out.append({
                "id": name,
                "added_as": have.get(name) or have.get(mid),
                "suggested_price": None if found is None
                else {"input_usd_per_m": found[0][0], "output_usd_per_m": found[0][1],
                      "source": found[1]},
            })  # fmt: skip
        return {"provider_id": provider_id, "models": out}

    @app.delete("/admin/v1/ai-keys/{provider_id}")
    def delete_key(a: Actor, provider_id: str, confirm_in_use: bool = False) -> dict[str, Any]:
        """Soft delete: the provider is switched off and its key destroyed; refused while
        models still use it (admin_deletes)."""
        from captain.api.admin_deletes import provider_check, refuse_unless_safe

        require(a.roles, Permission.AIKEYS_WRITE)
        svc = need()
        reg = registry()
        p = reg.provider(provider_id)
        if p is None or not (sid := stored_id(p)):
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no stored key"})
        refuse_unless_safe(provider_check(queries, provider_id), confirm_in_use)
        record(a, provider_id, "removed")  # recorded before it takes effect
        admin.upsert_registry(
            a,
            Provider(id=p.id, kind=p.kind, region=p.region, credential_ref="none:removed",
                     base_url=p.base_url, active=False),
        )  # fmt: skip
        svc.delete(sid)
        return {"provider_id": provider_id, "status": "removed"}
