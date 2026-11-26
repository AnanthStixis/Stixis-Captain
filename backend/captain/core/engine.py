"""CaptainCore — the real Core Decision API implementation, composing the 8 modules.

decide() order:
  1. Kill switch (any matching scope)            → BLOCK KILL_SWITCH_ACTIVE
  2. Guardrails (detect only)                    → findings; guard down → tier fail mode
  3. OPA policy (tier × autonomy)                → structured decision; OPA down → tier fail mode
  4. Redaction, if the policy says so
  5. Evidence: the decision is queued to the ledger; if that fails, Tier 1–2 are refused
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from captain.core.approvals.service import ApprovalService
from captain.core.budget.service import BudgetService, RateLimitedError
from captain.core.budget.store import BudgetStoreUnavailable, UnknownReservation
from captain.core.context.service import ContextService
from captain.core.decision_api import (
    ApprovalError,
    AuthenticationError,
    BudgetExceededError,
    ContextUnresolvedError,
    CoreError,
    EvidenceUnavailableError,
    ModelNotPricedError,
    NoRouteError,
)
from captain.core.guardrails.masking import MaskMap
from captain.core.guardrails.service import GuardrailService, apply_redaction
from captain.core.guardrails.streaming import StreamGuard
from captain.core.identity.killswitch import KillScope, KillSwitch
from captain.core.identity.oidc import WorkloadAuthenticator
from captain.core.identity.service import IdentityService
from captain.core.ledger.service import LedgerService
from captain.core.models import (
    ActionType,
    ActualUsage,
    ApprovalState,
    ApprovalTicket,
    Channel,
    ContentRef,
    ContextHints,
    CostEstimate,
    CoverageClass,
    Credential,
    Decision,
    DecisionType,
    EventType,
    FindingType,
    GovernanceRequest,
    GuardrailFinding,
    LedgerReceipt,
    Limits,
    PricedUsage,
    ProviderTarget,
    ReasonCode,
    Reservation,
    ResolvedContext,
    ScopeUsage,
    Settlement,
    TokenUsage,
    ToolDescriptor,
    ToolOutputVerdict,
    ToolTarget,
    UsageEvent,
    UsageSource,
)
from captain.core.policy.opa import PolicyEngine, PolicyUnavailableError, build_input
from captain.core.registry.service import RegistryService
from captain.core.risk import RiskAssessment, ToolFacts, assess
from captain.core.usage.normalize import normalize

if TYPE_CHECKING:
    from captain.core.evaluation.service import EvaluationService
from captain.core.usage.pricing import (
    ModelPrice,
    PriceCatalog,
    PriceNotFoundError,
    cost_of,
    estimate_cost,
    estimate_input_tokens,
)

CORE_BUNDLE = "captain-core"
CORE_VERSION = "core"
INJECTION_BLOCK_SCORE = 0.8  # tool output scoring at or above this is refused at Tier 1–2

_EVENT_FOR = {
    DecisionType.ALLOW: EventType.CALL_ALLOWED,
    DecisionType.ALLOW_WITH_LIMITS: EventType.CALL_ALLOWED,
    DecisionType.ALLOW_WITH_REDACTION: EventType.CALL_MASKED,
    DecisionType.BLOCK: EventType.CALL_REFUSED,
    DecisionType.REQUIRE_APPROVAL: EventType.TOOL_HELD,
    DecisionType.OBSERVE_ONLY: EventType.CALL_OBSERVED,
}


@dataclass
class CoreModules:
    identity: IdentityService
    context: ContextService
    killswitch: KillSwitch
    guardrails: GuardrailService
    policy: PolicyEngine
    budget: BudgetService
    approvals: ApprovalService
    ledger: LedgerService
    workload: WorkloadAuthenticator | None = None
    registry: RegistryService | None = None
    prices: PriceCatalog | None = None
    evaluation: EvaluationService | None = None  # live evaluation (ADR-005); None = off


def summarize_findings(findings: tuple[GuardrailFinding, ...]) -> str | None:
    """What the guardrails found, as kinds and counts only, e.g. "PII:EMAIL_ADDRESS x2,
    SECRET:OPENAI_API_KEY x1". Never the matched text or its position."""
    if not findings:
        return None
    counts: dict[str, int] = {}
    for f in findings:
        # The guard model reports a score for every request; only a likely hit is a finding
        # worth showing (0.5 is the lowest threshold any tier's policy uses).
        if f.type in (FindingType.INJECTION, FindingType.UNSAFE) and f.score < 0.5:
            continue
        k = f"{f.type.value}:{f.entity}"
        counts[k] = counts.get(k, 0) + 1
    return ", ".join(f"{k} x{n}" for k, n in sorted(counts.items()))[:500] or None


def budget_scopes(
    ctx: ResolvedContext, model: str | None, tool: str | None = None
) -> tuple[str, ...]:
    p = ctx.principal
    scopes = [
        f"org:{p.org_id}",
        f"cost_centre:{ctx.register_entry.cost_centre}",
        *([f"team:{p.team_id}"] if p.team_id else []),
        f"project:{ctx.project_id}",
        f"app:{ctx.application_id}",
        f"principal:{p.id}",
        *([f"key:{ctx.key_id}"] if ctx.key_id else []),
        *([f"model:{model}"] if model else []),
        # Per-tool limits: one counter for the tool across everyone, one per caller and tool.
        *([f"tool:{tool}", f"ptool:{p.id}:{tool}"] if tool else []),
    ]
    return tuple(scopes)


class MaskVault:
    """Per-request placeholder→value maps, kept in memory only for a short TTL so a
    response can be un-masked for the same caller. Never persisted or logged. One
    instance per process: restore must happen on the instance that masked (the API
    Gateway handles request and response in the same process)."""

    def __init__(self, ttl_s: float = 600) -> None:
        self._ttl = ttl_s
        self._maps: dict[UUID, tuple[float, str, MaskMap]] = {}
        self._lock = threading.Lock()

    def new(self, request_id: UUID, principal_id: str) -> MaskMap:
        m = MaskMap()
        now = time.monotonic()
        with self._lock:
            self._maps = {k: v for k, v in self._maps.items() if now - v[0] < self._ttl}
            self._maps[request_id] = (now, principal_id, m)
        return m

    def get(self, request_id: UUID, principal_id: str) -> MaskMap | None:
        with self._lock:
            hit = self._maps.get(request_id)
        if hit is None or time.monotonic() - hit[0] >= self._ttl or hit[1] != principal_id:
            return None
        return hit[2]

    def forget(self, request_id: UUID) -> None:
        with self._lock:
            self._maps.pop(request_id, None)


_TIER_STREAM_DEFAULT = {
    "TIER_1": "buffer",
    "TIER_2": "chunk",
    "TIER_3": "passthrough_async_check",
    "TIER_4": "passthrough",
}


class CaptainCore:
    def __init__(self, m: CoreModules, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.m = m
        self._now = clock
        self._masks = MaskVault()
        self._sec_seen: dict[tuple[str, str], float] = {}
        self._sec_lock = threading.Lock()
        self._sec_recent: deque[float] = deque()
        self._sec_suppressed = 0

    # ============================================================ responses
    def restore(self, decision: Decision, text: str, principal_id: str) -> str:
        """Put the caller's own masked values back into a response. Only the principal
        whose request was masked can restore, and only while the map is alive."""
        if decision.decision is not DecisionType.ALLOW_WITH_REDACTION:
            return text
        m = self._masks.get(decision.request_id, principal_id)
        if m is None:
            raise CoreError("mask map expired or not owned by caller", ReasonCode.PII_REDACTED)
        return m.restore(text)

    # ============================================================== gateway
    def route(self, decision: Decision) -> list[ProviderTarget]:
        """Ordered provider targets (primary, then fallbacks) for an allowed model call."""
        if not decision.permits_execution or not decision.model:
            raise NoRouteError("decision does not permit a model call")
        if self.m.registry is None:
            raise NoRouteError("no Model/Tool Registry configured")
        out = [
            ProviderTarget(
                alias=t.alias,
                provider_id=t.provider.id,
                provider_kind=t.provider.kind.value,
                provider_model=t.model.provider_model,
                credential_ref=t.provider.credential_ref,
                base_url=t.provider.base_url,
            )
            for t in self.m.registry.resolve_route(decision.model)
        ]
        if not out:
            raise NoRouteError(f"no enabled provider route for '{decision.model}'")
        return out

    def _price(self, alias: str) -> ModelPrice:
        if self.m.prices is None:
            raise ModelNotPricedError("no price catalogue configured")
        try:
            return self.m.prices.price_at(alias)
        except PriceNotFoundError as e:
            raise ModelNotPricedError(f"no price for '{alias}'; budget cannot be reserved") from e

    def estimate_cost(
        self, decision: Decision, input_chars: int, max_output_tokens: int
    ) -> CostEstimate:
        """Upper-bound cost for budget reservation. The output bound is the smaller of
        the request's max tokens and the tier cap from policy."""
        if not decision.model:
            raise ModelNotPricedError("decision has no model")
        cap = decision.limits.max_output_tokens
        out = min(max_output_tokens, cap) if cap else max_output_tokens
        return estimate_cost(self._price(decision.model), estimate_input_tokens(input_chars), out)

    def price_usage(
        self,
        target: ProviderTarget,
        usage_format: str,
        raw_usage: dict[str, object] | None,
        fallback_input_chars: int,
        fallback_output_chars: int,
    ) -> PricedUsage:
        """Cost the actual call. Provider-reported usage when present; otherwise an
        explicit ESTIMATE from text length — never presented as exact."""
        price = self._price(target.alias)
        norm = normalize(usage_format, raw_usage)
        if norm is not None:
            usage, cache_writes = norm
            source = UsageSource.PROVIDER_REPORTED
        else:
            usage = TokenUsage(
                input_tokens=estimate_input_tokens(fallback_input_chars),
                output_tokens=estimate_input_tokens(fallback_output_chars),
            )
            cache_writes, source = 0, UsageSource.ESTIMATED
        return PricedUsage(
            usage=usage,
            cost=cost_of(usage, price, cache_writes),
            price_version=price.version,
            source=source,
        )

    def output_guard(self, decision: Decision) -> StreamGuard:
        return self.stream_guard(decision)

    # ================================================================== usage
    def usage(self, ctx: ResolvedContext) -> list[ScopeUsage]:
        """Budgets that apply to this caller and how much of each is used this period."""
        scopes = budget_scopes(ctx, None)
        return [
            ScopeUsage(
                scope=d.scope,
                period=d.period.value,
                limit=d.limit,
                spent=st.spent,
                reserved=st.reserved,
            )
            for d, st in self.m.budget.usage_for(scopes)
        ]

    # ================================================================== MCP
    def _tool_server(self, tool: str | None) -> str | None:
        if not tool or self.m.registry is None:
            return None
        t = self.m.registry.usable_tool(tool)
        return t.server_id if t else None

    def list_tools(self, ctx: ResolvedContext) -> list[ToolDescriptor]:
        """Tools this caller may see: allowed by its register entry AND enabled in the
        registry. Seeing a tool is not permission to call it; every call is decided."""
        if self.m.registry is None:
            return []
        out = []
        for t in sorted(self.m.registry.all_tools(), key=lambda x: x.name):
            if t.name in ctx.register_entry.allowed_tools and self.m.registry.usable_tool(t.name):
                out.append(
                    ToolDescriptor(
                        name=t.name,
                        description=t.description,
                        input_schema=t.input_schema,
                        read_only=t.read_only,
                        impact=t.impact.value,
                    )
                )
        return out

    def screen_tool_output(self, request: GovernanceRequest, text: str) -> ToolOutputVerdict:
        """Tool results are untrusted input to an agent. Tier 1–2 refuse secrets and
        likely prompt injection and mask personal data; Tier 3–4 pass the text through
        but report the same findings so evidence shows them."""
        tier = request.context.register_entry.risk_tier
        content = ContentRef(text=text, trusted=False)
        report = self.m.guardrails.inspect((content,), tier)
        names = {f"{f.type.value}:{f.entity}" for f in report.findings}
        if report.oversize:
            return ToolOutputVerdict(text="", blocked=True, entities=("SIZE_LIMIT",))
        if report.guard_unavailable and tier.fails_closed:
            return ToolOutputVerdict(text="", blocked=True, entities=("GUARD_UNAVAILABLE",))
        entities = tuple(sorted(names))
        if not tier.fails_closed:
            return ToolOutputVerdict(text=text, entities=entities)
        hard = [
            f
            for f in report.findings
            if f.type is FindingType.SECRET
            or (f.type is FindingType.INJECTION and f.score >= INJECTION_BLOCK_SCORE)
        ]
        if hard:
            return ToolOutputVerdict(text="", blocked=True, entities=entities)
        pii = [f for f in report.findings if f.type is FindingType.PII]
        if pii:
            masked = apply_redaction((content,), pii, MaskMap())[0].text or ""
            return ToolOutputVerdict(text=masked, masked=True, entities=entities)
        return ToolOutputVerdict(text=text, entities=entities)

    def tool_target(self, ctx: ResolvedContext, tool: str) -> ToolTarget:
        reg = self.m.registry
        t = reg.usable_tool(tool) if reg else None
        server = reg.server(t.server_id) if reg and t else None
        if t is None or server is None or tool not in ctx.register_entry.allowed_tools:
            raise NoRouteError(f"no enabled server for tool '{tool}'")
        return ToolTarget(
            tool=t.name,
            server_id=server.id,
            upstream_name=t.upstream_name or t.name.split(".", 1)[-1],
            url=server.url,
            transport=server.transport,
            credential_ref=server.credential_ref,
        )

    def stream_guard(self, decision: Decision) -> StreamGuard:
        """Output checker for a streamed response, in the mode the policy chose for the
        tier (CTO F3). New PII produced by the model is masked with the same map."""
        mode = decision.limits.streaming_mode or _TIER_STREAM_DEFAULT[decision.risk_tier.value]
        mapping = self._masks.get(decision.request_id, decision.principal_id) or MaskMap()
        return StreamGuard(mode, mapping, pii=self.m.guardrails.pii_detector)

    def finish_response(self, decision: Decision) -> None:
        self._masks.forget(decision.request_id)

    def evaluate(self, decision: Decision, messages: list[dict[str, Any]], answer: str) -> None:
        """Offer a finished response (masked prompt and masked answer) to live evaluation.
        Tier 1-2 calls are sampled and scored by the in-perimeter judge in the background;
        content stays in memory only. Never raises and never delays the caller."""
        if self.m.evaluation is not None:
            self.m.evaluation.consider(decision, messages, answer)

    # ================================================================ resolve
    def resolve(self, credential: Credential, hints: ContextHints) -> ResolvedContext:
        principal_id: str | None = None
        try:
            if credential.kind == "virtual_key":
                key = self.m.identity.authenticate(credential.secret, hints.client_ip)
            elif credential.kind == "workload_oidc" and self.m.workload is not None:
                key = self.m.workload.authenticate(credential.secret)
            else:
                # Interactive SSO sessions belong to the admin API (dashboard), not the Core.
                raise AuthenticationError(f"credential kind {credential.kind} not enabled")
            principal_id = key.principal_id
            principal = self.m.identity.principal(key.principal_id)
            return self.m.context.resolve(key, principal, hints)
        except (AuthenticationError, ContextUnresolvedError) as e:
            self._security_event(e, principal_id, hints)
            raise

    def _security_event(self, e: CoreError, principal_id: str | None, hints: ContextHints) -> None:
        """A request that cannot be attributed to a register entry (unknown, revoked or
        expired key; retired system; bad context) still leaves evidence, in the shared
        'security' partition. Never the credential. Rate-limited per reason and subject so
        a flood of bad keys cannot fill the ledger; best effort: it never changes the
        refusal the caller gets."""
        # The client address comes from a header the caller controls: accept only a real IP
        # address (so it cannot be used to write arbitrary text, or to mint endless distinct
        # "subjects" that dodge the per-subject limit).
        ip: str | None = None
        if hints.client_ip:
            try:
                ip = str(ipaddress.ip_address(hints.client_ip.strip()))
            except ValueError:
                ip = "invalid"
        subject = principal_id or ip or "-"
        now = time.monotonic()
        k = (e.reason_code.value, subject)
        with self._sec_lock:
            if now - self._sec_seen.get(k, -1e9) < SECURITY_EVENT_INTERVAL_S:
                return
            # A global ceiling too: however many different sources attack at once, the
            # ledger takes at most this many security events a minute. The rest are counted
            # and reported on the next event that gets through.
            while self._sec_recent and now - self._sec_recent[0] > 60:
                self._sec_recent.popleft()
            if len(self._sec_recent) >= SECURITY_EVENTS_PER_MINUTE:
                self._sec_suppressed += 1
                return
            self._sec_recent.append(now)
            if len(self._sec_seen) > 10_000:
                self._sec_seen.clear()
            self._sec_seen[k] = now
            suppressed, self._sec_suppressed = self._sec_suppressed, 0
        try:
            self.m.ledger.enqueue(
                UsageEvent(
                    trace_id=f"security:{e.reason_code.value.lower()}",
                    event_type=EventType.CALL_REFUSED,
                    channel=Channel.API,
                    coverage=CoverageClass.GOVERNED,
                    principal_id=principal_id,
                    register_id=SECURITY_PARTITION,
                    decision=DecisionType.BLOCK,
                    reason_code=e.reason_code,
                    details={
                        "stage": "authentication"
                        if isinstance(e, AuthenticationError)
                        else "context",
                        "message": str(e)[:200],
                        "client_ip": ip,
                        "suppressed_since_last": suppressed,
                    },
                )
            )
        except EvidenceUnavailableError:
            pass

    # ================================================================= decide
    def decide(self, request: GovernanceRequest) -> Decision:
        d = self._evaluate(request)
        if request.action is ActionType.TOOL_CALL and request.tool and d.permits_execution:
            d = self._tool_rate_check(request, d)
        try:
            self.m.ledger.enqueue(self._decision_event(request, d))
        except EvidenceUnavailableError:
            if d.risk_tier.fails_closed:
                return self._core_decision(
                    request,
                    DecisionType.BLOCK,
                    ReasonCode.CONTROL_UNAVAILABLE,
                    "Evidence could not be recorded; Tier 1–2 actions are refused.",
                )
            d = d.model_copy(update={"fail_open_applied": True})
        return d

    def _tool_rate_check(self, request: GovernanceRequest, d: Decision) -> Decision:
        """Per-tool requests-per-minute limits (scopes `tool:<name>` and `ptool:<who>:<name>`).
        Counted only for a call that is otherwise permitted. Calls held for approval are not
        counted: a human decision is already the throttle. If the counter store is down,
        Tier 1-2 fail closed and Tier 3-4 proceed (same rule as every other control)."""
        try:
            self.m.budget.check_rate(d.budget_scopes, 0)
        except RateLimitedError as e:
            return self._core_decision(
                request, DecisionType.BLOCK, ReasonCode.RATE_LIMITED,
                f"Rate limit exceeded for {e.budget_key}; try again in a minute.",
            )  # fmt: skip
        except BudgetStoreUnavailable:
            if d.risk_tier.fails_closed:
                return self._core_decision(
                    request, DecisionType.BLOCK, ReasonCode.CONTROL_UNAVAILABLE,
                    "Rate-limit counters unavailable; Tier 1-2 fail closed.",
                )  # fmt: skip
        return d

    def _evaluate(self, request: GovernanceRequest) -> Decision:
        ctx = request.context
        tier = ctx.register_entry.risk_tier
        hit = self.m.killswitch.match(
            {
                KillScope.ORG: ctx.principal.org_id,
                KillScope.PRINCIPAL: ctx.principal.id,
                KillScope.KEY: ctx.key_id,
                KillScope.APPLICATION: ctx.application_id,
                KillScope.PROJECT: ctx.project_id,
                KillScope.REGISTER_ENTRY: ctx.register_entry.id,
                KillScope.MODEL: request.model,
                KillScope.TOOL: request.tool,
                KillScope.MCP_SERVER: self._tool_server(request.tool),
                KillScope.CHANNEL: request.channel.value,
            }
        )
        if hit:
            return self._core_decision(
                request,
                DecisionType.BLOCK,
                ReasonCode.KILL_SWITCH_ACTIVE,
                f"Kill switch active on {hit.scope.value} '{hit.target}': {hit.reason}",
            )

        tool_facts: ToolFacts | None = None
        if self.m.registry is not None and request.coverage is CoverageClass.GOVERNED:
            blocked = self._registry_check(request)
            if isinstance(blocked, Decision):
                return blocked
            tool_facts = blocked

        fail_open = False
        report = self.m.guardrails.inspect(request.contents, tier)
        if report.oversize:
            return self._core_decision(
                request,
                DecisionType.BLOCK,
                ReasonCode.CONTENT_TOO_LARGE,
                "Content exceeds size limit.",
            )
        if report.guard_unavailable and request.coverage is CoverageClass.GOVERNED:
            if tier.fails_closed:
                return self._core_decision(
                    request,
                    DecisionType.BLOCK,
                    ReasonCode.CONTROL_UNAVAILABLE,
                    "Guard model unavailable; Tier 1–2 fail closed.",
                )
            fail_open = True

        findings = tuple(report.findings)
        risk = assess(request, findings, tool_facts)
        try:
            r = self.m.policy.evaluate(build_input(request, findings, tool_facts, risk))
        except PolicyUnavailableError as e:
            if tier.fails_closed:
                return self._core_decision(
                    request,
                    DecisionType.BLOCK,
                    ReasonCode.CONTROL_UNAVAILABLE,
                    f"Policy engine unavailable; Tier 1–2 fail closed. ({e})",
                    findings=findings,
                    risk=risk,
                )
            return self._core_decision(
                request,
                DecisionType.ALLOW,
                ReasonCode.CONTROL_UNAVAILABLE_FAIL_OPEN,
                "Policy engine unavailable; Tier 3–4 fail open and this call is flagged.",
                findings=findings,
                fail_open=True,
                risk=risk,
            )

        redacted = None
        if r.decision is DecisionType.ALLOW_WITH_REDACTION:
            mapping = self._masks.new(request.request_id, ctx.principal.id)
            redacted = apply_redaction(request.contents, findings, mapping)
        cap = r.limits.get("max_output_tokens")
        mode = r.limits.get("streaming")
        return Decision(
            request_id=request.request_id,
            trace_id=request.trace_id,
            decision=r.decision,
            reason_code=r.reason_code,
            explanation=r.explanation,
            principal_id=ctx.principal.id,
            register_id=ctx.register_entry.id,
            channel=request.channel,
            model=request.model,
            policy_bundle=r.policy_bundle,
            policy_version=r.policy_version,
            matched_policy_ids=r.policy_ids,
            risk_tier=tier,
            limits=Limits(
                max_output_tokens=int(cap) if isinstance(cap, int) else None,
                streaming_mode=str(mode) if isinstance(mode, str) else None,
            ),
            redacted_contents=redacted,
            fail_open_applied=fail_open,
            findings=findings,
            risk_level=risk.level,
            risk_score=risk.score,
            risk_factors=risk.factors,
            budget_scopes=budget_scopes(ctx, request.model, request.tool),
        )

    def _registry_check(self, request: GovernanceRequest) -> Decision | ToolFacts | None:
        """What exists and is enabled (registry) — before policy decides what is allowed."""
        reg = self.m.registry
        assert reg is not None
        if request.action is ActionType.MODEL_CALL and request.model:
            if not reg.is_callable(request.model):
                return self._core_decision(
                    request,
                    DecisionType.BLOCK,
                    ReasonCode.MODEL_NOT_REGISTERED,
                    f"Model '{request.model}' is not in the registry or is disabled.",
                )
        if request.action is ActionType.TOOL_CALL and request.tool:
            tool = reg.usable_tool(request.tool)
            if tool is None:
                return self._core_decision(
                    request,
                    DecisionType.BLOCK,
                    ReasonCode.TOOL_NOT_REGISTERED,
                    f"Tool '{request.tool}' is not in the registry or is disabled.",
                )
            errors = reg.validate_arguments(tool, request.tool_arguments)
            if errors:
                return self._core_decision(
                    request,
                    DecisionType.BLOCK,
                    ReasonCode.TOOL_ARGUMENTS_INVALID,
                    "Tool arguments do not match the registered schema: " + "; ".join(errors[:5]),
                )
            return ToolFacts(tool.impact.value, tool.risk.value, tool.read_only)
        return None

    def _core_decision(
        self,
        request: GovernanceRequest,
        decision: DecisionType,
        reason: ReasonCode,
        why: str,
        findings: tuple = (),  # type: ignore[type-arg]
        fail_open: bool = False,
        risk: RiskAssessment | None = None,
    ) -> Decision:
        ctx = request.context
        risk = risk or assess(request, tuple(findings), None)
        return Decision(
            risk_level=risk.level,
            risk_score=risk.score,
            risk_factors=risk.factors,
            request_id=request.request_id,
            trace_id=request.trace_id,
            decision=decision,
            reason_code=reason,
            explanation=why,
            principal_id=ctx.principal.id,
            register_id=ctx.register_entry.id,
            channel=request.channel,
            model=request.model,
            policy_bundle=CORE_BUNDLE,
            policy_version=CORE_VERSION,
            matched_policy_ids=(f"captain.core.{reason.value.lower()}",),
            risk_tier=ctx.register_entry.risk_tier,
            fail_open_applied=fail_open,
            findings=findings,
            budget_scopes=budget_scopes(ctx, request.model, request.tool),
        )

    def _decision_event(self, request: GovernanceRequest, d: Decision) -> UsageEvent:
        return UsageEvent(
            request_id=request.request_id,
            trace_id=request.trace_id,
            event_type=_EVENT_FOR[d.decision],
            channel=request.channel,
            coverage=request.coverage,
            principal_id=d.principal_id,
            register_id=d.register_id,
            decision=d.decision,
            reason_code=d.reason_code,
            policy_version=f"{d.policy_bundle}@{d.policy_version}",
            model=request.model,
            tool=request.tool,
            details={
                "risk_level": d.risk_level.value if d.risk_level else None,
                "risk_score": d.risk_score,
                "fail_open": d.fail_open_applied,
                "detected": summarize_findings(d.findings),
            },
        )

    # ================================================================= budget
    def reserve(self, decision: Decision, estimate: CostEstimate) -> Reservation:
        if not decision.permits_execution:
            raise BudgetExceededError("decision does not permit execution", "n/a")
        try:
            # Budget first, then rate: a call refused for budget must not use up a
            # rate-limit slot. If the rate limit refuses, the reservation is released.
            res = self.m.budget.reserve(
                decision.request_id, decision.budget_scopes, estimate.amount
            )
            try:
                self.m.budget.check_rate(
                    decision.budget_scopes, estimate.input_tokens + estimate.max_output_tokens
                )
            except BudgetExceededError:
                self.m.budget.settle(res, 0)
                raise
        except BudgetExceededError as e:
            # UC-02: a budget or rate refusal costs nothing and is written to the ledger.
            self.m.ledger.enqueue(
                UsageEvent(
                    request_id=decision.request_id,
                    trace_id=decision.trace_id,
                    event_type=EventType.CALL_REFUSED,
                    channel=decision.channel,
                    coverage=CoverageClass.GOVERNED,
                    principal_id=decision.principal_id,
                    register_id=decision.register_id,
                    decision=DecisionType.BLOCK,
                    reason_code=e.reason_code,
                    policy_version=f"{CORE_BUNDLE}@{CORE_VERSION}",
                )
            )
            raise
        except BudgetStoreUnavailable as e:
            if decision.risk_tier.fails_closed:
                raise CoreError(
                    f"budget store unavailable: {e}", ReasonCode.CONTROL_UNAVAILABLE
                ) from e
            return Reservation(
                request_id=decision.request_id,
                amount=0,
                budget_keys=(),
                expires_at=self._now(),
                trace_id=decision.trace_id,
                principal_id=decision.principal_id,
                register_id=decision.register_id,
                channel=decision.channel,
                model=decision.model,
                scopes=decision.budget_scopes,
                fail_open_applied=True,
            )
        return res.model_copy(
            update={
                "trace_id": decision.trace_id,
                "principal_id": decision.principal_id,
                "register_id": decision.register_id,
                "channel": decision.channel,
                "model": decision.model,
                "scopes": decision.budget_scopes,
            }
        )

    def _finish(
        self, res: Reservation, charged: int, event: EventType, usage: ActualUsage | None
    ) -> Settlement:
        if res.budget_keys:
            try:
                self.m.budget.settle(res, charged)
            except UnknownReservation as e:
                raise BudgetExceededError("reservation unknown or already settled", "n/a") from e
        self.m.ledger.enqueue(
            UsageEvent(
                request_id=res.request_id,
                trace_id=res.trace_id,
                event_type=event,
                channel=res.channel,
                coverage=CoverageClass.GOVERNED,
                principal_id=res.principal_id or None,
                register_id=res.register_id or None,
                model=res.model,
                usage=usage,
                details={"scopes": ",".join(res.scopes), "fail_open": res.fail_open_applied},
            )
        )
        return Settlement(
            reservation_id=res.reservation_id,
            reserved=res.amount,
            charged=charged,
            released=max(res.amount - charged, 0),
        )

    def settle(self, reservation: Reservation, actual: ActualUsage) -> Settlement:
        return self._finish(reservation, actual.cost, EventType.USAGE_SETTLED, actual)

    def release(self, reservation: Reservation) -> Settlement:
        return self._finish(reservation, 0, EventType.USAGE_RELEASED, None)

    # =============================================================== approvals
    def request_approval(self, request: GovernanceRequest, decision: Decision) -> ApprovalTicket:
        return self.m.approvals.request(request, decision)

    def approval_status(self, ticket_id: UUID) -> ApprovalTicket:
        return self.m.approvals.status(ticket_id)

    def find_approval(self, request: GovernanceRequest) -> ApprovalTicket | None:
        return self.m.approvals.find_open(request)

    def consume_approval(self, ticket_id: UUID, request: GovernanceRequest) -> ApprovalTicket:
        def recheck() -> None:
            d = self._evaluate(request)
            if d.decision is DecisionType.BLOCK:
                raise ApprovalError(f"no longer permitted: {d.explanation}", d.reason_code)

        return self.m.approvals.consume(ticket_id, request, recheck)

    def decide_approval(
        self, ticket_id: UUID, approve: bool, approver_id: str, reason: str
    ) -> ApprovalTicket:
        return self.m.approvals.decide(ticket_id, approve, approver_id, reason)

    # ================================================================= record
    def record(self, event: UsageEvent) -> LedgerReceipt:
        return self.m.ledger.enqueue(event)

    # ============================================================ kill switch
    def kill(self, scope: KillScope, target: str, reason: str, actor_id: str) -> None:
        """Safety-increasing: the kill always takes effect first, then it is recorded.
        If recording fails the error is raised, but the kill stays active."""
        self.m.killswitch.activate(scope, target, reason, actor_id)
        if scope is KillScope.PRINCIPAL:
            self.m.approvals.cancel_for_principal(target, f"kill switch: {reason}")
        if scope is KillScope.KEY and not target.startswith("wl_"):
            # Workload ids have no stored key; the kill switch entry alone blocks them.
            self.m.identity.revoke_key(target, f"kill switch: {reason}")
        self.m.ledger.enqueue(
            admin_event(
                EventType.KILL_SWITCH,
                actor_id,
                {"action": "activate", "scope": scope.value, "target": target, "reason": reason},
            )
        )

    def unkill(self, scope: KillScope, target: str, reason: str, actor_id: str) -> None:
        """Safety-decreasing: recorded first; refused if evidence cannot be written."""
        self.m.ledger.enqueue(
            admin_event(
                EventType.KILL_SWITCH,
                actor_id,
                {"action": "deactivate", "scope": scope.value, "target": target, "reason": reason},
            )
        )
        self.m.killswitch.deactivate(scope, target)


ADMIN_PARTITION = "admin"
SECURITY_PARTITION = "security"
SECURITY_EVENT_INTERVAL_S = 30.0
SECURITY_EVENTS_PER_MINUTE = 60


def admin_event(
    event_type: EventType,
    actor_id: str,
    details: dict[str, str | int | float | bool | None],
    register_id: str | None = None,
) -> UsageEvent:
    """Administrative actions go to the register entry's partition when they concern one,
    otherwise to the shared 'admin' partition."""
    return UsageEvent(
        trace_id=f"admin:{event_type.value}",
        event_type=event_type,
        channel=Channel.API,
        coverage=CoverageClass.GOVERNED,
        principal_id=actor_id,
        register_id=register_id or ADMIN_PARTITION,
        details=details,
    )


def approval_event(t: ApprovalTicket) -> UsageEvent | None:
    """Ledger event for approval state changes (wired as ApprovalService.on_change)."""
    if t.state is ApprovalState.PENDING and t.escalated_at is None:
        return None  # the hold itself is already recorded by decide()
    etype = (
        EventType.APPROVAL_ESCALATED
        if t.state is ApprovalState.PENDING
        else EventType.APPROVAL_DECIDED
    )
    return UsageEvent(
        request_id=t.request_id,
        trace_id=str(t.ticket_id),
        event_type=etype,
        details={
            "state": t.state.value,
            "approvers": ",".join(t.assigned_approvers),
            "reason": t.reason,
        },
        channel=t.channel,
        coverage=CoverageClass.GOVERNED,
        principal_id=t.approver_id or t.requested_by,
        register_id=t.register_id or None,
    )
