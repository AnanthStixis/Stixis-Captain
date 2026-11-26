"""PostgreSQL store for the Model/Tool Registry (one row per item, JSON document)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from captain.core.registry.service import (
    McpServer,
    ModelEntry,
    Provider,
    ProviderKind,
    Route,
    ToolEntry,
    ToolImpact,
    ToolRisk,
)
from captain.db import schema as s


def _doc(v: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in v.__dataclass_fields__:
        x = getattr(v, f)
        out[f] = sorted(x) if isinstance(x, frozenset) else list(x) if isinstance(x, tuple) else x
    return out


def _obj(kind: str, d: dict[str, Any]) -> Any:
    if kind == "provider":
        return Provider(**{**d, "kind": ProviderKind(d["kind"])})
    if kind == "model":
        return ModelEntry(**{**d, "capabilities": frozenset(d["capabilities"])})
    if kind == "route":
        return Route(**{**d, "targets": tuple(d["targets"])})
    if kind == "server":
        return McpServer(**d)
    return ToolEntry(**{**d, "impact": ToolImpact(d["impact"]), "risk": ToolRisk(d["risk"])})


class PgRegistryStore:
    def __init__(self, engine: Engine) -> None:
        self._e = engine

    def _put(self, kind: str, key: str, v: Any) -> None:
        doc = _doc(v)
        stmt = pg_insert(s.registry_items).values(
            kind=kind,
            key=key,
            doc=doc,
            active=bool(doc.get("active", True)),
            updated_at=datetime.now(UTC),
        )
        with self._e.begin() as c:
            c.execute(
                stmt.on_conflict_do_update(
                    index_elements=["kind", "key"],
                    set_={
                        "doc": stmt.excluded.doc,
                        "active": stmt.excluded.active,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
            )

    def _get(self, kind: str, key: str) -> Any:
        r = s.registry_items
        with self._e.connect() as c:
            row = c.execute(select(r.c.doc).where(and_(r.c.kind == kind, r.c.key == key))).first()
        return _obj(kind, row.doc) if row else None

    def put_provider(self, p: Provider) -> None:
        self._put("provider", p.id, p)

    def provider(self, id: str) -> Provider | None:
        v: Provider | None = self._get("provider", id)
        return v

    def put_model(self, m: ModelEntry) -> None:
        self._put("model", m.alias, m)

    def model(self, alias: str) -> ModelEntry | None:
        v: ModelEntry | None = self._get("model", alias)
        return v

    def put_route(self, rt: Route) -> None:
        self._put("route", rt.alias, rt)

    def route(self, alias: str) -> Route | None:
        v: Route | None = self._get("route", alias)
        return v

    def put_server(self, sv: McpServer) -> None:
        self._put("server", sv.id, sv)

    def server(self, id: str) -> McpServer | None:
        v: McpServer | None = self._get("server", id)
        return v

    def put_tool(self, t: ToolEntry) -> None:
        self._put("tool", t.name, t)

    def tool(self, name: str) -> ToolEntry | None:
        v: ToolEntry | None = self._get("tool", name)
        return v

    def tools(self) -> list[ToolEntry]:
        r = s.registry_items
        with self._e.connect() as c:
            rows = c.execute(select(r.c.doc).where(r.c.kind == "tool"))
            return [_obj("tool", row.doc) for row in rows]
