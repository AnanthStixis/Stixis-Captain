"""Composition root for the MCP Proxy process:

    uvicorn --factory captain.api.mcp_main:create_mcp_app_from_env --port 8300

Builds the PostgreSQL-backed Core from settings and an upstream MCP client whose
credentials come from the environment (a vault adapter in production).

    CAPTAIN_MCP_ALLOWED_ORIGINS   comma list of browser origins allowed to call /mcp
                                  (default none: only non-browser clients)
    CAPTAIN_MCP_ALLOW_LOOPBACK    "1" lets upstream servers be plain-http on this machine
                                  (development only; default off)
    CAPTAIN_MCP_ALLOW_PRIVATE     "0" refuses upstream servers on private networks
                                  (default on: enterprise MCP servers are usually internal)
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from captain.config import build_from_env
from captain.envfile import DEFAULT_ENV_FILE, dotenv_values
from captain.integrations.api_gateway.provider import EnvSecretResolver
from captain.integrations.mcp_proxy.app import create_mcp_app
from captain.integrations.mcp_proxy.upstream import McpUpstream


def create_mcp_app_from_env() -> FastAPI:
    built, _ = build_from_env()
    env = {**dotenv_values(DEFAULT_ENV_FILE), **os.environ}
    upstream = McpUpstream(
        EnvSecretResolver(env),
        allow_loopback=env.get("CAPTAIN_MCP_ALLOW_LOOPBACK") == "1",
        allow_private=env.get("CAPTAIN_MCP_ALLOW_PRIVATE", "1") != "0",
    )
    origins = frozenset(
        o.strip() for o in env.get("CAPTAIN_MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
    )
    return create_mcp_app(built.core, upstream, origins)
