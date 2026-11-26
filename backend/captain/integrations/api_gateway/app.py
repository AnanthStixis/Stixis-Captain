"""Captain API Gateway — OpenAI-compatible endpoint for applications and agents.

Apps change only their base URL and key:

    client = OpenAI(base_url="https://captain.example.com/v1", api_key="cpt_live_…")

Per call (all Core operations go through the public Core API):
  authenticate → resolve context → decide (kill switch, registry, guardrails, risk,
  OPA) → apply masking and output cap → estimate + reserve budget → call provider
  (primary, then fallbacks on retryable errors before any output) → check output by
  tier → settle actual cost → evidence.

Errors use the OpenAI error shape with Captain's reason code, so existing SDKs surface
them naturally. Image/audio/realtime/batch content is refused explicitly (not yet
governable), so it cannot become a silent bypass.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar
from uuid import uuid4

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from captain.api.limits import DEFAULT_MAX_BODY_BYTES, BodyLimitMiddleware
from captain.api.state import InMemoryStateStore, StateStore
from captain.core.decision_api import (
    AuthenticationError,
    BudgetExceededError,
    ContextUnresolvedError,
    CoreError,
    EvidenceUnavailableError,
    GatewayCoreAPI,
    OutputBlockedError,
)
from captain.core.models import (
    ActionType,
    ActualUsage,
    Channel,
    ContentRef,
    ContextHints,
    CoverageClass,
    Credential,
    Decision,
    DecisionType,
    EventType,
    GovernanceRequest,
    ProviderTarget,
    ReasonCode,
    Reservation,
    ResolvedContext,
)
from captain.integrations.api_gateway.provider import ProviderClient, ProviderError
from captain.integrations.telemetry.base import ActorMap, import_records
from captain.integrations.telemetry.desktop_agent import DesktopReport, records_from_report

# Request fields forwarded to the provider. Anything else is dropped (not silently
# forwarded): new parameters are added deliberately after review.
PASSTHROUGH = {
    "temperature",
    "top_p",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "logit_bias",
    "user",
}
DEFAULT_MAX_OUTPUT = 1024
log = logging.getLogger("captain.gateway")


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "captain_governance", "code": code}},
        status_code=status,
    )


def _status_for(e: CoreError) -> int:
    if isinstance(e, AuthenticationError):
        return 401
    if isinstance(e, ContextUnresolvedError):
        return 403
    if isinstance(e, BudgetExceededError):
        return 429
    if isinstance(e, EvidenceUnavailableError):
        return 503
    return 403


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=200)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    stream: bool = False
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    n: int = 1
    stream_options: dict[str, Any] | None = None


MAX_EMBEDDING_INPUTS = 256
MAX_EMBEDDING_CHARS = 400_000


class BrowserCheckBody(BaseModel):
    """What the browser extension asks before a prompt is submitted to an AI website."""

    model_config = ConfigDict(extra="forbid")

    site: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")  # host only
    text: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = Field(default=None, max_length=100)


class EmbeddingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=200)
    input: str | list[str]
    encoding_format: str | None = Field(default=None, pattern="^(float|base64)$")
    dimensions: int | None = Field(default=None, gt=0, le=8192)
    user: str | None = Field(default=None, max_length=200)


def _texts(messages: list[dict[str, Any]]) -> list[tuple[int, int | None, str, bool]]:
    """(message index, part index or None, text, trusted). Tool results are untrusted
    (indirect prompt injection); non-text parts are refused."""
    out: list[tuple[int, int | None, str, bool]] = []
    for i, m in enumerate(messages):
        trusted = m.get("role") != "tool"
        c = m.get("content")
        if c is None:
            continue
        if isinstance(c, str):
            out.append((i, None, c, trusted))
        elif isinstance(c, list):
            for j, part in enumerate(c):
                if not isinstance(part, dict) or part.get("type") != "text":
                    raise GatewayError(
                        400,
                        "UNSUPPORTED_CONTENT",
                        "Only text content is governed today; image/audio parts are refused.",
                    )
                out.append((i, j, str(part.get("text", "")), trusted))
        else:
            raise GatewayError(400, "INVALID_REQUEST", "message content must be text")
    return out


def _apply_redaction(
    messages: list[dict[str, Any]], spots: list[tuple[int, int | None, str, bool]], d: Decision
) -> list[dict[str, Any]]:
    if not d.redacted_contents:
        return messages
    out = [dict(m) for m in messages]
    for (i, j, _, _), ref in zip(spots, d.redacted_contents, strict=True):
        if j is None:
            out[i]["content"] = ref.text
        else:
            parts = [dict(p) for p in out[i]["content"]]
            parts[j]["text"] = ref.text
            out[i]["content"] = parts
    return out


@dataclass(frozen=True)
class Prepared:
    d: Decision
    res: Reservation
    targets: list[ProviderTarget]
    payload: dict[str, Any]
    in_chars: int


class Refusal(Exception):
    """The Core refused the call (policy, budget, approval needed)."""

    def __init__(self, status: int, code: str, message: str, decision: Decision) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.decision = status, code, message, decision


def create_gateway_app(
    core: GatewayCoreAPI,
    provider: ProviderClient,
    state: StateStore | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    captain_hosts: tuple[str, ...] = (),
    desktop_actors: Mapping[str, str] | None = None,
) -> FastAPI:
    """`state` holds idempotency markers (content-free); use PgStateStore with several
    gateway replicas."""
    idem = state or InMemoryStateStore(ttl_s=24 * 3600)
    app = FastAPI(title="Captain API Gateway", version="1", docs_url=None, redoc_url=None)
    app.add_middleware(BodyLimitMiddleware, max_bytes=max_body_bytes)

    @app.exception_handler(GatewayError)
    async def gw_error(_: Request, e: GatewayError) -> JSONResponse:
        return _error(e.status, e.code, e.message)

    @app.exception_handler(CoreError)
    async def core_error(_: Request, e: CoreError) -> JSONResponse:
        return _error(_status_for(e), e.reason_code.value, str(e))

    def caller(authorization: str | None, client_ip: str | None) -> ResolvedContext:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("missing bearer credential")
        token = authorization[7:].strip()
        kind = "virtual_key" if token.startswith("cpt_") else "workload_oidc"
        return core.resolve(Credential(kind=kind, secret=token), ContextHints(client_ip=client_ip))

    def prepare(
        ctx: ResolvedContext,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        stream: bool,
        passthrough: dict[str, Any],
        idempotency_key: str | None,
        channel: Channel,
    ) -> Prepared:
        """Everything before the provider call, shared by every wire format: inspect,
        claim idempotency, decide, mask, bound output, reserve budget, route."""
        spots = _texts(messages)
        req = GovernanceRequest(
            trace_id=f"gw-{uuid4().hex[:12]}",
            context=ctx,
            channel=channel,
            action=ActionType.MODEL_CALL,
            model=model,
            contents=tuple(ContentRef(text=t, trusted=tr) for _, _, t, tr in spots),
            max_output_tokens=max_tokens,
            streaming=stream,
        )
        if idempotency_key:
            # Claim the key BEFORE any provider call, so retries and concurrent duplicates
            # are never charged twice. Only a marker is stored: never the response content.
            key = hashlib.sha256(f"{ctx.principal.id}|{idempotency_key}".encode()).hexdigest()
            if not idem.claim(
                "idempotency", key, ctx.principal.id, {"request_id": str(req.request_id)}
            ):
                prior = idem.get("idempotency", key, ctx.principal.id) or {}
                raise GatewayError(
                    409,
                    "IDEMPOTENCY_REPLAY",
                    f"Idempotency-Key already used (request {prior.get('request_id', 'unknown')}); "
                    "the original call was not repeated.",
                )
        d = core.decide(req)
        if not d.permits_execution:
            code = (
                429
                if d.reason_code in (ReasonCode.BUDGET_EXHAUSTED, ReasonCode.RATE_LIMITED)
                else 403
            )
            if d.decision is DecisionType.REQUIRE_APPROVAL:
                d_msg = "This model call requires human approval, which the gateway does not hold."
            else:
                d_msg = d.explanation
            raise Refusal(code, d.reason_code.value, d_msg, d)

        final = _apply_redaction(messages, spots, d)
        requested = max_tokens or DEFAULT_MAX_OUTPUT
        cap = d.limits.max_output_tokens
        max_out = min(requested, cap) if cap else requested
        in_chars = sum(len(t) for _, _, t, _ in spots)
        res = core.reserve(d, core.estimate_cost(d, in_chars, max_out))
        try:
            targets = core.route(d)
        except CoreError:
            core.release(res)
            raise
        return Prepared(
            d, res, targets, {**passthrough, "messages": final, "max_tokens": max_out}, in_chars
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    def models(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        ctx = caller(authorization, None)
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "owned_by": "captain"}
                for m in sorted(ctx.register_entry.allowed_models)
            ],
        }

    @app.get("/v1/governance/status")
    def status(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        ctx = caller(authorization, None)
        e = ctx.register_entry
        return {
            "principal": ctx.principal.id,
            "register_id": e.id,
            "system": e.name,
            "risk_tier": e.risk_tier.value,
            "autonomy": e.autonomy.value,
            "project": ctx.project_id,
            "application": ctx.application_id,
            "environment": ctx.environment.value,
            "allowed_models": sorted(e.allowed_models),
        }

    @app.get("/v1/usage")
    def usage(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        """How much of the caller's budgets is used this period. Money is in micro-units
        (1 USD = 1,000,000). Budgets that belong to the caller (principal, key, application,
        project) are listed; broader ones (organisation, cost centre, team) are not named,
        but they still count toward `tightest`: the least headroom across every budget."""
        ctx = caller(authorization, None)
        rows = core.usage(ctx)
        own = ("principal:", "key:", "app:", "project:", "model:")
        tightest = min(rows, key=lambda r: r.remaining, default=None)
        return {
            "object": "usage",
            "principal": ctx.principal.id,
            "currency": "USD",
            "unit": "micro-units (1 USD = 1000000)",
            "budgets": [
                {
                    "scope": r.scope,
                    "period": r.period.lower(),
                    "limit": r.limit,
                    "spent": r.spent,
                    "reserved": r.reserved,
                    "remaining": r.remaining,
                    "percent_used": round(100 * r.spent / r.limit, 1) if r.limit else None,
                }
                for r in rows
                if r.scope.startswith(own)
            ],
            "tightest": (
                {"kind": tightest.scope.split(":", 1)[0], "remaining": tightest.remaining}
                if tightest
                else None
            ),
        }

    @app.get("/v1/ide/continue")
    def ide_continue(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> Response:
        """A Continue `config.yaml` that sends this developer's chat through Captain, with
        the models their AI Register entry allows. The key is NOT filled in: the developer
        pastes their own, so it never travels through a config file Captain generated.
        Coverage: GOVERNED for chat/edit/apply made through these entries."""
        ctx = caller(authorization, None)
        base = str(request.base_url).rstrip("/") + "/v1"
        models = "\n".join(
            f"  - name: {m}\n    provider: openai\n    model: {m}\n    apiBase: {base}\n"
            "    apiKey: PASTE_YOUR_CAPTAIN_KEY_HERE\n    roles: [chat, edit, apply]"
            for m in sorted(ctx.register_entry.allowed_models)
        )
        body = f"name: Captain\nversion: 1.0.0\nschema: v1\nmodels:\n{models}\n"
        return Response(body, media_type="text/yaml")

    def anthropic_caller(
        authorization: str | None, x_api_key: str | None, client_ip: str | None
    ) -> ResolvedContext:
        # Anthropic SDKs send x-api-key; Claude Code sends Authorization: Bearer when a
        # gateway token is configured.
        return caller(authorization or (f"Bearer {x_api_key}" if x_api_key else None), client_ip)

    def anthropic_error(status: int, message: str, d: Decision | None = None) -> JSONResponse:
        from captain.integrations.api_gateway import messages as M

        rid = f"req_{d.request_id.hex[:24]}" if d else None
        resp = JSONResponse(M.error_body(M.error_type(status), message, rid), status_code=status)
        return _with_headers(resp, d) if d else resp

    @app.post("/v1/messages", response_model=None)
    async def messages(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
        x_forwarded_for: Annotated[str | None, Header()] = None,
    ) -> JSONResponse | StreamingResponse:
        from starlette.concurrency import run_in_threadpool

        try:
            raw = await request.json()
        except ValueError:
            return anthropic_error(400, "request body must be valid JSON")
        return await run_in_threadpool(
            handle_messages,
            raw,
            authorization,
            x_api_key,
            (x_forwarded_for or "").split(",")[0].strip() or None,
        )

    def handle_messages(
        raw: Any, authorization: str | None, x_api_key: str | None, ip: str | None
    ) -> JSONResponse | StreamingResponse:
        from captain.integrations.api_gateway import messages as M

        t0 = time.monotonic()
        try:
            ctx = anthropic_caller(authorization, x_api_key, ip)
            if not isinstance(raw, dict):
                raise M.MessagesError("request body must be a JSON object")
            tr = M.to_openai(raw)
            pr = prepare(
                ctx,
                model=tr.model,
                messages=tr.messages,
                max_tokens=tr.max_tokens,
                stream=tr.stream,
                passthrough=tr.passthrough,
                idempotency_key=None,
                channel=Channel.IDE,
            )
        except M.MessagesError as e:
            return anthropic_error(e.status, e.message)
        except Refusal as r:
            return anthropic_error(r.status, r.message, r.decision)
        except GatewayError as e:
            return anthropic_error(e.status, e.message)
        except CoreError as e:
            return anthropic_error(_status_for(e), str(e))
        if tr.stream:
            gen = _stream_translated(
                core,
                provider,
                pr.d,
                pr.res,
                pr.targets,
                pr.payload,
                pr.in_chars,
                t0,
                M.StreamTranslator(tr.model, "msg_" + pr.d.request_id.hex[:24], pr.in_chars // 4),
            )
            return _with_headers(StreamingResponse(gen, media_type="text/event-stream"), pr.d)
        try:
            data, text = _complete_data(
                core, provider, pr.d, pr.res, pr.targets, pr.payload, pr.in_chars, t0
            )
            msg = M.to_message(data, tr.model, "msg_" + pr.d.request_id.hex[:24], text, pr.in_chars)
        except GatewayError as e:
            return anthropic_error(e.status, e.message, pr.d)
        except M.MessagesError as e:
            return anthropic_error(e.status, e.message, pr.d)
        return _with_headers(JSONResponse(msg), pr.d)

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        """Approximate (4 characters per token) and not governed: no model is called."""
        from captain.integrations.api_gateway import messages as M

        try:
            anthropic_caller(authorization, x_api_key, None)
            raw = await request.json()
            tr = M.to_openai({**raw, "max_tokens": 1})
        except M.MessagesError as e:
            return anthropic_error(e.status, e.message)
        except CoreError as e:
            return anthropic_error(_status_for(e), str(e))
        chars = sum(len(str(m.get("content") or "")) for m in tr.messages)
        chars += len(json.dumps(tr.passthrough.get("tools", [])))
        return JSONResponse({"input_tokens": chars // 4})

    def responses_error(
        status: int, message: str, code: str | None = None, d: Decision | None = None
    ) -> JSONResponse:
        from captain.integrations.api_gateway import responses as RS

        body = {
            "error": {
                "message": message,
                "type": RS.error_type(status),
                "param": None,
                "code": code or RS.error_type(status),
            }
        }
        resp = JSONResponse(body, status_code=status)
        return _with_headers(resp, d) if d else resp

    @app.post("/v1/responses", response_model=None)
    async def responses(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
        x_forwarded_for: Annotated[str | None, Header()] = None,
    ) -> JSONResponse | StreamingResponse:
        from starlette.concurrency import run_in_threadpool

        try:
            raw = await request.json()
        except ValueError:
            return responses_error(400, "request body must be valid JSON")
        return await run_in_threadpool(
            handle_responses,
            raw,
            authorization,
            x_api_key,
            (x_forwarded_for or "").split(",")[0].strip() or None,
        )

    def handle_responses(
        raw: Any, authorization: str | None, x_api_key: str | None, ip: str | None
    ) -> JSONResponse | StreamingResponse:
        from captain.integrations.api_gateway import responses as RS

        t0 = time.monotonic()
        try:
            ctx = anthropic_caller(authorization, x_api_key, ip)
            if not isinstance(raw, dict):
                raise RS.ResponsesError("request body must be a JSON object")
            tr, echo_tools, echo_choice = RS.to_openai(raw)
            pr = prepare(
                ctx,
                model=tr.model,
                messages=tr.messages,
                max_tokens=tr.max_tokens,
                stream=tr.stream,
                passthrough=tr.passthrough,
                idempotency_key=None,
                channel=Channel.IDE,
            )
        except RS.ResponsesError as e:
            return responses_error(e.status, e.message, e.code)
        except Refusal as r:
            return responses_error(r.status, r.message, r.code, r.decision)
        except GatewayError as e:
            return responses_error(e.status, e.message, e.code)
        except CoreError as e:
            return responses_error(_status_for(e), str(e), e.reason_code.value)
        rid = "resp_" + pr.d.request_id.hex[:24]
        if tr.stream:
            gen = _stream_translated(
                core,
                provider,
                pr.d,
                pr.res,
                pr.targets,
                pr.payload,
                pr.in_chars,
                t0,
                RS.StreamTranslator(tr.model, rid, echo_tools, echo_choice, pr.in_chars // 4),
            )
            return _with_headers(StreamingResponse(gen, media_type="text/event-stream"), pr.d)
        try:
            data, text = _complete_data(
                core, provider, pr.d, pr.res, pr.targets, pr.payload, pr.in_chars, t0
            )
        except GatewayError as e:
            return responses_error(e.status, e.message, e.code, pr.d)
        out = RS.to_response(data, tr.model, rid, text, pr.in_chars, echo_tools, echo_choice)
        return _with_headers(JSONResponse(out), pr.d)

    @app.post("/v1/chat/completions", response_model=None)
    def chat(
        body: ChatBody,
        authorization: Annotated[str | None, Header()] = None,
        x_forwarded_for: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> JSONResponse | StreamingResponse:
        t0 = time.monotonic()
        if body.n != 1:
            raise GatewayError(
                400, "INVALID_REQUEST", "n > 1 is not supported (cost cannot be bounded)"
            )
        ctx = caller(authorization, (x_forwarded_for or "").split(",")[0].strip() or None)
        try:
            pr = prepare(
                ctx,
                model=body.model,
                messages=body.messages,
                max_tokens=body.max_completion_tokens or body.max_tokens,
                stream=body.stream,
                passthrough={k: v for k, v in (body.model_extra or {}).items() if k in PASSTHROUGH},
                idempotency_key=idempotency_key,
                channel=Channel.API,
            )
        except Refusal as r:
            return _with_headers(_error(r.status, r.code, r.message), r.decision)
        if body.stream:
            want_usage = bool((body.stream_options or {}).get("include_usage"))
            gen = _stream(
                core,
                provider,
                pr.d,
                pr.res,
                pr.targets,
                pr.payload,
                pr.in_chars,
                want_usage,
                body.model,
                t0,
            )
            return _with_headers(StreamingResponse(gen, media_type="text/event-stream"), pr.d)
        return _with_headers(
            _complete(
                core, provider, pr.d, pr.res, pr.targets, pr.payload, pr.in_chars, body.model, t0
            ),
            pr.d,
        )

    def check_texts(texts: list[Any]) -> None:
        if (
            not texts
            or len(texts) > MAX_EMBEDDING_INPUTS
            or any(not isinstance(t, str) for t in texts)
        ):
            raise GatewayError(
                400, "INVALID_REQUEST", f"input must be 1 to {MAX_EMBEDDING_INPUTS} text strings"
            )
        if sum(len(t) for t in texts) > MAX_EMBEDDING_CHARS:
            raise GatewayError(400, "INVALID_REQUEST", "input is too large")
        if getattr(provider, "embed", None) is None:
            raise GatewayError(501, "NOT_IMPLEMENTED", "this gateway's provider cannot embed")

    def run_embeddings(
        ctx: ResolvedContext,
        model: str,
        texts: list[str],
        dimensions: int | None,
        encoding_format: str | None,
        idempotency_key: str | None,
        t0: float,
    ) -> tuple[dict[str, Any], Decision]:
        """The one governed embeddings path every wire format uses: inspect and mask the TEXT
        before the provider sees it, reserve and settle on input tokens, record the call.
        Raises Refusal (policy/budget) or GatewayError (provider)."""
        embed = provider.embed  # type: ignore[attr-defined]  # check_texts ensured it exists
        pr = prepare(
            ctx,
            model=model,
            messages=[{"role": "user", "content": t} for t in texts],
            max_tokens=1,
            stream=False,
            passthrough={},
            idempotency_key=idempotency_key,
            channel=Channel.API,
        )
        payload: dict[str, Any] = {"input": [str(m["content"]) for m in pr.payload["messages"]]}
        if encoding_format:
            payload["encoding_format"] = encoding_format
        if dimensions:
            payload["dimensions"] = dimensions
        last: ProviderError | None = None
        for target in pr.targets:
            try:
                data = embed(target, payload)
            except ProviderError as e:
                last = e
                if e.retryable:
                    continue
                break
            _settle(core, pr.res, target, data.get("usage"), pr.in_chars, 0, t0)
            data["model"] = model
            return data, pr.d
        core.release(pr.res)
        raise GatewayError(
            502,
            ReasonCode.PROVIDER_ERROR.value,
            f"All provider routes failed ({last.status if last else 'n/a'}).",
        )

    @app.post("/v1/embeddings", response_model=None)
    def embeddings(
        body: EmbeddingsBody,
        authorization: Annotated[str | None, Header()] = None,
        x_forwarded_for: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> JSONResponse:
        """Embeddings go through the same governance as a chat call: the TEXT being embedded
        is inspected (and masked if the policy says so) BEFORE it reaches the provider, the
        budget is reserved and settled on input tokens, and the call is recorded. Token-id
        inputs are refused: they cannot be inspected."""
        t0 = time.monotonic()
        texts = [body.input] if isinstance(body.input, str) else list(body.input)
        check_texts(texts)
        ctx = caller(authorization, (x_forwarded_for or "").split(",")[0].strip() or None)
        try:
            data, d = run_embeddings(
                ctx, body.model, texts, body.dimensions, body.encoding_format, idempotency_key, t0
            )
        except Refusal as r:
            return _with_headers(_error(r.status, r.code, r.message), r.decision)
        return _with_headers(JSONResponse(data), d)

    def gemini_error(status: int, message: str, d: Decision | None = None) -> JSONResponse:
        from captain.integrations.api_gateway import gemini as G

        resp = JSONResponse(G.error_body(status, message), status_code=status)
        return _with_headers(resp, d) if d else resp

    @app.post("/v1beta/models/{model_action:path}", response_model=None)
    @app.post("/v1/models/{model_action:path}", response_model=None)  # SDK default version
    async def gemini(
        model_action: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_goog_api_key: Annotated[str | None, Header()] = None,
        x_forwarded_for: Annotated[str | None, Header()] = None,
    ) -> JSONResponse | StreamingResponse:
        """Google Gemini native API (`generateContent`, `streamGenerateContent?alt=sse`).
        The key goes in the `x-goog-api-key` header (or Authorization: Bearer), never the
        `?key=` query string, so it does not end up in access logs."""
        from starlette.concurrency import run_in_threadpool

        try:
            raw = await request.json()
        except ValueError:
            return gemini_error(400, "request body must be valid JSON")
        if x_goog_api_key is None and authorization is None and "key" in request.query_params:
            return gemini_error(
                401, "send the Captain key in the x-goog-api-key header, not the ?key= query string"
            )
        return await run_in_threadpool(
            handle_gemini,
            raw,
            model_action,
            request.query_params.get("alt"),
            authorization or (f"Bearer {x_goog_api_key}" if x_goog_api_key else None),
            (x_forwarded_for or "").split(",")[0].strip() or None,
        )

    def handle_gemini(
        raw: Any, model_action: str, alt: str | None, authorization: str | None, ip: str | None
    ) -> JSONResponse | StreamingResponse:
        from captain.integrations.api_gateway import gemini as G

        t0 = time.monotonic()
        model, _, action = model_action.rpartition(":")
        try:
            ctx = caller(authorization, ip)
            known = (
                "generateContent",
                "streamGenerateContent",
                "embedContent",
                "batchEmbedContents",
                "countTokens",
            )
            if not model or action not in known:
                raise G.GeminiError(f"unsupported method: {model_action!r}", 404)
            if not isinstance(raw, dict):
                raise G.GeminiError("request body must be a JSON object")
            bare = model.removeprefix("models/")
            if action == "countTokens":
                # Approximate and not governed (no model is called); the key is still required.
                return JSONResponse(G.count_tokens(raw, bare))
            if action in ("embedContent", "batchEmbedContents"):
                texts, dims = G.embed_request(raw, action == "embedContent")
                check_texts(texts)
                data, d = run_embeddings(ctx, bare, texts, dims, None, None, t0)
                return _with_headers(
                    JSONResponse(G.embed_response(data, action == "embedContent")), d
                )
            stream = action == "streamGenerateContent"
            if stream and alt != "sse":
                raise G.GeminiError("streaming is supported with ?alt=sse only")
            tr = G.to_openai(raw, model.removeprefix("models/"))
            pr = prepare(
                ctx,
                model=tr.model,
                messages=tr.messages,
                max_tokens=tr.max_tokens,
                stream=stream,
                passthrough=tr.passthrough,
                idempotency_key=None,
                channel=Channel.API,
            )
        except G.GeminiError as e:
            return gemini_error(e.status, e.message)
        except Refusal as r:
            return gemini_error(r.status, r.message, r.decision)
        except GatewayError as e:
            return gemini_error(e.status, e.message)
        except CoreError as e:
            return gemini_error(_status_for(e), str(e))
        rid = "resp_" + pr.d.request_id.hex[:24]
        if stream:
            gen = _stream_translated(
                core, provider, pr.d, pr.res, pr.targets, pr.payload, pr.in_chars, t0,
                G.GeminiStream(tr.model, rid),
            )  # fmt: skip
            return _with_headers(StreamingResponse(gen, media_type="text/event-stream"), pr.d)
        try:
            data, text = _complete_data(
                core, provider, pr.d, pr.res, pr.targets, pr.payload, pr.in_chars, t0
            )
            body = G.to_response(data, tr.model, rid, text, pr.in_chars)
        except GatewayError as e:
            return gemini_error(e.status, e.message, pr.d)
        except G.GeminiError as e:
            return gemini_error(e.status, e.message, pr.d)
        return _with_headers(JSONResponse(body), pr.d)

    @app.post("/v1/browser/check", response_model=None)
    def browser_check(
        body: BrowserCheckBody,
        authorization: Annotated[str | None, Header()] = None,
        x_forwarded_for: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        """Pre-submit check for the browser extension: would Captain allow this text to be
        sent to this AI website? The text is inspected (secrets, personal data, injection)
        and the answer is ALLOW, ALLOW_WITH_REDACTION (masked text returned) or BLOCK. The
        decision is recorded on the BROWSER channel; the text itself is never stored.

        Honest limit: this governs what the extension submits through it. It cannot see
        another browser, another profile, a direct API call or a desktop app."""
        ctx = caller(authorization, (x_forwarded_for or "").split(",")[0].strip() or None)
        req = GovernanceRequest(
            trace_id=f"br-{uuid4().hex[:12]}",
            context=ctx,
            channel=Channel.BROWSER,
            action=ActionType.WEB_SUBMIT,
            contents=(ContentRef(text=body.text, trusted=True),),
        )
        d = core.decide(req)
        masked: str | None = None
        if d.permits_execution and d.redacted_contents:
            masked = d.redacted_contents[0].text
        allowed = d.permits_execution and d.decision is not DecisionType.REQUIRE_APPROVAL
        message = d.explanation
        if d.decision is DecisionType.REQUIRE_APPROVAL:
            message = "This needs human approval, which a web page cannot wait for."
        resp = JSONResponse(
            {
                "decision": d.decision.value if allowed else "BLOCK",
                "allowed": allowed,
                "reason_code": d.reason_code.value,
                "message": message,
                "masked_text": masked,
                "site": body.site,
                "request_id": str(d.request_id),
            }
        )
        return _with_headers(resp, d)

    @app.post("/v1/desktop/report")
    def desktop_report(
        body: DesktopReport,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Daily report from the Windows desktop agent (ADR-006): AI apps installed or
        running, and MCP servers configured to bypass Captain. Observe-only metadata; the
        caller's key must belong to a principal with the `desktop-agent` role."""
        ctx = caller(authorization, None)
        if "desktop-agent" not in ctx.principal.roles:
            raise GatewayError(
                403, ReasonCode.AUTH_INVALID.value, "this key may not send desktop reports"
            )
        hosts = {*captain_hosts, request.url.hostname or ""} - {""}
        records, via_captain = records_from_report(body, hosts)
        result = import_records(core, records, ActorMap(desktop_actors))
        if result.failed:
            raise GatewayError(
                503, ReasonCode.CONTROL_UNAVAILABLE.value, "evidence could not be recorded"
            )
        return {
            "recorded": result.recorded,
            "apps": sum(1 for r in records if r.vendor == "desktop:inventory"),
            "bypass_signals": sum(1 for r in records if r.vendor == "desktop:mcp-config"),
            "via_captain": via_captain,
        }

    return app


R = TypeVar("R", JSONResponse, StreamingResponse)


def _with_headers(resp: R, d: Decision) -> R:
    resp.headers["X-Captain-Request-Id"] = str(d.request_id)
    resp.headers["X-Captain-Decision"] = d.decision.value
    if d.risk_level:
        resp.headers["X-Captain-Risk"] = d.risk_level.value
    resp.headers["X-Captain-Policy"] = f"{d.policy_bundle}@{d.policy_version}"
    return resp


def _settle(
    core: GatewayCoreAPI,
    res: Reservation,
    target: ProviderTarget,
    usage: dict[str, Any] | None,
    in_chars: int,
    out_chars: int,
    t0: float,
    ttft_ms: int | None = None,
) -> None:
    p = core.price_usage(target, "openai", usage, in_chars, out_chars)
    core.settle(
        res,
        ActualUsage(
            usage=p.usage,
            source=p.source,
            cost=p.cost,
            price_version=p.price_version,
            latency_ms=int((time.monotonic() - t0) * 1000),
            time_to_first_token_ms=ttft_ms,
            provider=target.provider_id,
        ),
    )


def _record_output_block(core: GatewayCoreAPI, d: Decision, entity: str) -> None:
    from captain.core.models import UsageEvent

    core.record(
        UsageEvent(
            request_id=d.request_id,
            trace_id=d.trace_id,
            event_type=EventType.CALL_REFUSED,
            channel=d.channel,
            coverage=CoverageClass.GOVERNED,
            principal_id=d.principal_id,
            register_id=d.register_id,
            decision=DecisionType.BLOCK,
            reason_code=ReasonCode.OUTPUT_BLOCKED,
            model=d.model,
            details={"blocked_entity": entity, "stage": "output"},
        )
    )


def _complete_data(
    core: GatewayCoreAPI,
    provider: ProviderClient,
    d: Decision,
    res: Reservation,
    targets: list[ProviderTarget],
    payload: dict[str, Any],
    in_chars: int,
    t0: float,
) -> tuple[dict[str, Any], str]:
    """Non-streaming provider call with fallbacks, output guard and settlement. Returns
    (provider reply, guarded text). Raises GatewayError when the call is refused or fails."""
    last: ProviderError | None = None
    for target in targets:
        try:
            data = provider.complete(target, payload)
        except ProviderError as e:
            last = e
            log.warning(
                "provider call failed: request=%s target=%s/%s status=%s retryable=%s: %s",
                d.request_id,
                target.provider_id,
                target.alias,
                e.status,
                e.retryable,
                e,
            )
            if e.retryable:
                continue  # fallback to the next route target
            break
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        text = str(msg.get("content") or "")
        guard = core.output_guard(d)
        try:
            checked = "".join(guard.feed(text)) + "".join(guard.finish())
        except OutputBlockedError as e:
            _settle(
                core, res, target, data.get("usage"), in_chars, len(text), t0
            )  # provider already charged
            _record_output_block(core, d, e.entity)
            raise GatewayError(
                403,
                ReasonCode.OUTPUT_BLOCKED.value,
                "The model response was blocked by output guardrails.",
            ) from e
        _settle(core, res, target, data.get("usage"), in_chars, len(text), t0)
        core.evaluate(d, payload["messages"], checked)
        return data, checked
    core.release(res)
    raise GatewayError(
        502,
        ReasonCode.PROVIDER_ERROR.value,
        f"All provider routes failed ({last.status if last else 'n/a'}).",
    )


def _complete(
    core: GatewayCoreAPI,
    provider: ProviderClient,
    d: Decision,
    res: Reservation,
    targets: list[ProviderTarget],
    payload: dict[str, Any],
    in_chars: int,
    alias: str,
    t0: float,
) -> JSONResponse:
    try:
        data, checked = _complete_data(core, provider, d, res, targets, payload, in_chars, t0)
    except GatewayError as e:
        return _error(e.status, e.code, e.message)
    data["model"] = alias  # the caller sees the Captain alias, not the provider id
    data["choices"][0]["message"]["content"] = checked
    return JSONResponse(data)


def _stream_translated(
    core: GatewayCoreAPI,
    provider: ProviderClient,
    d: Decision,
    res: Reservation,
    targets: list[ProviderTarget],
    payload: dict[str, Any],
    in_chars: int,
    t0: float,
    tr: Any,
) -> Iterator[str]:
    """Same governed stream as `_stream`, emitted by a wire-format translator `tr` (Anthropic
    Messages or OpenAI Responses): start(), text(), tool_call(), end(usage), error()."""
    from captain.integrations.api_gateway import messages as M

    guard = core.output_guard(d)
    said: list[str] = []  # guarded (masked) output, kept in memory for live evaluation only
    usage: dict[str, Any] | None = None
    out_chars, ttft, settled = 0, None, False
    target = targets[0]
    try:
        chunks: Iterator[dict[str, Any]] | None = None
        first: dict[str, Any] | None = None
        for target in targets:  # fall back only before anything reaches the caller
            try:
                chunks = provider.stream(target, payload)
                first = next(chunks, None)
                break
            except ProviderError as e:
                log.warning(
                    "provider stream failed to start: request=%s target=%s/%s status=%s: %s",
                    d.request_id,
                    target.provider_id,
                    target.alias,
                    e.status,
                    e,
                )
                if not e.retryable:
                    break
                chunks = None
        if chunks is None:
            core.release(res)
            settled = True
            yield tr.error("All provider routes failed.")
            return
        yield tr.start()

        def provider_chunks() -> Iterator[dict[str, Any]]:
            if first is not None:
                yield first
            assert chunks is not None
            yield from chunks

        for ch in provider_chunks():
            if ch.get("usage"):
                usage = ch["usage"]
            for c in ch.get("choices") or []:
                delta = c.get("delta") or {}
                if c.get("finish_reason"):
                    tr.finish = c["finish_reason"]
                text = delta.get("content")
                if text:
                    out_chars += len(text)
                    for piece in guard.feed(text):
                        if ttft is None:
                            ttft = int((time.monotonic() - t0) * 1000)
                        said.append(piece)
                        yield from tr.text(piece)
                for tc in delta.get("tool_calls") or []:
                    out_chars += len((tc.get("function") or {}).get("arguments") or "")
                    if ttft is None:
                        ttft = int((time.monotonic() - t0) * 1000)
                    yield from tr.tool_call(tc)
        for piece in guard.finish():
            if ttft is None:
                ttft = int((time.monotonic() - t0) * 1000)
            said.append(piece)
            yield from tr.text(piece)
        yield from tr.end(M.usage_of(usage, in_chars, out_chars))
        _settle(core, res, target, usage, in_chars, out_chars, t0, ttft)
        settled = True
        core.evaluate(d, payload["messages"], "".join(said))
    except OutputBlockedError as e:
        _settle(core, res, target, usage, in_chars, out_chars, t0, ttft)
        settled = True
        _record_output_block(core, d, e.entity)
        yield tr.error("Response stopped by output guardrails.")
    except ProviderError:
        _settle(core, res, target, usage, in_chars, out_chars, t0, ttft)
        settled = True
        yield tr.error("Provider stream failed.")
    finally:
        if not settled:  # client disconnected: charge what was produced (estimated)
            _settle(core, res, target, usage, in_chars, out_chars, t0, ttft)


def _sse_error(code: ReasonCode, message: str) -> str:
    return "data: " + json.dumps({"error": {"code": code.value, "message": message}}) + "\n\n"


def _chunk(
    alias: str,
    content: str | None = None,
    finish: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> str:
    delta: dict[str, Any] = {"content": content} if content else {}
    if tool_calls:
        delta["tool_calls"] = tool_calls
    obj = {
        "id": "chatcmpl-captain",
        "object": "chat.completion.chunk",
        "model": alias,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(obj)}\n\n"


def _stream(
    core: GatewayCoreAPI,
    provider: ProviderClient,
    d: Decision,
    res: Reservation,
    targets: list[ProviderTarget],
    payload: dict[str, Any],
    in_chars: int,
    want_usage: bool,
    alias: str,
    t0: float,
) -> Iterator[str]:
    guard = core.output_guard(d)
    said: list[str] = []  # guarded (masked) output, kept in memory for live evaluation only
    usage: dict[str, Any] | None = None
    out_chars, ttft, settled = 0, None, False
    target = targets[0]
    try:
        chunks: Iterator[dict[str, Any]] | None = None
        first: dict[str, Any] | None = None
        for target in targets:  # fall back only before the first chunk reaches the caller
            try:
                chunks = provider.stream(target, payload)
                first = next(chunks, None)
                break
            except ProviderError as e:
                log.warning(
                    "provider stream failed to start: request=%s target=%s/%s status=%s: %s",
                    d.request_id,
                    target.provider_id,
                    target.alias,
                    e.status,
                    e,
                )
                if not e.retryable:
                    break
                chunks = None
        if chunks is None:
            core.release(res)
            settled = True
            yield _sse_error(ReasonCode.PROVIDER_ERROR, "All provider routes failed.")
            yield "data: [DONE]\n\n"
            return

        def provider_chunks() -> Iterator[dict[str, Any]]:
            if first is not None:
                yield first
            assert chunks is not None
            yield from chunks

        finish_reason = "stop"
        for ch in provider_chunks():
            if ch.get("usage"):
                usage = ch["usage"]
            for c in ch.get("choices") or []:
                delta = c.get("delta") or {}
                text = delta.get("content")
                if text:
                    out_chars += len(text)
                    for piece in guard.feed(text):
                        if ttft is None:
                            ttft = int((time.monotonic() - t0) * 1000)
                        said.append(piece)
                        yield _chunk(alias, piece)
                calls = delta.get("tool_calls")
                if calls:
                    # The model is asking the CLIENT to run a tool. Pass the request through
                    # (it costs output tokens) so function calling works; Captain does not
                    # execute it. Tool arguments are not run through the text output guard;
                    # what the tool may do is governed where it runs (for example the MCP
                    # proxy), not here.
                    out_chars += sum(
                        len(str((tc.get("function") or {}).get("arguments") or "")) for tc in calls
                    )
                    if ttft is None:
                        ttft = int((time.monotonic() - t0) * 1000)
                    yield _chunk(alias, tool_calls=calls)
                if c.get("finish_reason") in ("tool_calls", "length"):
                    finish_reason = str(c["finish_reason"])
        for piece in guard.finish():
            if ttft is None:  # buffered tiers release their first output here
                ttft = int((time.monotonic() - t0) * 1000)
            said.append(piece)
            yield _chunk(alias, piece)
        yield _chunk(alias, finish=finish_reason)
        _settle(core, res, target, usage, in_chars, out_chars, t0, ttft)
        settled = True
        core.evaluate(d, payload["messages"], "".join(said))
        if want_usage and usage:
            tail = {"object": "chat.completion.chunk", "choices": [], "usage": usage}
            yield "data: " + json.dumps(tail) + "\n\n"
        yield "data: [DONE]\n\n"
    except OutputBlockedError as e:
        _settle(core, res, target, usage, in_chars, out_chars, t0, ttft)
        settled = True
        _record_output_block(core, d, e.entity)
        yield _sse_error(ReasonCode.OUTPUT_BLOCKED, "Response stopped by output guardrails.")
        yield "data: [DONE]\n\n"
    except ProviderError:
        _settle(
            core, res, target, usage, in_chars, out_chars, t0, ttft
        )  # partial output was produced
        settled = True
        yield _sse_error(ReasonCode.PROVIDER_ERROR, "Provider stream failed.")
        yield "data: [DONE]\n\n"
    finally:
        if not settled:  # client disconnected: charge what was produced (estimated)
            _settle(core, res, target, usage, in_chars, out_chars, t0, ttft)
