"""Model/Tool Registry: the catalogue of what may be called.

- Providers: who serves models (OpenAI, Anthropic, Gemini, Azure OpenAI, Bedrock,
  self-hosted), region, and whether they are enabled. Credentials are referenced by
  secret name only; the registry never holds a provider key.
- Models: a Captain alias (e.g. "claude-sonnet") mapped to a provider's model id,
  with capabilities and region, so applications never hard-code provider names.
- Routes: alias → ordered targets (primary, then fallbacks). The gateway tries them in
  order; a disabled provider or model is skipped.
- MCP servers and tools: each tool's owner server, impact, risk level, read-only flag
  and a JSON Schema for its arguments. The MCP Proxy and the policy use these.

The AI Register still decides WHICH models/tools a system may use (allow-lists);
the registry decides WHAT exists and HOW to reach it. Both must agree for a call.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


class ProviderKind(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    AZURE_OPENAI = "azure_openai"
    BEDROCK = "bedrock"
    MISTRAL = "mistral"
    COHERE = "cohere"
    SELF_HOSTED = "self_hosted"


class ToolImpact(StrEnum):
    LOW = "low"
    HIGH = "high"


class ToolRisk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    kind: ProviderKind
    region: str
    credential_ref: str  # name of the secret in the client's vault, never the secret
    base_url: str | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class ModelEntry:
    alias: str
    provider_id: str
    provider_model: str  # the provider's own model id
    region: str
    capabilities: frozenset[str] = frozenset({"chat"})  # chat, streaming, tools, embeddings, vision
    max_output_tokens: int | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class Route:
    alias: str
    targets: tuple[str, ...]  # model aliases in order: primary, then fallbacks
    max_retries: int = 1


@dataclass(frozen=True, slots=True)
class McpServer:
    id: str
    url: str
    owner_id: str
    transport: str = "streamable_http"
    credential_ref: str | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class ToolEntry:
    name: str  # e.g. "jira.create_issue"
    server_id: str
    description: str
    impact: ToolImpact
    risk: ToolRisk
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    read_only: bool = False
    active: bool = True
    upstream_name: str | None = None  # name on the MCP server; defaults to the part after "."


_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


class RegistryError(ValueError):
    pass


class RegistryStore(Protocol):
    def put_provider(self, p: Provider) -> None: ...
    def provider(self, id: str) -> Provider | None: ...
    def put_model(self, m: ModelEntry) -> None: ...
    def model(self, alias: str) -> ModelEntry | None: ...
    def put_route(self, r: Route) -> None: ...
    def route(self, alias: str) -> Route | None: ...
    def put_server(self, s: McpServer) -> None: ...
    def server(self, id: str) -> McpServer | None: ...
    def put_tool(self, t: ToolEntry) -> None: ...
    def tool(self, name: str) -> ToolEntry | None: ...
    def tools(self) -> list[ToolEntry]: ...


class InMemoryRegistryStore:
    def __init__(self) -> None:
        self._d: dict[str, dict[str, Any]] = {k: {} for k in ("p", "m", "r", "s", "t")}
        self._lock = threading.Lock()

    def _put(self, kind: str, key: str, v: Any) -> None:
        with self._lock:
            self._d[kind][key] = v

    def put_provider(self, p: Provider) -> None:
        self._put("p", p.id, p)

    def provider(self, id: str) -> Provider | None:
        return self._d["p"].get(id)

    def put_model(self, m: ModelEntry) -> None:
        self._put("m", m.alias, m)

    def model(self, alias: str) -> ModelEntry | None:
        return self._d["m"].get(alias)

    def put_route(self, r: Route) -> None:
        self._put("r", r.alias, r)

    def route(self, alias: str) -> Route | None:
        return self._d["r"].get(alias)

    def put_server(self, s: McpServer) -> None:
        self._put("s", s.id, s)

    def server(self, id: str) -> McpServer | None:
        return self._d["s"].get(id)

    def put_tool(self, t: ToolEntry) -> None:
        self._put("t", t.name, t)

    def tool(self, name: str) -> ToolEntry | None:
        return self._d["t"].get(name)

    def tools(self) -> list[ToolEntry]:
        return list(self._d["t"].values())


@dataclass(frozen=True, slots=True)
class RouteTarget:
    alias: str
    provider: Provider
    model: ModelEntry


class RegistryService:
    def __init__(self, store: RegistryStore) -> None:
        self._s = store

    # --------------------------------------------------------------- reads
    def provider(self, id: str) -> Provider | None:
        return self._s.provider(id)

    def model(self, alias: str) -> ModelEntry | None:
        """The model entry as stored, switched on or off."""
        return self._s.model(alias)

    # -------------------------------------------------------------- writes
    def upsert_provider(self, p: Provider) -> None:
        self._s.put_provider(p)

    def upsert_model(self, m: ModelEntry) -> None:
        if self._s.provider(m.provider_id) is None:
            raise RegistryError(f"unknown provider {m.provider_id}")
        self._s.put_model(m)

    def upsert_route(self, r: Route) -> None:
        if not r.targets:
            raise RegistryError("a route needs at least one target")
        for a in r.targets:
            if self._s.model(a) is None:
                raise RegistryError(f"route target {a} is not a registered model")
        self._s.put_route(r)

    def upsert_server(self, s: McpServer) -> None:
        self._s.put_server(s)

    def upsert_tool(self, t: ToolEntry) -> None:
        if self._s.server(t.server_id) is None:
            raise RegistryError(f"unknown MCP server {t.server_id}")
        for label, value in (("name", t.name), ("upstream name", t.upstream_name)):
            if value is not None and not _TOOL_NAME.fullmatch(value):
                raise RegistryError(f"tool {label} must match {_TOOL_NAME.pattern}")
        try:
            Draft202012Validator.check_schema(t.input_schema)
        except SchemaError as e:
            raise RegistryError(f"invalid input schema for {t.name}: {e.message}") from e
        self._s.put_tool(t)

    # --------------------------------------------------------------- reads
    def is_callable(self, alias: str) -> bool:
        """An alias can be called if it is an enabled model, or a route with at least
        one enabled target."""
        return bool(self.resolve_route(alias))

    def usable_model(self, alias: str) -> ModelEntry | None:
        m = self._s.model(alias)
        if m is None or not m.active:
            return None
        p = self._s.provider(m.provider_id)
        return m if p is not None and p.active else None

    def resolve_route(self, alias: str) -> list[RouteTarget]:
        """Ordered usable targets for an alias (an alias without a route is its own
        single-target route). Disabled models/providers are skipped."""
        r = self._s.route(alias)
        out = []
        for a in r.targets if r else (alias,):
            m = self.usable_model(a)
            if m is not None:
                p = self._s.provider(m.provider_id)
                assert p is not None
                out.append(RouteTarget(a, p, m))
        return out

    def usable_tool(self, name: str) -> ToolEntry | None:
        t = self._s.tool(name)
        if t is None or not t.active:
            return None
        s = self._s.server(t.server_id)
        return t if s is not None and s.active else None

    def server(self, id: str) -> McpServer | None:
        return self._s.server(id)

    def all_tools(self) -> list[ToolEntry]:
        return self._s.tools()

    def validate_arguments(self, tool: ToolEntry, args: dict[str, object] | None) -> list[str]:
        """Return human-readable schema violations (empty list = valid). Messages name
        the failing field and rule, never echo the submitted value."""
        v = Draft202012Validator(tool.input_schema)
        return sorted(
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.validator} violated"
            for e in v.iter_errors(args or {})
        )
