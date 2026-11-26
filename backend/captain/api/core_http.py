"""HTTP transport for the Core Decision API: /core/v1/*.

Used by out-of-process integrations (browser extension, IDE connector, desktop agent,
telemetry hooks). The in-process API Gateway and MCP Proxy call the Python API directly.

Security model — the server never trusts state sent by the client:
- Every call authenticates the caller's own credential (Bearer virtual key or workload
  OIDC token) and resolves context server-side. A client cannot send a context.
- Decisions, held requests and reservations are kept server-side and referenced by id;
  only the principal that created them can use them.
- `record` accepts only OBSERVED / BYPASS_DETECTED / UNOBSERVED events. GOVERNED evidence
  is written by the Core itself during decide/settle. A caller reports on its own
  behalf unless it holds the `integration` role.
- Content is inspected but never logged or echoed except as masked text.

Limitation: the id → state cache is per process (TTL). Run behind sticky routing or a
single replica until it moves to PostgreSQL.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from captain.api.limits import BodyLimitMiddleware
from captain.api.state import InMemoryStateStore, StateStore
from captain.core.decision_api import (
    ApprovalError,
    AuthenticationError,
    BudgetExceededError,
    ContextUnresolvedError,
    CoreError,
    EvidenceUnavailableError,
)
from captain.core.engine import CaptainCore
from captain.core.models import (
    ActionType,
    ActualUsage,
    ApprovalTicket,
    Channel,
    ContentRef,
    ContextHints,
    CostEstimate,
    CoverageClass,
    Credential,
    Decision,
    EventType,
    GovernanceRequest,
    LedgerReceipt,
    ReasonCode,
    Reservation,
    ResolvedContext,
    Settlement,
    UsageEvent,
)

_STATUS = {
    AuthenticationError: 401,
    ContextUnresolvedError: 403,
    BudgetExceededError: 429,
    ApprovalError: 409,
    EvidenceUnavailableError: 503,
}


_NOT_FOUND = {"reason_code": "NOT_FOUND", "message": "unknown or expired id"}


class _State:
    """Typed access to the StateStore. Missing and not-yours look identical (404) so ids
    cannot be probed. Content is stripped before anything is stored."""

    def __init__(self, store: StateStore) -> None:
        self._s = store

    def put_decision(self, owner: str, req: GovernanceRequest, d: Decision) -> None:
        self._s.put(
            "decision",
            str(req.request_id),
            owner,
            {
                "request": req.model_copy(update={"contents": ()}).model_dump(mode="json"),
                "decision": d.model_copy(update={"redacted_contents": None}).model_dump(
                    mode="json"
                ),
            },
        )

    def decision(self, request_id: UUID, owner: str) -> tuple[GovernanceRequest, Decision]:
        doc = self._s.get("decision", str(request_id), owner)
        if doc is None:
            raise HTTPException(404, _NOT_FOUND)
        return (
            GovernanceRequest.model_validate(doc["request"]),
            Decision.model_validate(doc["decision"]),
        )

    def put_reservation(self, owner: str, r: Reservation) -> None:
        self._s.put("reservation", str(r.reservation_id), owner, r.model_dump(mode="json"))

    def reservation(self, reservation_id: UUID, owner: str) -> Reservation:
        doc = self._s.get("reservation", str(reservation_id), owner)
        if doc is None:
            raise HTTPException(404, _NOT_FOUND)
        return Reservation.model_validate(doc)


# ------------------------------------------------------------------ bodies


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecideBody(_Body):
    channel: Channel
    action: ActionType
    model: str | None = None
    tool: str | None = None
    tool_arguments: dict[str, object] | None = None
    contents: list[ContentRef] = Field(default_factory=list, max_length=64)
    max_output_tokens: int | None = Field(default=None, gt=0)
    streaming: bool = False
    trace_id: str = Field(default="", max_length=128)
    hints: ContextHints = Field(default_factory=ContextHints)


class ReserveBody(_Body):
    request_id: UUID
    estimate: CostEstimate


class SettleBody(_Body):
    reservation_id: UUID
    actual: ActualUsage


class ReleaseBody(_Body):
    reservation_id: UUID


class ApprovalBody(_Body):
    request_id: UUID


class RestoreBody(_Body):
    request_id: UUID
    text: str = Field(max_length=1_000_000)


class RecordBody(_Body):
    event_type: EventType
    channel: Channel
    coverage: CoverageClass
    trace_id: str = Field(max_length=128)
    model: str | None = None
    tool: str | None = None
    usage: ActualUsage | None = None
    principal_id: str | None = None  # only honoured for the `integration` role
    details: dict[str, str | int | float | bool | None] | None = None


# ------------------------------------------------------------------- app


def create_app(
    core: CaptainCore,
    state: StateStore | None = None,
    max_body_bytes: int = 2_000_000,
) -> FastAPI:
    app = FastAPI(title="Captain Core API", version="1", docs_url=None, redoc_url=None)
    app.add_middleware(BodyLimitMiddleware, max_bytes=max_body_bytes)
    st = _State(state or InMemoryStateStore())

    @app.middleware("http")
    async def limit_size(request: Request, call_next: Any) -> Any:
        if int(request.headers.get("content-length") or 0) > max_body_bytes:
            return JSONResponse({"reason_code": "CONTENT_TOO_LARGE"}, status_code=413)
        return await call_next(request)

    @app.exception_handler(CoreError)
    async def core_error(_: Request, e: CoreError) -> JSONResponse:
        status = next((s for t, s in _STATUS.items() if isinstance(e, t)), 503)
        return JSONResponse(
            {"reason_code": e.reason_code.value, "message": str(e)}, status_code=status
        )

    def caller(
        authorization: Annotated[str | None, Header()] = None,
        x_client_ip: Annotated[str | None, Header()] = None,
    ) -> ResolvedContext:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("missing bearer credential")
        token = authorization[7:].strip()
        kind = "virtual_key" if token.startswith("cpt_") else "workload_oidc"
        return core.resolve(
            Credential(kind=kind, secret=token), ContextHints(client_ip=x_client_ip)
        )

    Caller = Annotated[ResolvedContext, Depends(caller)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/core/v1/decide")
    def decide(body: DecideBody, ctx: Caller) -> Decision:
        if body.hints.model_dump(exclude_none=True, exclude={"client_ip"}):
            # Hints may only confirm the key's binding; re-resolve to enforce that.
            for f in ("project_id", "application_id", "environment"):
                v = getattr(body.hints, f)
                if v is not None and v != getattr(ctx, f):
                    raise ContextUnresolvedError(f"{f} hint conflicts with credential binding")
        req = GovernanceRequest(
            trace_id=body.trace_id or "http",
            context=ctx,
            channel=body.channel,
            action=body.action,
            model=body.model,
            tool=body.tool,
            tool_arguments=body.tool_arguments,
            contents=tuple(body.contents),
            max_output_tokens=body.max_output_tokens,
            streaming=body.streaming,
        )
        d = core.decide(req)
        st.put_decision(ctx.principal.id, req, d)
        return d

    @app.post("/core/v1/reserve")
    def reserve(body: ReserveBody, ctx: Caller) -> Reservation:
        _, d = st.decision(body.request_id, ctx.principal.id)
        r = core.reserve(d, body.estimate)
        st.put_reservation(ctx.principal.id, r)
        return r

    @app.post("/core/v1/settle")
    def settle(body: SettleBody, ctx: Caller) -> Settlement:
        return core.settle(st.reservation(body.reservation_id, ctx.principal.id), body.actual)

    @app.post("/core/v1/release")
    def release(body: ReleaseBody, ctx: Caller) -> Settlement:
        return core.release(st.reservation(body.reservation_id, ctx.principal.id))

    @app.post("/core/v1/approvals")
    def request_approval(body: ApprovalBody, ctx: Caller) -> ApprovalTicket:
        req, d = st.decision(body.request_id, ctx.principal.id)
        return core.request_approval(req, d)

    @app.get("/core/v1/approvals/{ticket_id}")
    def approval_status(ticket_id: UUID, ctx: Caller) -> ApprovalTicket:
        try:
            t = core.approval_status(ticket_id)
        except ApprovalError:
            t = None
        if t is None or t.requested_by != ctx.principal.id:
            raise HTTPException(
                404, {"reason_code": "NOT_FOUND", "message": "unknown or expired id"}
            )
        return t

    @app.post("/core/v1/approvals/{ticket_id}/consume")
    def consume(ticket_id: UUID, body: ApprovalBody, ctx: Caller) -> ApprovalTicket:
        req, _ = st.decision(body.request_id, ctx.principal.id)
        return core.consume_approval(ticket_id, req)

    @app.post("/core/v1/restore")
    def restore(body: RestoreBody, ctx: Caller) -> dict[str, str]:
        _, d = st.decision(body.request_id, ctx.principal.id)
        return {"text": core.restore(d, body.text, ctx.principal.id)}

    @app.post("/core/v1/record")
    def record(body: RecordBody, ctx: Caller) -> LedgerReceipt:
        if body.coverage is CoverageClass.GOVERNED:
            raise CoreError(
                "GOVERNED evidence is written by the Core, not reported", ReasonCode.OBSERVED_ONLY
            )
        on_behalf = body.principal_id if "integration" in ctx.principal.roles else None
        return core.record(
            UsageEvent(
                trace_id=body.trace_id,
                event_type=body.event_type,
                channel=body.channel,
                coverage=body.coverage,
                principal_id=on_behalf or ctx.principal.id,
                register_id=ctx.register_entry.id,
                model=body.model,
                tool=body.tool,
                usage=body.usage,
                details=body.details,
            )
        )

    return app


def create_app_from_env() -> FastAPI:
    """uvicorn --factory captain.api.core_http:create_app_from_env"""
    from sqlalchemy import create_engine

    from captain.api.state import PgStateStore
    from captain.config import build_from_env

    built, settings = build_from_env()
    engine = create_engine(settings.database_url, pool_size=10, pool_pre_ping=True)
    return create_app(built.core, PgStateStore(engine))
