"""Composition root for the API Gateway process:

    uvicorn --factory captain.api.gateway_main:create_gateway_app_from_env --port 8000

Builds the PostgreSQL-backed Core from settings, a provider client that resolves
credentials from the environment (vault adapter in production), and a shared
idempotency store so several gateway replicas behave as one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import create_engine

from captain.api.state import PgStateStore
from captain.config import build_from_env
from captain.envfile import DEFAULT_ENV_FILE, dotenv_values
from captain.integrations.api_gateway.app import create_gateway_app
from captain.integrations.api_gateway.provider import EnvSecretResolver, OpenAICompatibleClient


def create_gateway_app_from_env() -> FastAPI:
    built, settings = build_from_env()
    engine = create_engine(settings.database_url, pool_size=10, pool_pre_ping=True)
    env = {**dotenv_values(DEFAULT_ENV_FILE), **os.environ}
    # Host names that ARE Captain (an MCP server configured on one of these is not a bypass).
    hosts = tuple(h.strip().lower() for h in env.get("CAPTAIN_PUBLIC_HOSTS", "").split(",") if h)
    # Desktop agent: Windows user / UPN -> Captain principal (JSON file). Unmapped users are
    # recorded as a short hash, never by name.
    actor_map = env.get("CAPTAIN_DESKTOP_ACTOR_MAP")
    actors = json.loads(Path(actor_map).read_text(encoding="utf-8")) if actor_map else None
    return create_gateway_app(
        built.core,
        # Real environment wins over backend/.env (development convenience only).
        OpenAICompatibleClient(
            EnvSecretResolver({**dotenv_values(DEFAULT_ENV_FILE), **os.environ})
        ),
        PgStateStore(engine, ttl_s=24 * 3600),
        captain_hosts=hosts,
        desktop_actors=actors,
    )
