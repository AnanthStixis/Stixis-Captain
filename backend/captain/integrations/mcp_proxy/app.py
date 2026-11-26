"""Captain MCP Proxy — an MCP server that fronts governed tools for agents.

An agent (Claude, Cursor, a custom agent) points its MCP client at `POST /mcp` with a
Captain credential. It sees only the tools its register entry allows and the registry
enables. Every `tools/call` is decided by Captain Core (kill switch, registry,
guardrails, risk, OPA) BEFORE the upstream tool server is contacted; results are
screened as untrusted content before they reach the agent; evidence is recorded.

Protocol: one stateless endpoint that serves both eras (see `upstream.py`):
- modern (2026-07-28): `server/discover`, `tools/list`, `tools/call`, no session.
- legacy (2025): `initialize` first. No `Mcp-Session-Id` is issued — the server is
  stateless, which the spec allows. Notifications get 202.
Batches, server-initiated streams (GET) and sampling/elicitation are not supported.
Browsers cannot call it: any `Origin` not on the allow-list gets 403 (DNS rebinding).

Approval flow: a call held for human approval returns a tool result (isError) naming a
ticket. After an approver decides in the dashboard, the agent repeats the same call with
`_meta["com.stixis.captain/approval_ticket"] = <ticket>`. The approval is bound to the
caller, the tool and the exact arguments, and works once.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from captain.api.limits import BodyLimitMiddleware
from captain.core.decision_api import (
    ApprovalError,
    AuthenticationError,
    ContextUnresolvedError,
    CoreError,
    EvidenceUnavailableError,
    McpCoreAPI,
)
from captain.core.models import (
    ActionType,
    ApprovalState,
    Channel,
    ContentRef,
    ContextHints,
    CoverageClass,
    Credential,
    Decision,
    DecisionType,
    EventType,
    GovernanceRequest,
    ResolvedContext,
    ToolTarget,
    UsageEvent,
)
from captain.integrations.mcp_proxy.upstream import (
    LEGACY_VERSION,
    MODERN_VERSION,
    UpstreamError,
    UpstreamResult,
)

APPROVAL_META = "com.stixis.captain/approval_ticket"
REASON_META = "com.stixis.captain/reason"
HANDSHAKE_VERSIONS = ("2025-11-25", LEGACY_VERSION, "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS = (MODERN_VERSION, *HANDSHAKE_VERSIONS)
UNSUPPORTED_PROTOCOL_VERSION = -32022  # from the 2026 spec, as in the official SDK
MAX_BODY_BYTES = 1_000_000
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
log = logging.getLogger("captain.mcp")


class ToolCaller(Protocol):
    def call_tool(self, target: ToolTarget, arguments: dict[str, Any]) -> UpstreamResult: ...


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _reply(rid: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


def _fail(rid: Any, code: int, message: str, status: int = 200, data: Any = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": error}, status_code=status)


def wire_name(name: str) -> str:
    """The name a client sees. Some clients (VS Code) accept only [a-z0-9_-] in tool names,
    so the registry's `server.tool` dot becomes `__`. Calls are mapped back to the registry
    name before the Core decides, so policy, approvals and evidence use the real name."""
    return name.replace(".", "__")


def _modern_envelope(method: str, result: dict[str, Any]) -> dict[str, Any]:
    """2026-07-28 results say they are complete; list/discover results also say how long
    they may be cached. Tool lists differ per caller, so they are never shared."""
    out = {**result, "resultType": "complete"}
    if method in ("tools/list", "server/discover"):
        out.update(ttlMs=0, cacheScope="private")
    return out


def _tool_result(text: str, is_error: bool, meta: dict[str, str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if meta:
        out["_meta"] = meta
    return out


def create_mcp_app(
    core: McpCoreAPI,
    upstream: ToolCaller,
    allowed_origins: frozenset[str] = frozenset(),
) -> FastAPI:
    app = FastAPI(title="Captain MCP Proxy", version="1", docs_url=None, redoc_url=None)
    app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # No server-initiated stream and no sessions to close.
    @app.get("/mcp")
    @app.delete("/mcp")
    def not_supported() -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    # ------------------------------------------------------------ helpers
    def authenticate(request: Request) -> ResolvedContext:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:].strip():
            raise AuthenticationError("missing bearer credential")
        token = auth[7:].strip()
        kind = "virtual_key" if token.startswith("cpt_") else "workload_oidc"
        fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        ip = fwd or (request.client.host if request.client else None)
        return core.resolve(Credential(kind=kind, secret=token), ContextHints(client_ip=ip))

    def record_execution(
        req: GovernanceRequest, ctx: ResolvedContext, details: dict[str, str | int | bool | None]
    ) -> None:
        core.record(
            UsageEvent(
                request_id=req.request_id,
                trace_id=req.trace_id,
                event_type=EventType.TOOL_EXECUTED,
                channel=Channel.MCP,
                coverage=CoverageClass.GOVERNED,
                principal_id=ctx.principal.id,
                register_id=ctx.register_entry.id,
                tool=req.tool,
                details=details,
            )
        )

    def execute(
        ctx: ResolvedContext, req: GovernanceRequest, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Call the upstream tool, screen the result, write evidence."""
        assert req.tool
        t0 = time.monotonic()
        details: dict[str, str | int | bool | None] = {}
        out: dict[str, Any]
        try:
            target = core.tool_target(ctx, req.tool)
            details["server"] = target.server_id
            res = upstream.call_tool(target, args)
        except CoreError as e:
            details["outcome"] = "no_route"
            out = _tool_result(f"Tool is not reachable: {e}", True, {REASON_META: e.reason_code})
        except UpstreamError as e:
            log.warning("upstream failure for %s: %s", req.tool, e)
            details["outcome"] = "upstream_error"
            out = _tool_result("The tool server could not complete the call.", True)
        else:
            text = "\n".join(str(c.get("text", "")) for c in res.content if c.get("type") == "text")
            dropped = sum(1 for c in res.content if c.get("type") != "text")
            verdict = core.screen_tool_output(req, text)
            details.update(
                outcome="output_blocked" if verdict.blocked else "ok",
                upstream_is_error=res.is_error,
                output_findings=",".join(verdict.entities),
                output_masked=verdict.masked,
                non_text_dropped=dropped,
            )
            if verdict.blocked:
                out = _tool_result(
                    "Captain withheld this tool's output: "
                    + (", ".join(verdict.entities) or "policy"),
                    True,
                    {REASON_META: "OUTPUT_BLOCKED"},
                )
            else:
                note = f"\n[Captain: {dropped} non-text item(s) removed]" if dropped else ""
                out = _tool_result(verdict.text + note, res.is_error)
        details["latency_ms"] = int((time.monotonic() - t0) * 1000)
        try:
            record_execution(req, ctx, details)
        except EvidenceUnavailableError:
            if ctx.register_entry.risk_tier.fails_closed:
                return _tool_result(
                    "Captain could not record evidence, so the output was withheld.",
                    True,
                    {REASON_META: "CONTROL_UNAVAILABLE"},
                )
        return out

    def call_approved(
        ctx: ResolvedContext, req: GovernanceRequest, raw: object, args: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            ticket_id = UUID(str(raw))
            ticket = core.approval_status(ticket_id)
            if ticket.requested_by != ctx.principal.id:
                raise ApprovalError("unknown ticket")  # never reveal another caller's tickets
        except (ValueError, ApprovalError):
            return _tool_result(
                "Unknown approval ticket.", True, {REASON_META: "APPROVAL_REQUIRED"}
            )
        if ticket.state is ApprovalState.PENDING:
            return _pending(ticket_id)
        if ticket.state is not ApprovalState.APPROVED:
            why = f" ({ticket.reason})" if ticket.reason else ""
            return _tool_result(
                f"Approval {ticket.state.value.lower()}{why}; this call will not run.",
                True,
                {REASON_META: "APPROVAL_REQUIRED"},
            )
        try:
            core.consume_approval(ticket_id, req)
        except ApprovalError as e:
            return _tool_result(f"Approval cannot be used: {e}", True, {REASON_META: e.reason_code})
        return execute(ctx, req, args)

    def _pending(ticket_id: UUID) -> dict[str, Any]:
        return _tool_result(
            f"This action needs human approval (ticket {ticket_id}). Once it is approved, call "
            "this tool again with exactly the same arguments. Do not add the ticket id to the "
            "arguments: Captain finds the approval itself.",
            True,
            {REASON_META: "APPROVAL_REQUIRED", APPROVAL_META: str(ticket_id), "state": "pending"},
        )

    def handle_call(ctx: ResolvedContext, params: dict[str, Any]) -> dict[str, Any]:
        name, args = params.get("name"), params.get("arguments", {})
        if isinstance(name, str) and "." not in name:
            name = {wire_name(t.name): t.name for t in core.list_tools(ctx)}.get(name, name)
        if not isinstance(name, str) or not TOOL_NAME.fullmatch(name) or not isinstance(args, dict):
            raise RpcError(-32602, "tools/call needs a valid name and an arguments object")
        meta = params.get("_meta")
        if isinstance(args, dict) and "_meta" in args:
            # Clients that cannot send request _meta (VS Code agent mode) let the model put
            # it inside the tool arguments. It is never a tool argument: take it out before
            # the call is fingerprinted, and honour an approval ticket found in it.
            args = dict(args)
            inner = args.pop("_meta")
            if not isinstance(meta, dict) and isinstance(inner, dict):
                meta = inner
        ticket = meta.get(APPROVAL_META) if isinstance(meta, dict) else None
        req = GovernanceRequest(
            trace_id=f"mcp-{uuid4().hex[:12]}",
            context=ctx,
            channel=Channel.MCP,
            action=ActionType.TOOL_CALL,
            tool=name,
            tool_arguments=args,
            contents=(ContentRef(text=json.dumps(args, sort_keys=True, default=str)),),
        )
        if ticket is not None:
            return call_approved(ctx, req, ticket, args)
        d = core.decide(req)
        if d.decision is DecisionType.REQUIRE_APPROVAL:
            # A client that cannot attach a ticket id (most chat clients only repeat the call)
            # still reaches its approval: the ticket for this caller, tool and exact arguments.
            known = core.find_approval(req)
            if known is not None and known.state is ApprovalState.APPROVED:
                return call_approved(ctx, req, str(known.ticket_id), args)
            if known is not None:
                return _pending(known.ticket_id)  # still waiting: never a second ticket
            try:
                return _pending(core.request_approval(req, d).ticket_id)
            except ApprovalError as e:
                return _tool_result(
                    f"Approval cannot be requested: {e}", True, {REASON_META: e.reason_code}
                )
        if not d.permits_execution:
            return _tool_result(
                f"Blocked by Captain policy: {d.reason_code.value}. {d.explanation}",
                True,
                {REASON_META: d.reason_code.value},
            )
        final = _redacted_args(d, args)
        if final is None:
            return _tool_result(
                "Captain could not safely mask this call's arguments, so it was refused.",
                True,
                {REASON_META: "PII_REDACTED"},
            )
        return execute(ctx, req, final)

    def _redacted_args(d: Decision, args: dict[str, Any]) -> dict[str, Any] | None:
        if d.decision is not DecisionType.ALLOW_WITH_REDACTION:
            return args
        try:
            text = d.redacted_contents[0].text if d.redacted_contents else None
            masked = json.loads(text or "")
        except ValueError:
            return None
        return masked if isinstance(masked, dict) else None

    def dispatch(ctx: ResolvedContext, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "ping":
            return {}
        if method == "server/discover":
            return {
                "supportedVersions": list(SUPPORTED_VERSIONS),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stixis-captain", "version": "0.1"},
            }
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in HANDSHAKE_VERSIONS else HANDSHAKE_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "stixis-captain", "version": "0.1"},
            }
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": wire_name(t.name),
                        "title": t.name,
                        "description": t.description,
                        "inputSchema": t.input_schema,
                        "annotations": {
                            "readOnlyHint": t.read_only,
                            "destructiveHint": (not t.read_only) and t.impact == "high",
                        },
                    }
                    for t in core.list_tools(ctx)
                ]
            }
        if method == "tools/call":
            return handle_call(ctx, params)
        raise RpcError(-32601, f"method not found: {method}")

    # ----------------------------------------------------------- endpoint
    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
        try:
            ctx = await run_in_threadpool(authenticate, request)
        except AuthenticationError:
            return JSONResponse(
                {"error": "invalid or missing credential"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="captain"'},
            )
        except ContextUnresolvedError:
            return JSONResponse({"error": "no governed context for this credential"}, 403)
        except CoreError:
            return JSONResponse({"error": "control unavailable"}, status_code=503)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse({"error": "request too large"}, status_code=413)
        try:
            msg = json.loads(raw)
        except ValueError:
            return _fail(None, -32700, "parse error", 400)
        if isinstance(msg, list):
            return _fail(None, -32600, "batch requests are not supported", 400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _fail(None, -32600, "invalid request", 400)
        method, rid = msg.get("method"), msg.get("id")
        if not isinstance(method, str):
            return (
                Response(status_code=202)
                if "result" in msg or "error" in msg
                else _fail(rid, -32600, "invalid request", 400)
            )
        if rid is None:  # notification
            return Response(status_code=202)
        requested = request.headers.get("mcp-protocol-version")
        if requested is not None and requested not in SUPPORTED_VERSIONS:
            return _fail(
                rid,
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                400,
                {"supported": list(SUPPORTED_VERSIONS), "requested": requested},
            )
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _fail(rid, -32602, "params must be an object")
        try:
            result = await run_in_threadpool(dispatch, ctx, method, params)
        except RpcError as e:
            return _fail(rid, e.code, e.message)
        except CoreError as e:
            return _fail(rid, -32603, f"{e.reason_code.value}")
        except Exception:
            log.exception("mcp dispatch failed")
            return _fail(rid, -32603, "internal error")
        if requested == MODERN_VERSION:
            result = _modern_envelope(method, result)
        return _reply(rid, result)

    return app
