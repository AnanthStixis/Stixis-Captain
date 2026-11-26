"""Composition root for the API Gateway process:

    uvicorn --factory captain.api.gateway_main:create_gateway_app_from_env --port 8000

Builds the PostgreSQL-backed Core from settings, a provider client that resolves
credentials from the environment (vault adapter in production), and a shared
idempotency store so several gateway replicas behave as one.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import create_engine

from captain.api.state import PgStateStore
from captain.config import build_from_env, build_secrets
from captain.envfile import current_env
from captain.integrations.api_gateway.app import create_gateway_app
from captain.integrations.api_gateway.provider import (
    ChainSecretResolver,
    EnvSecretResolver,
    OpenAICompatibleClient,
    SecretResolver,
    StoredSecretResolver,
)


def create_gateway_app_from_env() -> FastAPI:
    built, settings = build_from_env()
    engine = create_engine(
        settings.database_url,
        pool_size=int(current_env().get("CAPTAIN_DB_POOL_SIZE", "10")),
        pool_pre_ping=True,
    )
    env = current_env()
    # Host names that ARE Captain (an MCP server configured on one of these is not a bypass).
    hosts = tuple(h.strip().lower() for h in env.get("CAPTAIN_PUBLIC_HOSTS", "").split(",") if h)
    # Desktop agent: Windows user / UPN -> Captain principal (JSON file). Unmapped users are
    # recorded as a short hash, never by name.
    actor_map = env.get("CAPTAIN_DESKTOP_ACTOR_MAP")
    actors = json.loads(Path(actor_map).read_text(encoding="utf-8")) if actor_map else None
    return create_gateway_app(
        built.core,
        # Real environment wins over backend/.env (development convenience only).
        OpenAICompatibleClient(secret_resolver(env, build_secrets(settings, engine))),
        PgStateStore(engine, ttl_s=24 * 3600),
        captain_hosts=hosts,
        desktop_actors=actors,
    )


def secret_resolver(env: dict[str, str], secrets: object | None) -> SecretResolver:
    """`env:NAME` from the environment (real env wins over backend/.env); `captain:<id>`
    from the AI keys stored encrypted in Captain, when that is switched on."""
    by_kind: dict[str, SecretResolver] = {"env": EnvSecretResolver(env)}
    if secrets is not None:
        by_kind["captain"] = StoredSecretResolver(secrets)
    return ChainSecretResolver(by_kind)
