"""MCP Proxy end to end: CaptainCore (in-memory) + a fake upstream tool server.
No real MCP server is called. The upstream client has its own tests in test_mcp_upstream."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from captain.core.decision_api import McpCoreAPI
from captain.core.guardrails.service import GuardrailService
from captain.core.identity.killswitch import KillScope
from captain.core.ledger.service import InMemoryLedgerStore
from captain.core.models import (
    AutonomyLevel,
    ContextHints,
    Credential,
    PrincipalType,
    RiskTier,
    ToolTarget,
)
from captain.core.registry.service import (
    InMemoryRegistryStore,
    McpServer,
    RegistryService,
    ToolEntry,
    ToolImpact,
    ToolRisk,
)
from captain.integrations.mcp_proxy.app import APPROVAL_META, create_mcp_app
from captain.integrations.mcp_proxy.upstream import UpstreamError, UpstreamResult
from tests.conftest import make_context
from tests.harness.adapters import CaptainCoreHarness
from tests.unit.test_engine import Guard

SCHEMA_SEARCH = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}
ALLOWED = frozenset({"demo.search_issues", "demo.create_issue", "demo.retired"})


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = UpstreamResult([{"type": "text", "text": "ok"}], False)
        self.error: Exception | None = None

    def call_tool(self, target: ToolTarget, arguments: dict[str, Any]) -> UpstreamResult:
        self.calls.append((target.upstream_name, arguments))
        if self.error:
            raise self.error
        return self.result


def registry() -> RegistryService:
    r = RegistryService(InMemoryRegistryStore())
    r.upsert_server(
        McpServer("demo-mcp", "https://mcp.example.com/mcp", "owner-1", "streamable_http", "env:K")
    )
    r.upsert_tool(
        ToolEntry(
            "demo.search_issues",
            "demo-mcp",
            "Search issues",
            ToolImpact.LOW,
            ToolRisk.LOW,
            SCHEMA_SEARCH,
            read_only=True,
        )  # fmt: skip
    )
    r.upsert_tool(
        ToolEntry("demo.create_issue", "demo-mcp", "Create", ToolImpact.HIGH, ToolRisk.HIGH)
    )
    r.upsert_tool(
        ToolEntry("demo.admin_reset", "demo-mcp", "Reset", ToolImpact.HIGH, ToolRisk.CRITICAL)
    )
    r.upsert_tool(
        ToolEntry("demo.retired", "demo-mcp", "Old", ToolImpact.LOW, ToolRisk.LOW, active=False)
    )
    return r


class Rig:
    def __init__(self, tier: RiskTier, autonomy: AutonomyLevel) -> None:
        self.h = CaptainCoreHarness()
        m = self.h.core.m
        m.registry = registry()
        m.guardrails = GuardrailService(Guard())
        self.up = FakeUpstream()
        self.c = TestClient(create_mcp_app(self.h.core, self.up))
        self.hdr = self.add("k", "agent-1", tier, autonomy)

    def add(self, secret: str, pid: str, tier: RiskTier, autonomy: AutonomyLevel) -> dict[str, str]:
        ctx = make_context(pid, tier, autonomy, PrincipalType.AGENT)
        ctx = ctx.model_copy(
            update={
                "register_entry": ctx.register_entry.model_copy(update={"allowed_tools": ALLOWED})
            }
        )
        self.h.register_key(secret, ctx)
        return {"Authorization": f"Bearer {self.h._keys[secret]}"}  # noqa: SLF001

    def rpc(self, method: str, params: dict[str, Any] | None = None, hdr: Any = None) -> Any:
        return self.c.post(
            "/mcp",
            headers=hdr or self.hdr,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        )

    def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        ticket: str | None = None,
        hdr: Any = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": args if args is not None else {}}
        if ticket:
            params["_meta"] = {APPROVAL_META: ticket}
        body = self.rpc("tools/call", params, hdr).json()
        assert "result" in body, body
        res: dict[str, Any] = body["result"]
        return res

    def ledger(self) -> list[tuple[str, dict[str, Any]]]:
        store = self.h.built.infra.ledger_store
        assert isinstance(store, InMemoryLedgerStore)
        return [(t, b) for (_p, t, b) in store.outbox.values()]


def rig(tier: RiskTier = RiskTier.TIER_3, autonomy: AutonomyLevel = AutonomyLevel.A2) -> Rig:
    return Rig(tier, autonomy)


def text(res: dict[str, Any]) -> str:
    return str(res["content"][0]["text"])


# ------------------------------------------------------------------ protocol


def test_core_satisfies_mcp_contract() -> None:
    assert isinstance(rig().h.core, McpCoreAPI)


def test_requires_credential_and_checks_origin() -> None:
    r = rig()
    resp = r.c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 401 and "Bearer" in resp.headers["www-authenticate"]
    resp = r.c.post(
        "/mcp",
        headers={"Authorization": "Bearer cpt_live_nope"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert resp.status_code == 401
    resp = r.c.post(
        "/mcp",
        headers={**r.hdr, "Origin": "https://evil.example"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert resp.status_code == 403


def test_serves_both_protocol_eras_statelessly() -> None:
    r = rig()
    legacy = r.rpc("initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert legacy["protocolVersion"] == "2025-06-18" and "tools" in legacy["capabilities"]
    assert "mcp-session-id" not in r.rpc("initialize", {}).headers
    modern = r.rpc("server/discover").json()["result"]
    assert "2026-07-28" in modern["supportedVersions"]
    note = r.c.post(
        "/mcp", headers=r.hdr, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert note.status_code == 202
    assert r.rpc("nope").json()["error"]["code"] == -32601


def test_rejects_batches_bad_json_and_get() -> None:
    r = rig()
    batch = r.c.post("/mcp", headers=r.hdr, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert batch.status_code == 400 and batch.json()["error"]["code"] == -32600
    bad = r.c.post("/mcp", headers=r.hdr, content=b"{not json")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32700
    assert r.c.get("/mcp", headers=r.hdr).status_code == 405


def test_tools_list_is_allowed_and_enabled_only() -> None:
    r = rig()
    listed = r.rpc("tools/list").json()["result"]["tools"]
    # Clients see names with only [a-z0-9_-] (VS Code rejects dots); the title keeps the
    # registry name.
    assert {t["name"] for t in listed} == {"demo__create_issue", "demo__search_issues"}
    tools = {t["title"]: t for t in listed}
    # admin_reset is registered but not allowed for this agent; retired is disabled.
    assert set(tools) == {"demo.create_issue", "demo.search_issues"}
    assert tools["demo.search_issues"]["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
    }
    assert tools["demo.create_issue"]["annotations"]["destructiveHint"] is True
    assert tools["demo.search_issues"]["inputSchema"] == SCHEMA_SEARCH


# --------------------------------------------------------------------- calls


def test_allowed_call_is_forwarded_and_recorded_without_content() -> None:
    r = rig()
    res = r.call("demo.search_issues", {"query": "very-private-term"})
    assert res["isError"] is False and text(res) == "ok"
    assert r.up.calls == [("search_issues", {"query": "very-private-term"})]  # upstream name
    events = r.ledger()
    executed = [b for t, b in events if t == "tool.executed"]
    assert executed and executed[0]["details"]["outcome"] == "ok"
    assert executed[0]["tool"] == "demo.search_issues"
    assert "very-private-term" not in json.dumps(events)


def test_client_safe_name_is_mapped_back_to_the_registry_name() -> None:
    r = rig()
    res = r.call("demo__search_issues", {"query": "x"})
    assert res["isError"] is False and r.up.calls == [("search_issues", {"query": "x"})]
    executed = [b for t, b in r.ledger() if t == "tool.executed"]
    assert executed[0]["tool"] == "demo.search_issues"  # evidence keeps the real name
    # A tool the caller may not use stays refused under either spelling.
    for name in ("demo__admin_reset", "demo.admin_reset"):
        assert r.call(name, {})["isError"] is True
    assert r.up.calls == [("search_issues", {"query": "x"})]


def test_refusals_never_reach_the_upstream() -> None:
    r = rig()
    for name, args, code in [
        ("demo.admin_reset", {}, "TOOL_NOT_ALLOWED"),
        ("demo.nonexistent", {}, "TOOL_NOT_REGISTERED"),
        ("demo.retired", {}, "TOOL_NOT_REGISTERED"),
        ("demo.search_issues", {"query": 5}, "TOOL_ARGUMENTS_INVALID"),
        ("demo.search_issues", {}, "TOOL_ARGUMENTS_INVALID"),
    ]:
        res = r.call(name, args)
        assert res["isError"] is True and res["_meta"]["com.stixis.captain/reason"] == code, name
    assert r.up.calls == []
    assert (
        r.rpc("tools/call", {"name": "bad name!", "arguments": {}}).json()["error"]["code"]
        == -32602
    )
    bad_args = r.rpc("tools/call", {"name": "demo.search_issues", "arguments": []})
    assert bad_args.json()["error"]["code"] == -32602


def test_kill_switch_on_tool_and_on_server() -> None:
    r = rig()
    r.h.core.kill(KillScope.TOOL, "demo.search_issues", "test", "admin")
    res = r.call("demo.search_issues", {"query": "x"})
    assert res["_meta"]["com.stixis.captain/reason"] == "KILL_SWITCH_ACTIVE"
    r.h.core.kill(KillScope.MCP_SERVER, "demo-mcp", "server down", "admin")
    res = r.call("demo.create_issue", {})
    assert res["_meta"]["com.stixis.captain/reason"] == "KILL_SWITCH_ACTIVE"
    assert r.up.calls == []


def test_upstream_failure_is_generic_and_recorded() -> None:
    r = rig()
    r.up.error = UpstreamError("upstream HTTP 500 at https://internal.corp/secret-path")
    res = r.call("demo.search_issues", {"query": "x"})
    assert res["isError"] is True and "internal.corp" not in text(res)
    outcomes = [b["details"]["outcome"] for t, b in r.ledger() if t == "tool.executed"]
    assert outcomes == ["upstream_error"]


# ----------------------------------------------------------- output screening


def test_tier2_withholds_secret_and_masks_pii_in_results() -> None:
    r = rig(RiskTier.TIER_2)
    r.up.result = UpstreamResult([{"type": "text", "text": "key AKIAIOSFODNN7EXAMPLE"}], False)
    res = r.call("demo.search_issues", {"query": "x"})
    assert res["isError"] is True and "AKIA" not in json.dumps(res)
    r.up.result = UpstreamResult([{"type": "text", "text": "mail ravi@example.com now"}], False)
    res = r.call("demo.search_issues", {"query": "x"})
    assert "ravi@example.com" not in text(res) and res["isError"] is False
    executed = [b["details"] for t, b in r.ledger() if t == "tool.executed"]
    assert executed[0]["outcome"] == "output_blocked" and executed[1]["output_masked"] is True


def test_tier3_passes_secret_through_but_records_finding() -> None:
    r = rig(RiskTier.TIER_3)
    r.up.result = UpstreamResult([{"type": "text", "text": "key AKIAIOSFODNN7EXAMPLE"}], False)
    res = r.call("demo.search_issues", {"query": "x"})
    assert res["isError"] is False and "AKIA" in text(res)
    details = [b["details"] for t, b in r.ledger() if t == "tool.executed"][0]
    assert "SECRET" in details["output_findings"]
    assert "AKIA" not in json.dumps(r.ledger())  # evidence names the finding, not the value


def test_non_text_content_is_dropped_with_a_note() -> None:
    r = rig()
    r.up.result = UpstreamResult(
        [
            {"type": "text", "text": "hi"},
            {"type": "image", "data": "aGk=", "mimeType": "image/png"},
        ],
        False,
    )
    res = r.call("demo.search_issues", {"query": "x"})
    assert len(res["content"]) == 1 and "1 non-text" in text(res)


def test_evidence_failure_withholds_output_at_tier2_only() -> None:
    for tier, withheld in [(RiskTier.TIER_2, True), (RiskTier.TIER_3, False)]:
        r = rig(tier)
        # Decision evidence succeeds; the result record fails.
        orig = r.h.core.record

        def failing(event: Any, _orig: Any = orig, _r: Any = r) -> Any:
            _r.h.evidence_available = False
            return _orig(event)

        r.h.core.record = failing  # type: ignore[method-assign]
        res = r.call("demo.search_issues", {"query": "x"})
        assert res["isError"] is withheld, tier


# ------------------------------------------------------------------ approvals


def approve(r: Rig, ticket: str, who: str = "fin-1") -> None:
    from uuid import UUID

    r.h.decide_approval(UUID(ticket), True, who)


def test_approval_flow_runs_once_with_the_approved_arguments() -> None:
    r = rig(RiskTier.TIER_3, AutonomyLevel.A3)
    args = {"title": "Pay vendor"}
    held = r.call("demo.create_issue", args)
    assert held["isError"] is True and held["_meta"]["state"] == "pending"
    ticket = held["_meta"][APPROVAL_META]
    assert r.up.calls == []
    # not yet approved → still pending, still nothing executed
    assert r.call("demo.create_issue", args, ticket)["_meta"]["state"] == "pending"
    approve(r, ticket)
    ok = r.call("demo.create_issue", args, ticket)
    assert ok["isError"] is False and r.up.calls == [("create_issue", args)]
    again = r.call("demo.create_issue", args, ticket)  # replay
    assert again["isError"] is True and len(r.up.calls) == 1


def test_approval_is_bound_to_arguments_tool_and_caller() -> None:
    r = rig(RiskTier.TIER_3, AutonomyLevel.A3)
    other = r.add("k2", "agent-2", RiskTier.TIER_3, AutonomyLevel.A3)
    args = {"title": "Pay vendor"}
    ticket = r.call("demo.create_issue", args)["_meta"][APPROVAL_META]
    approve(r, ticket)
    # different arguments
    res = r.call("demo.create_issue", {"title": "Pay someone else"}, ticket)
    assert res["isError"] is True and "arguments changed" in text(res)
    # same arguments, different tool (regression: the approval named only the arguments)
    res = r.call("demo.search_issues", {}, ticket)
    assert res["isError"] is True and r.up.calls == []
    # same arguments, other caller
    res = r.call("demo.create_issue", args, ticket, hdr=other)
    assert res["isError"] is True and "Unknown approval ticket" in text(res)
    # the legitimate retry still works after all those refusals
    assert r.call("demo.create_issue", args, ticket)["isError"] is False


def test_rejected_and_garbage_tickets() -> None:
    from uuid import UUID, uuid4

    r = rig(RiskTier.TIER_3, AutonomyLevel.A3)
    ticket = r.call("demo.create_issue", {})["_meta"][APPROVAL_META]
    r.h.core.decide_approval(UUID(ticket), False, "fin-1", "not this one")
    res = r.call("demo.create_issue", {}, ticket)
    assert res["isError"] is True and "rejected" in text(res) and "not this one" in text(res)
    assert "Unknown" in text(r.call("demo.create_issue", {}, "not-a-uuid"))
    assert "Unknown" in text(r.call("demo.create_issue", {}, str(uuid4())))
    assert r.up.calls == []


# ------------------------------------------------------------- registry / core


def test_registry_rejects_unsafe_tool_names() -> None:
    import pytest

    from captain.core.registry.service import RegistryError

    r = registry()
    for bad in ("has space", "ünïcode", "a/b", "", "x" * 129):
        with pytest.raises(RegistryError):
            r.upsert_tool(ToolEntry(bad, "demo-mcp", "d", ToolImpact.LOW, ToolRisk.LOW))
    with pytest.raises(RegistryError):
        r.upsert_tool(
            ToolEntry("ok.name", "demo-mcp", "d", ToolImpact.LOW, ToolRisk.LOW, upstream_name="a b")
        )


def test_tool_target_uses_upstream_name_and_respects_allow_list() -> None:
    import pytest

    from captain.core.decision_api import NoRouteError

    r = rig()
    reg = r.h.core.m.registry
    assert reg is not None
    reg.upsert_tool(
        ToolEntry(
            "demo.renamed", "demo-mcp", "d", ToolImpact.LOW, ToolRisk.LOW, upstream_name="real"
        )
    )
    ctx = r.h.core.resolve(
        Credential(kind="virtual_key", secret=r.hdr["Authorization"][7:]), ContextHints()
    )
    t = r.h.core.tool_target(ctx, "demo.search_issues")
    assert t.upstream_name == "search_issues" and t.credential_ref == "env:K"  # default: after "."
    with pytest.raises(NoRouteError):  # registered and enabled, but not allowed for this entry
        r.h.core.tool_target(ctx, "demo.renamed")
    with pytest.raises(NoRouteError):
        r.h.core.tool_target(ctx, "demo.retired")


def test_a_client_that_only_repeats_the_call_still_reaches_its_approval() -> None:
    """Most chat clients cannot attach a ticket id: repeating the SAME call must find the
    approval for this caller, tool and exact arguments, run once, and never double-ticket."""
    r = rig(RiskTier.TIER_3, AutonomyLevel.A3)
    args = {"title": "Rotate the signing key"}
    first = r.call("demo.create_issue", args)
    ticket = first["_meta"][APPROVAL_META]
    again = r.call("demo.create_issue", args)  # still waiting: the SAME ticket, not a new one
    assert again["_meta"]["state"] == "pending" and again["_meta"][APPROVAL_META] == ticket
    assert r.up.calls == []
    # Other arguments are a different action: their own ticket, and they do not ride on this one.
    other = r.call("demo.create_issue", {"title": "something else"})
    assert other["_meta"][APPROVAL_META] != ticket
    approve(r, ticket)
    ok = r.call("demo.create_issue", args)  # no ticket id sent
    assert ok["isError"] is False and r.up.calls == [("create_issue", args)]
    used = r.call("demo.create_issue", args)  # the approval is spent: a new ticket is needed
    assert used["isError"] is True and used["_meta"]["state"] == "pending"
    assert used["_meta"][APPROVAL_META] != ticket and len(r.up.calls) == 1
    # Another caller with the same arguments cannot use it either.
    r2 = rig(RiskTier.TIER_3, AutonomyLevel.A3)
    assert r2.call("demo.create_issue", args)["_meta"]["state"] == "pending"


def test_ticket_put_inside_the_arguments_does_not_change_the_action() -> None:
    """Regression (VS Code agent mode, 2026-11-26): the model cannot send request _meta, so
    on a retry it put `_meta: {ticket}` INSIDE the tool arguments. Every retry then looked
    like a new action and got a new ticket. `_meta` is never a tool argument: it is removed
    before the call is fingerprinted, and the ticket in it is honoured."""
    r = rig(RiskTier.TIER_3, AutonomyLevel.A3)
    args = {"title": "Rotate the signing key"}
    first = r.call("demo.create_issue", args)
    ticket = first["_meta"][APPROVAL_META]
    assert "_meta" not in text(first) and "same arguments" in text(first)
    with_meta = {**args, "_meta": {APPROVAL_META: ticket}}
    waiting = r.call("demo.create_issue", with_meta)  # not approved yet: the same ticket
    assert waiting["_meta"][APPROVAL_META] == ticket and r.up.calls == []
    approve(r, ticket)
    ok = r.call("demo.create_issue", with_meta)
    assert ok["isError"] is False
    assert r.up.calls == [("create_issue", args)]  # the upstream never sees `_meta`
    spent = r.call("demo.create_issue", with_meta)  # single use still holds
    assert spent["isError"] is True and len(r.up.calls) == 1


# ------------------------------------------------------------------ per-tool limits


def test_a_per_tool_rate_limit_stops_the_tool_and_only_that_tool() -> None:
    from captain.core.budget.model import RateLimitDefinition

    r = rig()
    r.h.built.infra.budget_catalog.add_rate(  # type: ignore[attr-defined]
        RateLimitDefinition("tool:demo.search_issues", requests_per_minute=2)
    )
    assert r.call("demo.search_issues", {"query": "a"})["isError"] is False
    assert r.call("demo.search_issues", {"query": "b"})["isError"] is False
    limited = r.call("demo.search_issues", {"query": "c"})
    assert limited["isError"] is True
    assert limited["_meta"]["com.stixis.captain/reason"] == "RATE_LIMITED"
    assert "demo.search_issues" in text(limited) and len(r.up.calls) == 2  # never reached it
    assert r.call("demo.create_issue", {"title": "other tool"})["isError"] is False  # unaffected


def test_a_per_caller_tool_limit_is_counted_per_caller() -> None:
    from captain.core.budget.model import RateLimitDefinition

    r = rig()
    r.h.built.infra.budget_catalog.add_rate(  # type: ignore[attr-defined]
        RateLimitDefinition("ptool:agent-1:demo.search_issues", requests_per_minute=1)
    )
    assert r.call("demo.search_issues", {"query": "a"})["isError"] is False
    assert r.call("demo.search_issues", {"query": "b"})["isError"] is True


def test_another_callers_limit_does_not_slow_this_caller() -> None:
    from captain.core.budget.model import RateLimitDefinition

    r = rig()
    r.h.built.infra.budget_catalog.add_rate(  # type: ignore[attr-defined]
        RateLimitDefinition("ptool:someone-else:demo.search_issues", requests_per_minute=1)
    )
    for q in "abc":
        assert r.call("demo.search_issues", {"query": q})["isError"] is False
