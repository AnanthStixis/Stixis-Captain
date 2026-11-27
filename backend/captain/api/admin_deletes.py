"""Admin API: safe removal of AI keys and models.

    GET    /admin/v1/ai-keys/{provider_id}/delete-check   what still depends on the key
    GET    /admin/v1/registry/model/{alias}/delete-check  what still depends on the model
    DELETE /admin/v1/registry/model/{alias}               switch the model off (soft)
    GET    /admin/v1/registry/server/{id}/delete-check    MCP server: its tools still on
    DELETE /admin/v1/registry/server/{id}                 switch the server off (soft)
    DELETE /admin/v1/registry/tool/{name}                 switch the tool off (soft)
    GET    /admin/v1/register/{id}/delete-check           AI use: live Captain keys under it
    DELETE /admin/v1/register/{id}                        retire the AI use (soft)
    GET    /admin/v1/budgets/delete-check?scope=          the last limit that stops calls?
    DELETE /admin/v1/budgets?scope=                       remove a spending limit
    GET    /admin/v1/users/{id}/delete-check              before deactivating a person

Removal is always soft: the entry is switched off, never erased, so past activity, costs
and the evidence ledger keep pointing at something real. Before that:
  * blockers  - live configuration that would break (models still using a key, AI uses
                still allowing a model, other routes falling back to it). Remove those
                first; the delete is refused (409) while any is left.
  * recent use - calls in the last 30 days. Not a blocker, but the admin must confirm
                (`confirm_in_use=true`), because people are actively using it.
The AI-key DELETE itself lives in admin_aikeys and runs the same check first.
"""

# No `from __future__ import annotations`: FastAPI must resolve the local `Actor` alias.
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query

from captain.api.admin_queries import AdminQueries
from captain.core.admin import AdminActor, AdminService
from captain.core.factory import BuiltCore
from captain.core.identity.rbac import Permission, require

RECENT_DAYS = 30


def _calls_by_model(queries: AdminQueries) -> dict[str, int]:
    end = datetime.now(UTC)
    try:
        rows = queries.usage_by("model", end - timedelta(days=RECENT_DAYS), end)
    except Exception:  # noqa: BLE001 (usage figures are advisory; never block on them)
        return {}
    return {str(r[0]): int(r[1]) for r in rows if r[0]}


def model_check(queries: AdminQueries, alias: str) -> dict[str, Any]:
    uses = [e.name for e in queries.register_entries() if e.active and alias in e.allowed_models]
    routes = [
        str(r.get("alias"))
        for r in queries.registry("route")
        if alias in (r.get("targets") or []) and r.get("alias") != alias
    ]
    blockers = [f"AI use “{u}” allows it" for u in uses] + [
        f"model “{r}” falls back to it" for r in routes
    ]
    calls = _calls_by_model(queries).get(alias, 0)
    return {
        "name": alias,
        "blockers": blockers,
        "calls_recent": calls,
        "recent_days": RECENT_DAYS,
        "can_delete": not blockers,
    }


def provider_check(queries: AdminQueries, provider_id: str) -> dict[str, Any]:
    models = [
        str(m.get("alias"))
        for m in queries.registry("model")
        if m.get("provider_id") == provider_id and m.get("active", True)
    ]
    by_model = _calls_by_model(queries)
    return {
        "name": provider_id,
        "blockers": [f"model “{m}” uses this key" for m in models],
        "calls_recent": sum(by_model.get(m, 0) for m in models),
        "recent_days": RECENT_DAYS,
        "can_delete": not models,
    }


def _calls_by_principal(queries: AdminQueries) -> dict[str, int]:
    end = datetime.now(UTC)
    try:
        rows = queries.usage_by("principal", end - timedelta(days=RECENT_DAYS), end)
    except Exception:  # noqa: BLE001
        return {}
    return {str(r[0]): int(r[1]) for r in rows if r[0]}


def _check(name: str, blockers: list[str], calls: int, **extra: Any) -> dict[str, Any]:
    return {"name": name, "blockers": blockers, "calls_recent": calls,
            "recent_days": RECENT_DAYS, "can_delete": not blockers, **extra}  # fmt: skip


def server_check(queries: AdminQueries, server_id: str) -> dict[str, Any]:
    tools = [
        str(t.get("name"))
        for t in queries.registry("tool")
        if t.get("server_id") == server_id and t.get("active", True)
    ]
    return _check(server_id, [f"tool “{t}” is on this server" for t in tools], 0)


def register_check(queries: AdminQueries, register_id: str) -> dict[str, Any]:
    keys = [k for k in queries.keys() if k.register_id == register_id
            and str(getattr(k.status, "value", k.status)) == "ACTIVE"]  # fmt: skip
    name = next((e.name for e in queries.register_entries() if e.id == register_id), register_id)
    return _check(
        name,
        [f"{len(keys)} active Captain key(s) belong to it: revoke them on Captain keys"]
        if keys
        else [],
        0,
    )


def budget_check(queries: AdminQueries, budgets: Any, scope: str) -> dict[str, Any]:
    defs = queries.budget_definitions()
    me = next((d for d in defs if d.scope == scope), None)
    blockers = []
    if me is not None and me.hard and not any(d.hard for d in defs if d.scope != scope):
        blockers.append(
            "it is the only spending limit that stops AI calls; every call needs one, so all "
            "AI calls would be refused. Add another limit first"
        )
    spent = budgets.state(me.bucket()).spent if me is not None else 0
    return _check(scope, blockers, 0, spent_micros=spent)


def user_check(queries: AdminQueries, user_id: str, admins: set[str]) -> dict[str, Any]:
    uid = user_id.strip().lower()
    owns = [e.name for e in queries.register_entries() if e.active and e.owner_id == uid]
    blockers = [f"owner of the AI use “{n}”: pick another owner first" for n in owns]
    if uid in admins and len(admins) == 1:
        blockers.append("the only person who can manage users; give someone else that role")
    keys = [k for k in queries.keys(uid) if str(getattr(k.status, "value", k.status)) == "ACTIVE"]
    return _check(uid, blockers, _calls_by_principal(queries).get(uid, 0),
                  active_keys=len(keys))  # fmt: skip


def user_admins(queries: AdminQueries) -> set[str]:
    """Active people who can manage users (so one always remains)."""
    from captain.core.identity.rbac import permissions_for

    return {
        p.id for p in queries.principals()
        if str(p.type) == "USER" and p.active
        and Permission.USERS_WRITE in permissions_for(frozenset(p.roles or ()))
    }  # fmt: skip


def refuse_unless_safe(check: dict[str, Any], confirm_in_use: bool) -> None:
    if check["blockers"]:
        raise HTTPException(409, {
            "error": "IN_USE",
            "message": f"{check['name']} is still needed: " + "; ".join(check["blockers"])
            + ". Remove those first.",
            "blockers": check["blockers"],
        })  # fmt: skip
    if check["calls_recent"] and not confirm_in_use:
        raise HTTPException(409, {
            "error": "RECENTLY_USED",
            "message": f"{check['name']} handled {check['calls_recent']} AI calls in the last "
            f"{check['recent_days']} days. Confirm to switch it off anyway.",
        })  # fmt: skip


def install_delete_routes(
    app: FastAPI,
    built: BuiltCore,
    admin: AdminService,
    queries: AdminQueries,
    actor_dep: Callable[..., AdminActor],
) -> None:
    Actor = Annotated[AdminActor, Depends(actor_dep)]

    @app.get("/admin/v1/ai-keys/{provider_id}/delete-check")
    def ai_key_check(a: Actor, provider_id: str) -> dict[str, Any]:
        require(a.roles, Permission.REGISTER_READ)
        return provider_check(queries, provider_id)

    @app.get("/admin/v1/registry/model/{alias}/delete-check")
    def model_delete_check(a: Actor, alias: str) -> dict[str, Any]:
        require(a.roles, Permission.REGISTER_READ)
        return model_check(queries, alias)

    @app.delete("/admin/v1/registry/model/{alias}")
    def delete_model(a: Actor, alias: str, confirm_in_use: bool = False) -> dict[str, str]:
        require(a.roles, Permission.REGISTER_WRITE)
        reg = built.core.m.registry
        m = reg.model(alias) if reg else None
        if m is None:
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no such model"})
        refuse_unless_safe(model_check(queries, alias), confirm_in_use)
        admin.upsert_registry(a, replace(m, active=False))  # soft: history stays linked
        return {"status": "switched_off", "model": alias}

    @app.get("/admin/v1/registry/server/{server_id}/delete-check")
    def server_delete_check(a: Actor, server_id: str) -> dict[str, Any]:
        require(a.roles, Permission.REGISTER_READ)
        return server_check(queries, server_id)

    @app.delete("/admin/v1/registry/server/{server_id}")
    def delete_server(a: Actor, server_id: str) -> dict[str, str]:
        require(a.roles, Permission.REGISTER_WRITE)
        reg = built.core.m.registry
        srv = reg.server(server_id) if reg else None
        if srv is None:
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no such MCP server"})
        refuse_unless_safe(server_check(queries, server_id), True)
        admin.upsert_registry(a, replace(srv, active=False))
        return {"status": "switched_off", "server": server_id}

    @app.get("/admin/v1/registry/tool/{name}/delete-check")
    def tool_delete_check(a: Actor, name: str) -> dict[str, Any]:
        require(a.roles, Permission.REGISTER_READ)
        return _check(name, [], 0)  # nothing is configured on top of a single tool

    @app.delete("/admin/v1/registry/tool/{name}")
    def delete_tool(a: Actor, name: str) -> dict[str, str]:
        require(a.roles, Permission.REGISTER_WRITE)
        tool = next((t for t in (built.core.m.registry.all_tools() if built.core.m.registry
                                 else []) if t.name == name), None)  # fmt: skip
        if tool is None:
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no such tool"})
        admin.upsert_registry(a, replace(tool, active=False))
        return {"status": "switched_off", "tool": name}

    @app.get("/admin/v1/register/{register_id}/delete-check")
    def register_delete_check(a: Actor, register_id: str) -> dict[str, Any]:
        require(a.roles, Permission.REGISTER_READ)
        return register_check(queries, register_id)

    @app.delete("/admin/v1/register/{register_id}")
    def retire_register(a: Actor, register_id: str) -> dict[str, str]:
        entry = next((e for e in queries.register_entries() if e.id == register_id), None)
        if entry is None:
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no such AI use"})
        refuse_unless_safe(register_check(queries, register_id), True)
        admin.upsert_register(a, entry.model_copy(update={"active": False}))
        return {"status": "retired", "id": register_id}

    @app.get("/admin/v1/budgets/delete-check")
    def budget_delete_check(a: Actor, scope: str = Query(max_length=256)) -> dict[str, Any]:
        require(a.roles, Permission.BUDGET_READ)
        return budget_check(queries, built.core.m.budget, scope)

    @app.delete("/admin/v1/budgets")
    def delete_budget(a: Actor, scope: str = Query(max_length=256)) -> dict[str, str]:
        require(a.roles, Permission.BUDGET_WRITE)
        refuse_unless_safe(budget_check(queries, built.core.m.budget, scope), True)
        if not admin.remove_budget(a, scope):
            raise HTTPException(404, {"error": "NOT_FOUND", "message": "no budget for scope"})
        return {"status": "removed", "scope": scope}

    @app.get("/admin/v1/users/{user_id}/delete-check")
    def user_delete_check(a: Actor, user_id: str) -> dict[str, Any]:
        require(a.roles, Permission.KEYS_READ)
        return user_check(queries, user_id, user_admins(queries))
